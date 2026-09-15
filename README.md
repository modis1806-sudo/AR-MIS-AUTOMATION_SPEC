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

## Live Tally connection: Test Extraction passes end-to-end against real TallyPrime

This pipeline was originally developed in a sandboxed environment with no LAN
access to a real Tally instance, exercised only against synthetic fixture XML
in `fixtures/` and `tests/`. It has since been tested live against a real
TallyPrime install via the webapp's Test Extraction page. Four real bugs were
found and fixed along the way — the fourth one is worth reading carefully,
since it silently corrupted correctness after the first fix appeared to work:

- The original voucher export request (a TDL `Collection` definition
  fetching `ALLLEDGERENTRIES.LIST`) **hung indefinitely against real
  TallyPrime** — no error, no response, just a dead socket — the moment any
  nested list-type field was requested, regardless of exact FETCH syntax.
  Flat-field Collection requests worked fine, which is how this was
  isolated. Fixed by switching to a REPORTNAME-based "Export Data" request
  instead, which returns the full native voucher object with no FETCH list
  needed at all.
- Once vouchers were flowing, `parsers.py` hit two real Tally XML
  well-formedness quirks: numeric character references to codepoints illegal
  in XML 1.0 (`<GSTCLASS>&#4; Not Applicable</GSTCLASS>`), and the same kind
  of illegal control character appearing instead as a raw literal byte
  elsewhere in the same export. Both are now stripped in `_sanitize_xml`
  before parsing — see that module's docstring.
- **The REPORTNAME first tried, `Day Book`, silently ignored
  `SVFROMDATE`/`SVTODATE` entirely.** Test Extraction reported PASS with
  plausible-looking voucher counts, which looked like success — but
  requesting two different, non-overlapping date ranges returned a
  byte-identical response both times, always showing whatever period
  Tally's own UI session happened to have set (1-Apr-2026) rather than the
  requested range. This was caught by deliberately testing two date ranges
  side by side, not by the original passing test alone — a passing
  connectivity check is not the same as a correctness check. Fixed by
  switching to `REPORTNAME=Voucher Register`, confirmed to (a) correctly
  return different vouchers for different date ranges and (b) still carry
  the exact same `ALLLEDGERENTRIES.LIST`/`BILLALLOCATIONS.LIST` detail Day
  Book did. See `voucher_export_request()`'s docstring for the full
  diagnostic trail across all of the above.
- **A fifth bug surfaced only once a large date range was tried**: Voucher
  Register can hang/time out on a wide range in a way that does **not** scale
  linearly with calendar days — confirmed by bisecting a month in half: two
  independent 15-day halves each succeeded in ~1 minute, but the same month
  requested as one 30-day range failed even at a 180-second timeout. This is
  a real data-volume effect in Tally's own report engine, not something this
  codebase can predict per company in advance. Fixed by having
  `TallyClient.fetch_vouchers` transparently split any requested range into
  14-day chunks and stitch the results together — real margin under the
  confirmed-working 15-day figure, since one extra day of margin isn't much
  for a busier company. A normal ~7-day weekly pull still issues exactly one
  request, unchanged; only a genuinely wide range (the Section 4.2 YTD
  cross-check, or an operator picking a multi-month backfill range in Test
  Extraction) gets split. The 15s/30s timeouts used across this codebase were
  also too low for a real chunk's realistic duration and have been raised
  (`tally_client.DEFAULT_TIMEOUT_SECONDS`).

The actual field-reading logic (`LEDGERNAME`, `AMOUNT`, `NAME` by plain tag
name) needed **no changes** through any of this — every bug found was in
request-building or input-sanitization, never in how data is interpreted
once parsed.

Confirmed working against real TallyPrime, correctly scoped to the requested
date range: real Sales, Receipt, Journal, Payment and other voucher types
pulled with full ledger-entry and bill-allocation detail, and Sundry Debtors
ledgers pulled for the YTD cross-check — the first time this pipeline has
processed real production-shaped data end-to-end, with the date-range
correctness actually verified rather than assumed from one passing test.

