# AR MIS — Tally Extraction & Automation Pipeline

Implementation of `AR_MIS_Automation_Spec.md`. Read that document first — this
README only covers build-specific decisions and how to run things.

## Deployment target

This is a Python application (backend + a small local web app). It must run on
a machine that can reach Tally's XML/HTTP gateway (default `http://<host>:9000`)
directly — same office LAN as Tally, or a private VPN link to it. Requirements:

- Python 3.10+
- `pip install -e .` (installs Flask and openpyxl)

**Do not** run this on a public cloud VM with no private network path to
Tally, and do not open Tally's port 9000 to the public internet to make that
work. Tally's XML/HTTP interface has no authentication of its own — anyone
who can reach that port can query it. The spec's "LAN-only, no cloud
dependency" framing exists specifically to keep that interface unreachable
from outside the office network; that's a security boundary, not an
implementation detail to route around.

## Decisions on Section 8 open items

These were open in the spec and have been resolved by the client before build:

1. **Tie-out tolerance (4.1):** zero tolerance. Any mismatch between the
   extracted YTD closing balance and the computed roll-forward
   (Opening + Sales + CN + DN + Receipts + Journals) for a party fails that
   party's reconciliation. All monetary arithmetic in this codebase uses
   `decimal.Decimal` rounded to 2 places (paise) — never `float` — specifically
   so a zero-tolerance comparison means "genuine mismatch", not floating-point
   noise. See `ar_mis/money.py`.
2. **Per-branch extraction failure (4.3):** a branch that fails extraction
   (connection drop, malformed XML, company-mismatch) is skipped and the run
   continues with the remaining branches, then **the failed branch(es) are
   retried once at the end of the run**, after the rest of the queue has been
   processed. Only a branch that still fails on that retry pass is recorded as
   a final FAILED branch for the week (excluded from the consolidated report,
   flagged explicitly — never silently dropped, never shown as stale prior-week
   data). See `ar_mis/orchestration.py`.
3. **CFO consumption mode (Section 6):** static weekly snapshot report,
   matching the confirmed weekly cadence. Interactive drill-down is a secondary
   tool for AR Managers, not the CFO's primary view. See `ar_mis/reporting.py`.
4. **PTP status granularity (Section 6):** per-entry status
   (Active / Kept / Broken), consistent with the existing Layer 3 Action Log
   design.

## Known limitation: no live Tally connection in this build environment

This pipeline was developed in a sandboxed environment with no LAN access to
the client's Tally instance. `ar_mis/tally_client.py` implements Tally's
documented XML/HTTP request-response protocol (default gateway
`http://<tally-host>:9000`) and has been exercised against synthetic fixture
XML in `fixtures/` and `tests/`, matching Tally's published export schema
(`ENVELOPE` / `BILLALLOCATIONS.LIST` / `LEDGERENTRIES.LIST` structure).

**It has not been run against a real Tally instance.** Before the first live
run:
- Confirm the exact Tally version/release in use (TallyPrime vs Tally.ERP 9 —
  XML schema has minor differences between them).
- Confirm the ODBC/HTTP gateway port and that "Act as ODBC/HTTP Server" is
  enabled per company.
- Run `ar_mis/tally_client.py`'s `confirm_current_company()` against a real
  session and verify it correctly reflects the loaded company before trusting
  it as the Section 2.2 safety check.
- Validate one branch end-to-end in parallel with the manual process (Section 5
  parallel-run requirement) before removing any manual step.

## Layout

