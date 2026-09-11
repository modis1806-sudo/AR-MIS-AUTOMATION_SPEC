# AR MIS — Tally Extraction & Automation Pipeline

Implementation of `AR_MIS_Automation_Spec.md`. Read that document first — this
README only covers build-specific decisions and how to run things.

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

## Running the weekly cycle

```
python -m ar_mis.cli 2026-01-05    # week ending date; defaults to today
```

Before the first real run, edit `ar_mis/config.py`'s `BRANCHES` list with the
client's actual branch names and exact Tally company names, and seed
`ar_mis/storage.py`'s `customer_master` table with each party's Pre-MIS
Outstanding baseline (Layer 1) — there is deliberately no automatic seeding
path, since that number must come from an explicit one-time load, not a
weekly extraction run.

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