Still outstanding before the first live production run:
- Confirm the exact Tally version/release in use at the client (TallyPrime vs
  Tally.ERP 9 — XML schema has minor differences between them); this was
  validated against TallyPrime specifically.
- The three fixes above were validated via Test Extraction (a read-only
  connectivity + extraction check). The full pipeline — roll-forward,
  reconciliation, storage writes (`ar_mis.pipeline.process_branch_data`) —
  has not yet been run against this real data.
- Validate one branch end-to-end in parallel with the manual process (Section 5
  parallel-run requirement) before removing any manual step.

## Layout

Extraction and core reconciliation:
- `ar_mis/money.py` — Decimal-based money type, zero-tolerance comparison.
- `ar_mis/models.py` — data model for parties, vouchers, ledger entries, and
  every register/report row type below.
- `ar_mis/config.py` — branch config type, financial-year helpers, and
  `split_into_chunks` (extraction is chunked into ~7-day pieces purely for
  Tally request-size reasons — see "Continuous extraction" below for why
  this is NOT the same as the Monday–Sunday week-snapping that was removed).
- `ar_mis/xml_requests.py` — builds Tally XML export request envelopes. Each
  function's docstring is a live diagnostic trail of every request shape
  tried against real Tally and why it did or didn't work — read before
  changing one of these, not just before writing a new one.
- `ar_mis/tally_client.py` — HTTP transport, current-company confirmation,
  transparent voucher-fetch chunking for wide date ranges.
- `ar_mis/parsers.py` — parses Tally XML responses into models.
- `ar_mis/sign.py` — the single uniform sign-flip rule (Section 2.3).
- `ar_mis/rollforward.py` — party-level roll-forward computation (Section 2.4).
- `ar_mis/storage.py` — append-only SQLite store for every layer, with an
  automatic schema-version migration mechanism (`SCHEMA_VERSION`/
  `MIGRATIONS`, backs up the database before migrating an existing one).
- `ar_mis/reconciliation.py` — party-level gate (4.1), YTD drift cross-check
  (4.2, backdated/altered-entry detection).
- `ar_mis/drift_correction.py` — the correction mechanism for a Drift
  Finding: replays its original voucher into the registers under a new,
  Maker-chosen week, never editing the locked historical week.
- `ar_mis/orchestration.py` — human-in-the-loop branch loop + retry queue.
- `ar_mis/gate.py` — PASS/FAIL aggregation + output gate (4.4).
- `ar_mis/reporting.py` — static weekly snapshot report (Section 6).
- `ar_mis/pipeline.py` — wires extraction → roll-forward → reconciliation →
  register-building → storage into the per-branch runner orchestration
  drives; the one function (`process_branch_data`) that both a live
  Tally pull and a manual XML upload both call, so neither path can drift
  from the other.
- `ar_mis/cli.py` — the actual script run each Monday (Section 2.2); ties
  orchestration, pipeline, the 4.2 drift check, the 4.4 gate, and reporting
  into one weekly command. Reads its branch list from Branch Master
  (`Store.list_branches()`) — `ar_mis/config.py` no longer carries a
  hardcoded branch list at all.
- `ar_mis/seed.py` — one-time Layer 1 Pre-MIS Outstanding load, shared by
  the CLI tool (multi-branch CSV) and the webapp's own branch-scoped
  upload (see "One-time setup" below).

Registers and reporting (the actual day-to-day screens):
- `ar_mis/registers.py` — builds the three master registers (Sales & DN,
  Credit Note, Receipt & Journal) from vouchers; tax classification, Due
  Date, bill-reference matching, PTP status.
- `ar_mis/register_export.py` — Excel export for every register/report
  screen that has one (openpyxl, Indian digit grouping preserved as real
  numbers, not display strings).
- `ar_mis/dashboard.py` — AR Snapshot (KPI dashboard) and Branch-wise
  Ageing Schedule.