- `ar_mis/money.py` — Decimal-based money type, zero-tolerance comparison.
- `ar_mis/models.py` — data model for parties, vouchers, ledger entries.
- `ar_mis/xml_requests.py` — builds Tally XML export request envelopes.
- `ar_mis/tally_client.py` — HTTP transport + current-company confirmation.
- `ar_mis/parsers.py` — parses Tally XML responses into models.
- `ar_mis/sign.py` — the single uniform sign-flip rule (Section 2.3).
- `ar_mis/rollforward.py` — party-level roll-forward computation (Section 2.4).
- `ar_mis/storage.py` — append-only SQLite store for Layers 1–3.
- `ar_mis/reconciliation.py` — party-level gate (4.1) + YTD cross-check (4.2).
- `ar_mis/orchestration.py` — human-in-the-loop branch loop + retry queue.
- `ar_mis/gate.py` — PASS/FAIL aggregation + output gate (4.4).
- `ar_mis/reporting.py` — static weekly snapshot report (Section 6).
- `ar_mis/pipeline.py` — wires extraction → roll-forward → reconciliation →
  storage into the per-branch runner orchestration drives.
- `ar_mis/cli.py` — the actual script run each Monday (Section 2.2); ties
  orchestration, pipeline, the 4.2 drift check, the 4.4 gate, and reporting
  into one weekly command.
- `ar_mis/seed.py` — one-time Layer 1 Pre-MIS Outstanding load from a CSV.
- `ar_mis/webapp/` — local web app: Branch Master (extraction config as
  user-editable data) and Test Extraction (a self-serve connectivity +
  extraction check against a real Tally instance).

## Seeing the application: the local web app

Before touching real data, use this to (a) configure branches yourself
without editing any Python file, and (b) check this can actually reach and
pull from your Tally instance:

```
python -m ar_mis.webapp.app
```

Then open `http://127.0.0.1:5000` in a browser, on the same machine (or LAN)
that can reach Tally. Two screens:

- **Branch Master** — add/edit/remove branches: branch name, exact Tally
  company name, host, port. This replaced a hardcoded list in
  `ar_mis/config.py` specifically so this is something you configure, not
  something you edit source code for.
- **Discover Companies** — asks Tally what companies are actually open right
  now at a given host/port, and lets you pick one ("Use this company")
  instead of having to already know and correctly type the exact current
  name. Exists because that name can change: a shared/multi-user Tally
  deployment (or a standalone Tally Gateway Server service, as opposed to
  whichever interactive Tally window a person happens to have open) may
  report a different "currently open" company at different moments.
- **Test Extraction** — pick a branch, click Run Test. It runs the real
  Section 2.2 company-check, a voucher pull, and a Sundry Debtors pull
  against that branch's Tally instance and shows PASS/FAIL per step with the
  actual error if something fails (wrong company loaded, Tally unreachable,
  etc.). Read-only — nothing is written to the database. This is the fastest
  way to find out whether host/port/company-name config is right before any
  real weekly run depends on it.
- **Manual Upload** — a fallback for when the live Tally connection isn't
  reachable at all (built after the client's own Tally Gateway Server proved
  unreliable during real testing). Upload the same data by hand, exported
  from Tally's own File → Export menu: the five weekly voucher types, the
  Trial Balance/Sundry Debtors closing balance as-on the reporting date, and
  optionally a YTD voucher-wise detail file for the Section 4.2 drift check.
  Runs through the exact same roll-forward, auto-discovery, and
  zero-tolerance reconciliation as a live extraction
  (`ar_mis.pipeline.process_branch_data`) — parsing never cared whether XML
  arrived over HTTP or as a file, so neither does anything downstream of it.
  Refuses outright if the branch/week already has recorded data from either
  a live run or an earlier upload — first one in wins, no silent overwrite.

## Diagnosing a live Tally connection from the command line

`ar_mis/diagnostics.py` is a small CLI for looking at exactly what a Tally
instance is saying, rather than guessing:

```
python -m ar_mis.diagnostics companies --host localhost --port 9000 --raw
python -m ar_mis.diagnostics vouchers --company "Exact Name" --days 7 --output dump.txt
python -m ar_mis.diagnostics ledgers --company "Exact Name" --as-of 2026-04-07
```