- `ar_mis/ageing_matrix.py` — Ageing Matrix (branch summary + customer
  detail, cross-checked against Tally's own ledger closing balance).
- `ar_mis/exception_register.py` — the six Exception Register sub-reports
  (unapplied cash/CN, negative open amount, non-active debtors, unresolved
  references, top 20 overdue).
- `ar_mis/weekly_movement.py` — Weekly Movement Register: a recorded,
  locked-once history, not a live recalculation like every other report.
- `ar_mis/reconciliation_report.py` — TB Reconciliation Cross-Check
  (Tally's own closing balance next to this app's own workings, one row
  per party per branch per week, mismatches never discarded) and AR
  Concentration Risk (top N parties by outstanding, as a % of total AR).
- `ar_mis/branch_totals.py` — Branch Sales + CN + DN Total, a plain
  per-branch turnover figure to eyeball against Tally's own P&L.
- `ar_mis/manual_upload.py` — the fallback extraction path (see "Manual
  Upload" below) — parses hand-exported Tally XML through the exact same
  `process_branch_data` a live pull uses.
- `ar_mis/diagnostics.py` — small CLI for inspecting exactly what a live
  Tally instance is saying, rather than guessing (see its own section
  below).
- `ar_mis/webapp/` — the local Flask app tying all of the above into one
  UI, plus `ar_mis/webapp/static/table_tools.js`: one shared,
  dependency-free script providing search, column filters, live
  SUMIFS-style totals, and select-all-visible-rows on every register/
  report table, applied via `data-tt-*` attributes in each template.

## Seeing the application: the local web app

Before touching real data, use this to (a) configure branches yourself
without editing any Python file, and (b) check this can actually reach and
pull from your Tally instance:

```
python -m ar_mis.webapp.app
```

Then open `http://127.0.0.1:5000` in a browser, on the same machine (or LAN)
that can reach Tally. First visit asks you to pick a role — **Maker** (full
access: configure branches, extract/upload data, record corrections) or
**Checker** (read-only across every screen) — a placeholder role split, not
real authentication; see "Known gaps" below. Screens are grouped into three
nav sections:

### Extraction

- **Branch Master** — add/edit/remove branches: branch name, exact Tally
  company name, host, port. This replaced a hardcoded list in
  `ar_mis/config.py` specifically so this is something you configure, not
  something you edit source code for. Each branch row also has:
  - **Data on Record** — the date range and week count actually extracted
    for that branch, and a **Delete latest week** action that undoes
    exactly one mistaken extraction (only ever the branch's own most
    recently recorded week — every earlier week's closing balance is the
    next week's opening, so deleting out of order would strand that
    roll-forward chain).
  - **Catch up a party** — see its own section below.
  - An optional **Pre-MIS Outstanding CSV** upload right on the branch
    create/edit form — see "One-time setup" below.
- **Discover Companies** — asks Tally what companies are actually open right
  now at a given host/port, and lets you pick one ("Use this company")
  instead of having to already know and correctly type the exact current
  name. Exists because that name can change: a shared/multi-user Tally
  deployment (or a standalone Tally Gateway Server service, as opposed to
  whichever interactive Tally window a person happens to have open) may
  report a different "currently open" company at different moments.
- **Test & Save Extraction** — pick a branch and a date range (any range —
  a day, a month, a full financial year for a first-time backfill; run
  exactly as typed, never snapped to any calendar boundary — see
  "Continuous extraction" below). **Run Test** does a read-only company
  check + voucher pull + Sundry Debtors pull and shows PASS/FAIL per step
  with the actual error if something fails, before anything is written.
  **Save** re-runs the same range and actually writes it — reconciliation,
  registers, everything — chunked into ~7-day pieces for request-size
  reasons, one chunk at a time, so a chunk that already has recorded data
  is safely skipped rather than re-processed.
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

### Registers

One row per real transaction, every branch combined, cumulative from FY
start — the base every report below is derived from, never a separate data
store of its own:

- **Customer Master** — every party on record: Pre-MIS Outstanding, Credit
  Limit (see its own section below), last-human-reconciled date, and
  search/filter/select-all across the full list (real deployments have
  been in the 900+ party range, where scrolling a plain list stopped being
  usable).
- **Sales & DN Register**, **Credit Note Register**, **Receipt & Journal
  Register** — voucher-wise detail with Open Amount/Ageing/PTP status
  recalculated live for whatever date you pick (never a stored total), an
  Excel export button, and the same search/filter/select-all toolbar.

### Reports

- **TB Reconciliation Cross-Check** — Tally's own Sundry Debtors closing
  balance next to this app's own workings (Opening + Sales + DN − CN +
  Receipts + Journals), one row per party per branch per week, **kept
  forever, mismatches included** — the direct answer to "how do I know
  this data is correct?" A From/To range scopes the table; the summary
  tiles (Total Debtor as per Books, parties currently mismatched, total
  absolute difference) always use each party's latest state as of the
  selected date regardless of that range, so they never silently shrink
  just because a narrow window is on screen. **Export list** downloads
  exactly the mismatched parties behind the tile as an Excel file.
- **AR Concentration Risk** — top 5/10/20 parties by current outstanding,
  each as a % of total AR, plus a combined "top N = X% of Total AR"
  headline — is exposure spread across many customers or concentrated in
  a few, independent of whether any of them are currently overdue. Uses
  the same Total AR figure as TB Cross-Check, so the two screens can never
  quietly disagree.
- **Register Exceptions Review** — every voucher Test & Save Extraction or
  Manual Upload couldn't confidently place into a register (no
  PARTYLEDGERNAME, a party with no customer_master record, a voucher that
  touches no tracked Sundry Debtor) — persisted permanently instead of
  vanishing once that run's result page is closed. Two outcomes: **Reviewed
  — no action needed** (a human confirmed the exclusion is genuinely
  correct — zero effect on any figure, an audit note only) or **Resolved
  via catch-up** (the party turned out to be a real, wrongly-excluded
  debtor — links straight into Catch Up a Party with the name pre-filled).
- **AR Snapshot** — the main KPI dashboard: Outstanding Position (Total AR,
  Pre-MIS Outstanding, Related Party AR shown separately from Sundry
  Debtor AR, Unapplied Cash/CN), Performance (Overdue AR and its ageing
  breakdown, Bad Debt Risk 181+ days, Notional Interest Cost at a flat 10%
  p.a., DSO, Collection Efficiency, PTP Kept Rate), Average Collection
  Period by branch. Every figure is as-of-date selectable; nothing here is
  a stored total.
- **Branch-wise Ageing Schedule** — one ageing-bucket row per branch plus
  an all-branches total.
- **Exception Register** — six lists in one screen: Unapplied Cash,
  Unapplied Credit Notes (both netted per party), Negative Open Amount
  invoices, Non-Active Debtors (180+ days silent, balance still open),
  Unresolved References (an "Against Ref" that didn't match any tracked
  invoice), Top 20 Overdue Customers.
- **Ageing Matrix** — a branch-level summary plus customer-level detail,
  the customer view cross-checked against Tally's own ledger closing
  balance with no rounding tolerance — every difference shown exactly as
  computed, never smoothed over.
- **Weekly Movement Register** — unlike every report above, this one does
  **not** recalculate: a Maker explicitly records the current position as
  that week's row, and it's locked in permanently from then on (a week
  already recorded can't be recorded again). Each figure carries a trend
  arrow against the previously recorded week.
- **Branch Sales + CN + DN Total** — a simple per-branch turnover total
  for a chosen period, plain enough to eyeball directly against Tally's
  own P&L page (gross/GST-inclusive by necessity, since Credit Note rows
  never carry a tax split).
- **Backdated Entry Findings** (reached from Reports; see `ar_mis/
  reconciliation.py`/`drift_correction.py`) — a voucher a fresh full-year
  Tally pull found that no prior weekly extraction ever captured. Kept
  permanently until reviewed. **Acknowledge** is an audit note only.
  **Incorporate** is the real fix: replays the voucher into the registers
  under a new week you choose, never editing the locked historical week,
  and needs that party's real Tally closing balance as of that date to
  confirm it actually reconciles clean afterward.

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
Outstanding baseline (Section 2.5) — the balance as it stood the day before
this pipeline went live for that branch, set once and never re-pulled or
recalculated weekly. Two ways to load it, sharing the same underlying
guardrails (`ar_mis/seed.py`):

**From the webapp** (the normal path for a single branch) — on Branch
Master's create/edit form, upload a CSV with columns `party_name,
pre_mis_outstanding` (no `party_id`, no `branch_id` — every row belongs to
the branch you're on, and `party_name` alone is the join key against Tally's
own extraction, so it must be typed exactly as the ledger appears there).
Download the template straight from that form. Amounts use this pipeline's
own sign convention (positive for a debtor who owes money, negative for a
credit balance) — never Tally's raw export, and never a "Dr"/"Cr" suffix.

**From the CLI** (for seeding multiple branches from one shared file):

```
python -m ar_mis.seed pre_mis_outstanding.csv --db-path data/ar_mis.db --dry-run
python -m ar_mis.seed pre_mis_outstanding.csv --db-path data/ar_mis.db
```

CSV columns: `party_id,party_name,branch_id,pre_mis_outstanding`. See
`fixtures/sample_pre_mis_outstanding.csv` for the format. Unlike the webapp
path, this one does ask for a separate `party_id` — kept for backward
compatibility with existing seed files, but note that the live extraction
path always uses the Tally ledger name itself as `party_id` for any
auto-discovered party (see `pipeline.process_branch_data`), so a `party_id`
here that doesn't exactly match the eventual ledger name will silently
never line up with the real party once extraction runs.

Both paths are guarded to match Section 2.5's "must not move except through
explicit, logged adjustment": re-running against an unchanged CSV is a safe
no-op; a changed balance for a party that already has weekly snapshot data on
record is refused outright, `--force` included — at that point the only path
is `Store.record_pre_mis_adjustment()`, a deliberate logged correction, not a
bulk re-load. A changed balance before any weekly run exists for that party
(i.e. still fixing a bad initial load) is applied automatically from the
webapp (there's no way to pass `--force` through a browser, and the
has_weekly_snapshots check already protects the one case that actually
matters) or requires `--force` from the CLI; both report what happened either
way — nothing is overwritten silently.

**New customers that arrive after the seed is loaded are handled automatically,
not by editing the CSV again.** If extraction finds a party in Tally's Sundry
Debtors ledger with no customer_master record, it auto-creates one with
Pre-MIS Outstanding = 0 — the only correct value for a party that didn't exist
at go-live, given the seed CSV is a comprehensive one-time list of everyone
who did. It's still surfaced as a "New party this week" line in the report's
Exceptions section, not silently absorbed — worth a human noticing even
though the number itself isn't in doubt.

## Catch Up a Party — onboarding a debtor Tally excluded

Reached from Branch Master. For a real Sundry Debtor that Tally excluded from
extraction for some real-world reason — most commonly, mistakenly filed under
Sundry Creditors — so neither its opening balance nor any of its vouchers
(sales, receipts, even a Bad Debt write-off Journal) ever reached AR
reconciliation or the real registers.

Deliberately does **not** ask for a hand-typed closing figure — that only
patches the TB total and leaves the Sales/CN/Receipt/Journal registers wrong
for however long the party was excluded. Instead:

1. Fix the ledger's group in Tally first (this tool cannot do that for you).
2. Read that ledger's real balance off Tally as of some past anchor date you
   trust.
3. Enter the party name (exactly as it appears in Tally), that anchor date
   and balance, and how far to catch up through (defaults to today).
4. The tool pulls the party's **real voucher history** since the anchor date
   and replays it through the exact same `process_branch_data` every normal
   weekly run uses — so whatever mix of Sales, Receipts, Credit Notes, or a
   write-off Journal actually happened is handled by the one general
   mechanism that already understands all of them, not a new one invented
   per voucher type.

Refuses outright if the party already has weekly_snapshot history (this tool
is for onboarding a never-tracked party, not correcting an already-tracked
one) or if the party still isn't found in Tally's Sundry Debtors pull
(classification not actually fixed yet, or a name mismatch). Only ever
replays vouchers touching this one party — never the full company-wide pull
for that date range — so no other party's own already-recorded register rows
for the same historical window are touched or duplicated.

## Credit Limit (reference only)

Customer Master carries an editable `credit_limit` per party, defaulting to
Rs 1,00,00,000 (one crore). This is a secondary reporting layer, not the
system of record — the figure has no power to block a sale in Tally the way
a real credit-control system would; it exists purely so a breach can
eventually be surfaced as its own report (not yet built — see "Known gaps"
below).

## Continuous extraction — no calendar-grid week-snapping

Extraction runs on exactly the date range typed into Test & Save Extraction
or the CLI's own week-ending calculation — **never** snapped to a
Monday–Sunday calendar grid. An earlier build did snap every range to the
nearest full calendar week regardless of what was actually requested, and it
caused a real incident against live data: extracting "1st April" (the
client's actual go-live date) silently pulled in 30–31 March as well,
because that Monday-starting week included them. The fix (`ar_mis/config.py`,
`split_into_chunks`) treats "chunk into ~7-day pieces" as purely a practical
choice about Tally request size, completely independent from any calendar
boundary — a range is chunked starting exactly at its own first day, period.
`WeeklySnapshotRow.period_start` stores each run's own real start date
explicitly now, since it can no longer be derived by subtracting 6 days from
`week_ending` once chunks aren't fixed calendar weeks.

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

Branches come from Branch Master (`Store.list_branches()`), not from a
hardcoded list — add/edit them there before the first real run, same as for
a live webapp extraction.

## Known gaps

**Built and working:** every register/report described above, live-tested
against real TallyPrime (see the section below), bill-level ageing/due-date
detail (`BILLTYPE`, due date — the original "no ageing-bucket detail" gap
this section used to describe), Drift Findings + correction, Register
Exceptions Review, Catch Up a Party, search/filter/select-all across every
large table.

**Deliberately not built yet** — real internal-controls gaps identified and
discussed explicitly with the client, kept here rather than silently assumed
to be someone else's problem:

- **No real authentication.** Maker/Checker is a session toggle, not a login
  — every `reviewed_by`/`adjusted_by`/`incorporated_by` field in this system
  is a free-text box, not a verified identity. This is the gap everything
  else below depends on: an approval workflow or an audit trail is only as
  trustworthy as who's allowed to type into it.
- **Maker/Checker is a viewing permission, not an approval gate.** A Maker's
  action (an extraction, an adjustment, a catch-up) takes effect
  immediately; a Checker only ever reads it after the fact. No second
  signature is required on anything, including a write-off.
- **No bank-statement tie-out.** A Receipt voucher is trusted at Tally's own
  word; nothing here confirms it corresponds to a real bank credit.
- **No period lock.** Any period stays open to a correction forever — to be
  revisited after roughly 3 months of live operation, by the client's own
  call.
- **Concentration Risk is built; the rest of the AR-process control list
  agreed with the client is not yet**: targets/benchmarks with variance
  (DSO, Collection Efficiency, PTP Kept Rate are all reported as raw
  actuals, nothing to hold them against), per-owner accountability rollup
  (PTP already carries an `owner` field, nothing rolls performance up by
  it), an ageing-bucket-transition escalation trigger, dispute/hold
  classification distinct from plain "unpaid", and a due date on the
  Receipt/Journal register's free-text "Next Action" field.
- **Provisioning is explicitly out of scope for now** — ageing is shown, but
  nothing here computes what should actually be provisioned against it.

## Running tests

```
pip install -e .[dev]
pytest
```