This is the generalized, committed form of one-off scripts that came out of
real troubleshooting against a live TallyPrime instance during this
project's first on-site test: the "List of Companies" request this codebase
originally used turned out to return "Unknown Request, cannot be processed"
against that real server, and only reading the raw XML directly (via what
became this CLI) revealed the request shape that actually worked. Keep
reaching for this whenever a new Tally version/edition behaves unexpectedly.

## One-time setup: seeding Layer 1 (Pre-MIS Outstanding)

Before the first weekly run for a branch, load each party's Pre-MIS
Outstanding baseline (Section 2.5) from a CSV:

```
python -m ar_mis.seed pre_mis_outstanding.csv --db-path data/ar_mis.db --dry-run
python -m ar_mis.seed pre_mis_outstanding.csv --db-path data/ar_mis.db
```

CSV columns: `party_id,party_name,branch_id,pre_mis_outstanding`. Amounts must
already be in this pipeline's sign convention (Dr positive / Cr negative,
Section 2.3), not Tally's raw export. See
`fixtures/sample_pre_mis_outstanding.csv` for the format.

This is guarded to match Section 2.5's "must not move except through
explicit, logged adjustment": re-running against an unchanged CSV is a safe
no-op; a changed balance for a party that already has weekly snapshot data on
record is refused outright, `--force` included — at that point the only path
is `Store.record_pre_mis_adjustment()`, a deliberate logged correction, not a
bulk re-load. A changed balance before any weekly run exists for that party
(i.e. still fixing a bad initial load) requires `--force` and is reported
either way — nothing is overwritten silently.

**New customers that arrive after the seed is loaded are handled automatically,
not by editing the CSV again.** If extraction finds a party in Tally's Sundry
Debtors ledger with no customer_master record, it auto-creates one with
Pre-MIS Outstanding = 0 — the only correct value for a party that didn't exist
at go-live, given the seed CSV is a comprehensive one-time list of everyone
who did. It's still surfaced as a "New party this week" line in the report's
Exceptions section, not silently absorbed — worth a human noticing even
though the number itself isn't in doubt.

## Voucher type matching: contains, not exact

Real Tally deployments commonly customize voucher type names with prefixes
or suffixes (e.g. "Sales - Export", "GST Credit Note") while the underlying
category is unchanged. Extraction no longer filters by voucher type
server-side (an earlier version did, via a TDL formula) — it pulls every
voucher in the date range in one request and categorizes each one
client-side (`parsers.categorize_voucher_type`) by matching the raw
`VOUCHERTYPENAME` against the five category keywords, case-insensitively,
as a substring. A voucher that matches none of them (Payment, Contra,
Purchase, Stock Journal, and so on) is silently excluded — that's the normal
case for most of a company's vouchers, not an error. One deliberate
exclusion: "Stock Journal" is never categorized as Journal despite
containing that word, since it's Tally's built-in name for an inventory
transfer between godowns, unrelated to an accounting Journal voucher.

## Running the weekly cycle

```
python -m ar_mis.cli 2026-01-05    # week ending date; defaults to today
```

Before the first real run, also edit `ar_mis/config.py`'s `BRANCHES` list
with the client's actual branch names and exact Tally company names.

## Known gap: no ageing-bucket detail yet

Section 6 asks for an Ageing Matrix and a `>180-day` "Bad Debt Risk" KPI —
exactly the two areas responsible for two of the six original defects (a
bucket sub-split not summing to its own combined total, and a Bad Debt Risk
figure disagreeing with the Ageing Matrix for the same date). Building these
correctly requires bill-level due-date and `BILLTYPE` (New Ref vs Against
Ref) data that the current extraction layer does not pull. Rather than derive
an ageing bucket from closing balances alone — which would silently
reintroduce the same class of unvalidated-figure bug this project exists to
remove — this build ships without it. Adding it means extending
`xml_requests.py`'s bill-allocation fetch to include `BILLTYPE` and due date,
and a new module to bucket open bills by age as of the report date.

## Running tests

```
pip install -e .[dev]
pytest
```
