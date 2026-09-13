# Registers, Reporting & Deployment Design Discussion (v1 scope)

Living record of design conversations that haven't been implemented yet. This
is **not** a spec that's been built — nothing in this document exists in code
until it's explicitly moved into `AR_MIS_Automation_Spec.md` and built. Keep
this updated every session so no decision or open question depends on chat
history surviving. Confirmed items are things the client (Modi) has
explicitly agreed to; open items still need an answer before they can be
built.

## Confirmed decisions

### 1. Registers, not the weekly snapshot, are the base of reporting

The existing `weekly_snapshot` table (party-level weekly totals) is a
secondary, derivable view — not the foundation. The real base is a set of
voucher-wise / invoice-wise master registers (below). The weekly snapshot can
still be produced *from* the registers if useful, but reporting starts from
the registers.

### 2. Three master registers, combined across all branches, cumulative from FY start

Not one register per branch — one continuously growing register per category,
spanning every branch, appended to on every extraction run or manual upload.
Column shapes below were cross-checked in a follow-up session against the
client's actual current dashboard column headers; differences were resolved
one by one (see "Resolved in the column cross-check session" further down for
the reasoning behind each addition/removal).

**Sales & Debit Note Register** — one row per invoice (DNs included here
since both are receivables). Final columns:

| Column | Notes |
|---|---|
| Branch | |
| Invoice / Debit Note Date | |
| Note Type | Invoice or Debit Note — needed because this register combines both; not called out in the original design, added during the column cross-check |
| Voucher Number (InvoiceNo / DebitNote No) | The human-facing invoice/DN number, printed/sent to customer |
| Bill Allocation Reference ("New Ref") | The *actual* Tally field (`BILLALLOCATIONS.LIST`) receipts/CNs match against internally — see item 8. Confirmed as a genuinely new column: it does not exist in the client's current dashboard at all |
| Job ID / Shipment ID | Client-specific field, retained on client's explicit request; pass-through only, no computation keyed off it |
| Customer Name | |
| Grouping (Sundry Debtor / Related Party) | Set manually per party; see item 5 |
| Taxable Value | |
| CGST / SGST / IGST | Needs sample XML to confirm tax-ledger naming — see Open Items |
| Invoice Value | = Taxable Value + Tax |
| Due Date | = Invoice Date + that customer's Credit Period **in effect at invoice creation, computed and stored once** — a later Credit Period change on Customer Master never retroactively recalculates existing invoices' Due Dates (see item 6) |
| Linked CN No. | The CN number if exactly one CN against this invoice; "Multiple credit notes issued" if more than one |
| Linked CN Amount | Sum of all CNs against this invoice (rollup from CN Register) |
| Net Receivable | = Invoice Value − Linked CN Amount |
| Receipts Applied | Sum of all receipts applied against this invoice (rollup from Receipt & Journal Register) |
| Open Amount | = Net Receivable − Receipts Applied. (A Discount/Adjustment term was proposed and then explicitly dropped — Open Amount has no discount component) |
| Carried Forward Open Amount | A **frozen snapshot** of Open Amount taken at the moment of FY rollover — display/audit only, never an input back into the live Open Amount formula above |
| Overdue Flag (Y/N) | As-of-date relative — see new as-of-date decision below |
| DPD / Due Days | As-of-date relative; only meaningful when Open |
| Ageing Bucket | Derived from DPD, as-of-date relative |
| PTP Date | **Editable** — see item 7 |
| PTP Amount | **Editable** — see item 7 |
| PTP Status | Active / Kept / Broken — as-of-date relative (a PTP not yet due when viewed "as of" an earlier date must show Active even if it has since been broken by today's real date) |
| Next Action | **Editable**, part of the PTP workflow, same tier as PTP Date/Amount |
| Expected Collection Date | **Editable**, part of the PTP workflow, same tier as PTP Date/Amount |

Explicitly removed from the original proposal during the cross-check:
**Discount/Adjustment** (dropped entirely, not deferred — no longer part of
the Open Amount formula at all), **Collector** (removed for now), and
**Reference No. (BL No.)** (this was a Bill of Lading / shipping reference —
confirmed unrelated to the Bill Allocation Reference above; a naming
collision, not the same field).

**Credit Note Register** — voucher-wise, all branches combined, date-wise.
Columns: Branch, Date, Customer, CN Number, Original Invoice/DN Ref (matched
via Party + Bill Allocation Reference — the same composite-key principle as
item 8, not voucher number alone), CN Amount, Reason Code. No tax split
needed here (client's explicit instruction).

Confirmed this session: a Credit Note can exist **on account / unapplied**,
exactly like a receipt can. So the register also needs, mirroring the
Receipt & Journal Register:
- An Open/Unapplied CN Amount column (client's current dashboard already has
  `OpenCreditNotes` for this).
- **Classification** (Current / Pre-MIS Adjustment / Pending Review) — an
  unmatched CN "Against Ref" is the same pre-go-live exception problem as an
  unmatched receipt/journal (item 10), just from the credit side.
- Netting, not pair-matching, for unapplied CNs — same principle as item 9.

The exact final column names for the unapplied/classification fields on this
register are drafted by inference from the Receipt & Journal Register's
equivalent columns, not yet reviewed line-by-line with the client — **confirm
next session**.

Explicitly removed: `Match_Key` — an Excel-era manual lookup column,
unnecessary once the tool enforces Party + Bill Allocation Reference matching
itself.

**Receipt & Journal Register** — voucher-wise, all branches combined,
bill-allocated. Columns: Branch, Date, Voucher Type (as per books) / Voucher
Type (normalised), Voucher Number, Customer (from the receipt/journal entry
itself), Allocation Type (Against Ref / Unapplied), **Target Doc No.** — the
Bill Allocation Reference of the invoice this is tagged against (confirmed
this is the actual matching key, not the Voucher Number — this is the field
that implements item 8's fix), Applied Amount, Unapplied Flag / Unapplied
Balance, **Classification** (Current / Pre-MIS Adjustment / Pending Review —
see item 9 and item 10), DPD at Application / Age Unapplied Days, Invoice Fin
Year (the FY of the invoice being applied against, which can differ from the
receipt's own FY), Narration/Ref Text.

A voucher/journal can allocate against a specific invoice ("Agst Ref") or be
left unapplied ("New Ref" / on-account) — this applies to Journals exactly
like Receipts, not just Receipts (any voucher type can carry a bill
allocation if the ledger has bill-by-bill tracking on).

Explicitly removed: **ETA Application Date** (a mistaken duplicate of PTP
Date — no separate "unapplied cash follow-up date" concept exists), **Owner**
(removed for now), and **Allocated Party** (redundant — confirmed it can
never differ from Customer Name when a receipt is actually allocated,
since matching is party-scoped by construction; and when unapplied, that
state is already captured by Allocation Type / Target Doc No. being blank —
so it carries no information Customer Name + Allocation Type don't already
carry).

### 3. PTP stays inside the Sales & DN Register — not a separate editable register

PTP Date/Amount are edited directly against the relevant invoice row by the
AR team (who filter the register to Open invoices and fill them in). The PTP
view is a filtered extract of the Sales & DN Register, **not** a second data
store — confirmed independently this session: the client's own current PTP
register is column-for-column a subset of the Sales & DN Register (drops
Note Type, Job ID, Grouping, and the tax split; keeps everything else).

Confirmed shape of the filter: shows only rows where **both** PTP Date and
PTP Amount are populated — not all Open invoices ("otherwise there's no
point to a PTP register"). Does **not** carry the Grouping column.

### 4. Financial-year handling

- One continuous data store — not physically separate files per year.
- UI has a Financial Year selector (FY 25-26, FY 26-27, ...) to view that
  year's working set.
- At year-end, every invoice still carrying an Open Amount (partial or full)
  carries forward into the new FY **invoice-wise** — each keeps its own
  identity, original due date, and running ageing. Explicitly **not** a
  lump-sum "Opening Balance" collapse per customer (that would destroy
  per-invoice ageing accuracy for anything older than one FY).
- Rollover is triggered by a **deliberate button/confirmation**, not silently
  by the calendar date. Reason: late-arriving branch data (e.g. a branch's
  March vouchers extracted in April) could otherwise get carried forward
  incorrectly if rollover fires the instant the FY turns on the calendar.
- See the new as-of-date decision below for how this interacts with viewing
  a prior FY's position after rollover has happened.

### 5. New-party classification (Sundry Debtor vs Related Party)

- For the initial Pre-MIS seed, the client tags each party themselves.
- For any new party discovered afterward (auto-created at Pre-MIS
  Outstanding = 0, per existing behavior), the tool asks interactively at the
  point the report is generated — since the user is already sitting with the
  tool at that point. **Not blocking** — doesn't halt the whole run if
  unanswered, just surfaced clearly (client's explicit call).

### 6. Credit period lives on Customer Master, not a global constant

New column on Customer Master: `Credit Period (Days)`, default 30,
overridable per customer per branch. Due Date on every invoice = Invoice Date
+ that specific customer's credit period **as it stood at the moment the
invoice was created** — computed and stored once. Confirmed this session:
if the Credit Period is changed later, it applies only to invoices created
after the change; it never retroactively recalculates Due Date on invoices
already in the register.

### 7. Editable fields — general principle

**Extracted fields (from Tally) and computed fields must never be freely
editable.** This was pushed back on hard: making everything editable
recreates the exact defect class this tool exists to eliminate (bucket
sub-splits not summing to totals, Bad Debt Risk disagreeing with the Ageing
Matrix — the original six defects named in the top-level spec/README).

Fields that ARE legitimately editable, because there's no other source of
truth for them:
- PTP Date, PTP Amount, Next Action, Expected Collection Date (Sales & DN
  Register — all part of the PTP workflow, confirmed same editable tier)
- Sundry Debtor / Related Party classification
- Credit Period override (Customer Master)

Everything else (Taxable Value, CGST/SGST/IGST, Total Invoice Value, Open
Amount, Status, Due Days, Ageing Bucket, CN/Receipt rollups) stays
system-derived, never hand-typed.

### 8. Cross-party bill-reference collisions — root-cause fix, not detection

Real scenario from the client: an invoice for Party Y was created by
duplicating Party X's invoice, the Voucher Number was updated (1 → 2) but the
Bill Allocation Reference ("New Ref") was mistakenly left as "1" — the same
value already used under Party X's own ledger. Tally itself never sees this
as a conflict because bill references are scoped per-ledger internally.

**Fix:** matching a receipt/CN's "Against Ref" to an invoice must be done on
**(Party, Bill Allocation Reference)** as a composite key — using the actual
New Ref field from the XML (`BILLALLOCATIONS.LIST`), not the Voucher Number,
and not the reference number alone. This exactly mirrors what Tally does
internally and eliminates the whole class of cross-party reference collisions
by construction, rather than detecting them after the fact. Confirmed the
Sales & DN Register needs **both** Voucher Number (display) and Bill
Allocation Reference (actual matching key) as separate columns — see the
register table above. Confirmed this session: the Receipt & Journal
Register's `Target Doc No.` and the Credit Note Register's `Original
Invoice/DN Ref` are both meant to carry this same Bill Allocation Reference,
not the Voucher Number — this is the field that actually implements the fix.

**Important scoping note (confirmed in follow-up discussion):** New Ref must
never be used to identify or de-duplicate rows *within* the Sales & DN
Register itself — it is purely a stored lookup column, read only when a
receipt/CN needs to find which invoice to apply against. The register's own
row identity (deciding "this is a new invoice, append it" vs. "already
recorded, don't duplicate it" on re-extraction) is **Branch + Voucher Number
+ Party** (+ Date as a safety check) — never New Ref. Walked through the
worked example again to confirm: Mr. X's (Voucher 1, New Ref "1") and Mr.
Y's (Voucher 2, New Ref "1") are obviously two different rows under a
Voucher-Number-based identity key, so Mr. Y's invoice is never at risk of
being dropped as a "duplicate" of Mr. X's, even though they share a New Ref
value. New Ref colliding across parties is only ever a problem for the
matching direction (item 8's fix above), never for the register's own
row identity.

Known residual gap this can't fix: if a receipt is recorded against the
*wrong party's ledger entirely* by a genuine data-entry mistake, and that
wrong party happens to have their own real bill with the same reference, the
match will succeed and look internally consistent — because it is
consistent, just factually wrong about who paid. Not solvable from Tally's
own exported data; only catchable via normal reconciliation or a human
noticing.

### 9. Unapplied Receipts/Cash report — netting, not pair-matching

Scenario: an on-account (unapplied) receipt is later "corrected" via a
reversing journal (client's own workaround, forced by their own "no
backdated edits after 2 days" rule) rather than editing the original entry.
This creates two offsetting on-account line items in the data.

**Fix:** the Unapplied Receipts/Cash report shows a **net total per party**
(sum of all "New Ref" amounts), not voucher-wise. A receipt + its reversing
journal leg net to zero automatically through simple addition — no need to
detect and exclude a matched pair, which would be fragile (risk of false
matches on coincidental same-amount transactions). Full detail is still
visible in the Receipt & Journal register itself if anyone needs to trace
why a total is zero.

Confirmed this session: the **same netting principle applies to unapplied
Credit Notes** on the Credit Note Register — a CN left "on account" nets per
party the same way an unapplied receipt does.

### 10. Pre-MIS cleanup journals

Client's team sometimes creates reversing journals to bill-wise-tag old
pre-MIS-period invoices/receipts that were never tagged in Tally. These
journals land inside the normal extraction window and reference invoices
that predate go-live — invoices this system only ever captured as one
lump-sum Pre-MIS Outstanding baseline per party, never individually.

**Fix:**
- When a receipt/journal's "Against Ref" allocation doesn't match any
  invoice actually tracked in the Sales & DN Register, it's flagged in an
  **Exceptions list** for the user to classify: **Pre-MIS Adjustment** or
  **typographical error** (needs fixing in Tally). Never auto-classified
  silently — a typo could otherwise get silently misfiled as pre-MIS.
- Confirmed as **Pre-MIS Adjustment** → routed through the existing
  `record_pre_mis_adjustment()` mechanism (logged, reasoned correction to
  the party's Pre-MIS Outstanding baseline), not force-fit into the
  invoice-level register.
- **Voucher-level, not line-level:** if any leg of a journal is flagged
  Pre-MIS, the *entire* voucher — including its on-account/New-Ref leg —
  travels together and is excluded wholesale from the ordinary Unapplied
  Cash netting (item 9). Splitting it (one leg "Current", one leg "Pre-MIS")
  would recreate a phantom imbalance, since the on-account leg's true
  counterpart predates go-live and was never captured by this system.
- **Classification** column: Current / Pre-MIS Adjustment / Pending Review —
  set per voucher, all lines of that voucher share the same value. Confirmed
  this session: the same three-way classification is needed on the Credit
  Note Register too, for exactly the same reason (an unmatched CN reference
  can equally predate go-live).

### 11. Multi-user access, roles, and audit trail

The tool will be used by the client's team member initially, then handed to
the full AR team after a couple of months of success. This means **real
login/authentication with roles is required** — not a single-user toggle.
Two roles: AR team members (day-to-day register use, PTP entry, filtering)
and Administrator (override capability for genuinely unanticipated data
situations).

**Non-negotiable:** every administrator override is logged — who (which
logged-in user), when, which field, old value, new value, and ideally a
reason. A silent, untraceable override recreates the exact hole this system
exists to close. This is new scope beyond what exists today (currently zero
authentication — anyone with access to the machine has full access to
everything).

### 12. Deployment/packaging

Keep the existing local Flask web app architecture (SQLite + local server +
browser UI) — do not rebuild as a from-scratch native desktop app, and this
is explicitly *not* a hosted/internet-facing web app; "web application" here
only ever meant "local server, browser as the display surface," same as
today. Package it behind a one-click executable/launcher that starts the
local server and opens the browser automatically, so using it feels like
double-clicking a normal Windows program rather than typing terminal
commands.

### 13. Version updates and schema migrations (from the earlier update-safety discussion)

- `data/` already lives outside git (`.gitignore`), so a code update via zip
  cannot touch it *as long as delivery doesn't mean "delete and replace the
  whole project folder."* Data must be kept in a location a code swap can't
  reach.
- Schema changes must be additive-only (`ALTER TABLE ADD COLUMN`, new
  tables) via a version-sticker mechanism (e.g. `PRAGMA user_version` or a
  metadata table) — never a `DROP`/recreate.
- Confirmed: this schema-check + backup runs **fully automatically** the
  moment the app starts and detects a version mismatch — no separate button,
  no step to remember. (Contrast with item 4's year-end rollover, which
  *does* need a deliberate button — different risk profile: a schema
  migration is deterministic and safe to automate, a year-end rollover
  depends on data completeness that the calendar can't guarantee.)
- A backup of the database is taken automatically before any migration
  commits, regardless.

### 14. As-of-date / point-in-time reporting

Reports must always be generated for a **selected reporting date (or
range)**, independent of how much data has actually been loaded/extracted.
Example: data has been extracted through 12th Sept, but the user wants to
see the position as of 6th Sept — changing the reporting date must not
require touching or removing any data, and must not show anything dated
after the selected date.

**Mechanism:** every derived/aggregate field — Open Amount, the CN Amount and
Receipts Applied rollups, Overdue Flag, DPD, Ageing Bucket, PTP Status — is
computed by filtering the underlying registers to `transaction date ≤
selected as-of date` and re-deriving from that filtered slice. Nothing is
read from a precomputed "current" total. This is only possible because of
item 1's decision to base reporting on dated, voucher-level registers rather
than the old cumulative weekly-snapshot model; a snapshot has no way to be
rewound.

This also directly validates why Carried Forward Open Amount (item 2) must
be a frozen, non-formula snapshot rather than a live input: Open Amount stays
always freshly derived from date-filtered register data, which is what lets
an as-of query work identically whether or not an FY rollover has happened in
between.

- **Filtering is by transaction/voucher date, not by data-entry/extraction
  date.** A backdated entry keyed in later still correctly appears in an
  as-of report for its own (earlier) date. Practical consequence: the same
  "as of 6th Sept" report can, in principle, produce a different result if
  re-run after new backdated data lands. Accepted as-is — backdated entries
  are mostly prohibited within the company and thus rare in practice, so this
  isn't worth building a snapshot/locking feature for.
- **No report-locking/freeze feature.** Always live recompute; confirmed
  explicitly given how rare backdating is.
- **Due Date is frozen at invoice creation** (see item 6) — this matters more
  under as-of reporting than it otherwise would, since two as-of reports
  straddling a Credit Period change must never disagree about the same
  invoice's Due Date.
- **PTP Status is as-of-date relative**, not tied to today's real date — a
  PTP not yet due when viewed as of an earlier date shows Active even if it
  has since been broken by today's actual date.
- **Scoping across FY rollover:** the FY selector (item 4) and the as-of date
  are two independent, nested filters — the FY selector picks which year's
  invoice set is in view, and the as-of date filters the underlying
  transaction registers within that view. Viewing a prior FY's position after
  rollover works via the same general mechanism: select the prior FY first,
  then the as-of date within it. No special-case "reconstruct pre-rollover
  state while viewing the current FY" logic is needed or wanted — that's not
  expected to be a realistic user flow.

### 15. Report catalog — outputs the application must produce (v1)

Cross-checked against the client's current Excel-based reporting so nothing
already relied upon gets dropped. Important framing for this whole section:
these Excel reports are **not a spec to reconcile or preserve as-is** — they
are unreliable precisely because they're Excel, which is the entire reason
this application is being built. Inconsistencies between them (different
ageing-bucket granularities, ad hoc thresholds, manual formula-swapping for
"as of" views) are not open design questions to negotiate; they're the
problem statement. The app fixes all of them by construction, the same way
items 1, 7, and 14 already do: one register-derived source of truth, one
Ageing Bucket definition, one as-of-date mechanism. The list below is scope
(what must exist), not a spec to audit.

- **AR Snapshot (KPI dashboard).** Outstanding Position (Total AR, Open AR
  split by FY, Pre-MIS Outstanding, Unapplied Cash, Unapplied CN, Related
  Party AR shown separately, Rounding Difference), Performance (Overdue AR
  and its sub-bucket breakdown, Overdue %, Bad Debt Risk >180 Days, Notional
  Interest Cost, DSO, CEI, PTP Kept Rate), Trend Analysis (Sales Trend and
  Collection Trend — MIS Period vs Pre-MIS Period — last 4 months), Average
  Collection Period by branch. Every figure on this dashboard is as-of-date
  selectable per item 14 — confirmed this session that the Excel version's
  split between "as on last updated date" and "selected period" sections was
  itself just the manual Excel workaround for as-of reporting (changing an
  input cell, recalculating, then reverting the formula by hand). The app
  replaces that entirely; there is one as-of-date mechanism, not two report
  categories.
  - **Notional Interest Cost** — interest rate assumed flat at **10%** for
    now (confirmed this session), applied over the relevant overdue period.
- **Branch-wise Ageing Schedule** — one ageing-bucket row per branch plus a
  total row, all branches.
- **Ageing Matrix**, both a branch-level summary and a customer-level detail
  view (Branch, Customer, Grouping, ageing buckets, Total Open), FY-scoped.
  The customer-level view also ties each customer's register-derived Total
  Open against Tally's own ledger closing balance (likely sourced the same
  way as `fixtures/ledger_closing_balances.xml`) as an independent
  cross-check. The Excel version flagged a break using a ₹500 materiality
  tolerance — confirmed that threshold was an Excel-only convenience, not
  something carried into the app as a hardcoded rule. The "Business" column
  on this view is dropped for now (see Deferred).
- **Exception Register** — six sub-reports: Unapplied Cash (netted per
  party, per item 9), Unapplied CN (netted per party, per item 9 extended to
  CN as decided this session), rows where Total Open Amount is negative,
  Non-Active debtors (180+ days with no transaction but balance still open),
  "Against Ref" receipts/journals that don't match any invoice in the
  register (this is item 10's exception routing by another name), Top 20
  Overdue Customers.
- **Weekly Movement Register** — one row per week-ending date: Total AR,
  Open AR by FY, Pre-MIS Outstanding, Overdue AR and ageing buckets, DSO,
  CEI, Unapplied Cash, each with a week-over-week trend indicator. This is
  the concrete, working form of the `weekly_snapshot` table referenced in
  item 1 — a derived view produced from the registers, not a second data
  store. Whether each week's row is computed once and stored (so the trend
  reflects a genuine historical log) or fully recomputed live every time the
  report is opened is still an open question — see Open Items.

Ageing Bucket granularity was inconsistent across the client's existing
reports (some split 91–180 into 91-120/121-150/151-180, others use one
combined 91-180 bucket). Per item 7's single-source-of-truth principle, the
app computes Ageing Bucket exactly once and every report reads from that same
definition — which specific granularity to standardize on is a remaining
open item (see below), not a design question, since it doesn't change how
the value is computed, only how finely it's binned for display.

## Findings from real sample XML (client-provided, this session)

Client provided seven real exported files from a live company (Speedways
Logistics): Sales, Credit Note, Receipt, Journal, and Debit Note registers,
plus a Trial Balance (Sundry Debtors) export and a YTD voucher-wise detail
export. All seven were UTF-16LE-encoded (a real, generalizable fact about
Tally's manual "File → Export" output — separate from the live HTTP gateway,
which the earlier live-testing session confirmed responds in UTF-8). This
resolves former open item 1.

- **Tax ledger naming (item 2's CGST/SGST/IGST columns) — confirmed.** The
  actual tax ledgers are bare, exact-match `CGST`, `SGST`, `IGST` — no
  prefix/suffix variation found across an 8.7MB real Sales register (62
  distinct ledger names total). Important distinction found in the same
  data: many *revenue* ledgers are also named with a `_GST` suffix (e.g.
  `Road Transport Services_GST_18%`, `Handling Services_GST_INTER`,
  `Other Supporting Services_Non-GST`) — these are taxable/non-taxable
  income line items, **not** tax ledgers, and a naive "contains GST"
  substring match (the pattern already used elsewhere in this codebase for
  voucher-type categorization) would wrongly catch them. Tax-ledger
  identification must be **exact name match** against `CGST`/`SGST`/`IGST`,
  not substring. A `Round Off` ledger also appears on real invoices — a
  small rounding adjustment that is neither tax, party, nor revenue; needs
  a decision on which total it folds into (see Open Items).
- **Bill Allocation Reference matching (item 8) — confirmed correct.** In
  the real Credit Note sample, the voucher's own `REFERENCE` field and the
  `BILLALLOCATIONS.LIST`'s `NAME` (with `BILLTYPE=Agst Ref`) both carry the
  same value (`8569/HSLPL/25-26`) — the original invoice's Bill Allocation
  Reference. This is exactly the field item 8 already specified as the CN
  Register's "Original Invoice/DN Ref" column. No change needed.
- **A third `BILLTYPE` value exists: `Advance`** (in addition to the two
  already designed for, `Agst Ref` and `New Ref`) — found once in the real
  Receipt register, still carrying a bill reference `NAME` (money received
  ahead of an invoice being raised for that reference). Conclusion: no
  special-case handling needed — it flows through the same
  match-by-(Party, Bill Allocation Reference) logic as any other allocated
  reference. If the reference matches a tracked invoice, treat it as
  applied; if not (likely, since the invoice may not exist yet), it
  correctly lands in the Pending Review exception queue (item 10) like any
  other unmatched reference — the existing design already covers this
  without modification.
- **NEW FINDING requiring a decision — Tally already tracks a per-bill
  credit period.** Real `BILLALLOCATIONS.LIST` entries carry a
  `BILLCREDITPERIOD` field (e.g. `<BILLCREDITPERIOD P="30 Days">30
  Days</BILLCREDITPERIOD>`), tied to that specific bill. This was not known
  when item 6 (Credit Period lives on Customer Master, a manually-set
  default) was designed. Using Tally's own per-bill value instead of (or as
  an override to) a generic Customer Master default would be more accurate
  for any invoice whose terms were individually negotiated. **Needs a
  decision — see Open Items.**
- **CONFIRMED BUG — Manual Upload's parsing does not match what a real
  manual export produces.** The Trial Balance (`TBDebtors.xml`) and YTD
  detail (`YTDData.xml`) samples are in Tally's "Display Report" XML shape
  (`DSPACCNAME`/`DSPACCINFO`/`DSPCLAMT` for the trial balance;
  `DSPVCHDATE`/`DSPVCHLEDACCOUNT`/`DSPEXPLVCHNUMBER` for the YTD detail) —
  structurally nothing like the `LEDGER`/`CLOSINGBALANCE` or
  `TALLYMESSAGE`/`VOUCHER` shapes `parse_ledger_closing_balances()` and
  `parse_voucher_collection()` expect (confirmed by reading
  `ar_mis/manual_upload.py`, which calls exactly those two functions on
  these file uploads). This isn't a hypothetical edge case: Tally's UI
  "File → Export" simply cannot produce the gateway's Collection/Export
  Data XML shape at all — that shape only exists via the HTTP/ODBC gateway
  API. Manual Upload is specifically the fallback for when that gateway is
  unreachable, so the files a real user uploads through it are *guaranteed*
  to be in this Display Report shape. As built, Manual Upload would likely
  fail silently (parse to zero records, not crash) against real exported
  files rather than process them. **Needs fixing — see Open Items.**

## Open items — bring next session

1. ~~Sample XML files~~ — resolved, see Findings above.
2. **Validate the Pre-MIS Classification/exception logic** (item 10) against
   real data now that sample XML is available — now covers both the Receipt
   & Journal Register and the Credit Note Register.
3. ~~Final Ageing Bucket granularity~~ — **resolved: client's explicit
   scheme**, standardized across every report:
   `Current, 1-30, 31-60, 61-90, 91-120, 121-150, 151-180, 181+`
   (Current = within credit period; every band after that is days past
   the due date.) Implemented in `ar_mis.registers.compute_ageing_bucket`.
4. ~~Weekly Movement Register storage mechanism~~ — **resolved this
   session: append-only stored history**, not live recompute. Each week's
   row is written once and kept as-is, so the trend reflects genuine
   historical fact even if later data would compute a different result.
5. **Live Tally connectivity / hosting question — parked, not resolved.**
   Client's Tally setup may involve a third-party "Tally on Cloud" style
   host running multiple companies' Tally instances on shared infrastructure
   reachable over the public internet. Real security question (Tally's
   gateway has no authentication of its own) — client is checking the
   hosting arrangement themselves (dedicated VM per tenant vs. shared
   Windows Server session) before this is discussed further. See prior
   session's discussion for the full reasoning; nothing to build here yet,
   and this should not be assumed resolved just because it's parked.
6. **Credit Note Register's exact new column names** — the concept
   (unapplied CN balance + Classification, mirroring the Receipt & Journal
   Register) is confirmed, but the precise column set was drafted by
   inference this session rather than reviewed line-by-line with the client.
7. **NEW: Credit Period source of truth** — use Tally's own per-bill
   `BILLCREDITPERIOD` (found in real data, see Findings), the existing
   Customer-Master-default design (item 6), or the per-bill value as an
   override when present, falling back to the Customer Master default
   otherwise? Needs the client's decision.
8. **NEW: `Round Off` ledger handling** — confirmed present on real
   invoices (see Findings); decide whether it folds into Taxable Value,
   into Invoice Value only (after tax), or gets its own column.
9. ~~Manual Upload parsing bug~~ — **resolved**: `parse_ledger_closing_balances`
   now handles both the gateway Collection shape and Tally's real Display
   Report shape (DSPACCNAME/DSPACCINFO), verified against the real
   `fixtures/real_samples/TBDebtors.xml` (389 parties).
10. **NEW, resolved: Collection Efficiency (%) formula** — client confirmed
    the standard version: (opening balance at the start of the period + new
    sales during the period) compared against what was actually collected
    during the period, shown as a percentage.
11. **NEW, resolved: PTP Kept Rate formula** — client confirmed the standard
    version: of all promised-payment dates that have already passed (as of
    the selected reporting date), what percentage were marked Kept rather
    than Broken. A PTP whose promised date hasn't arrived yet is excluded
    entirely — it does not count toward the rate either way.
12. **NEW, still open: DSO (average days to collect payment) formula** —
    asked the client to choose between the two common versions (a 90-day
    trailing window vs. a 12-month trailing window) and the question wasn't
    clear as put; needs re-explaining in plain terms with a worked example,
    then a decision, before the KPI dashboard can compute this figure.

## Deferred to a later version (not rejected, not in scope now)

- **Operational collections workflow** — using the application for day-to-day
  AR team follow-up: logging follow-up activity/replies, scheduling
  re-follow-ups, and tracking legal action taken on an invoice/party.
  Client's explicit call: defer this rather than mix it into the current
  build. Reasoning discussed: this project has already grown substantially
  from its original "extract and reconcile" scope (registers, ageing, PTP,
  pre-MIS exceptions, multi-user roles) without yet validating any of it
  against a real Tally instance — adding a full collections/legal module on
  top now risks the reconciliation core never shipping. Nothing about
  deferring this requires re-architecting anything above; it would sit on
  top as another invoice-tied log, the same way PTP already does. When this
  is picked up later, still need answers to: does it replace an existing
  tool the AR team uses, or run alongside it; does "legal action" need only
  a status/date/notes field or real case tracking (counsel, hearings, case
  stage, documents); and is follow-up logged per invoice or per party (one
  call often covers several invoices).
- **Discount/Adjustment as a formula component of Open Amount** — dropped
  entirely for now during the column cross-check, not built as a hidden
  zero-value placeholder. If a real "management discount/write-down"
  requirement surfaces later, it needs its own design pass (editable-field
  status, audit trail per item 11) rather than being silently reintroduced.
- **Collector / Owner assignment columns** on the Sales & DN Register and the
  Receipt & Journal Register — removed for now; no per-invoice/per-voucher
  ownership tracking in v1.
- **"Business" column** on the customer-level Ageing Matrix / Customer
  Master — dropped for now.

## Explicit non-scope / rejected ideas

- **Making every field in every register editable** — rejected; see item 7.
  The real need behind this request turned out to be covered by the
  exception-routing (item 10) and party-scoped matching (item 8) work
  instead.
- **Auto-detecting and hiding matched offsetting transaction pairs** in the
  Unapplied Cash report — rejected in favor of simple netting (item 9);
  pair-detection is fragile and risks false matches.
- **Lump-sum year-end carry-forward** — rejected in favor of invoice-wise
  carry-forward (item 4).
- **`Match_Key` (Credit Note Register)** — an Excel-era manual lookup column;
  unnecessary now that the tool enforces Party + Bill Allocation Reference
  matching itself.
- **`Allocated Party` (Receipt & Journal Register)** — redundant with
  Customer Name + Allocation Type; carries no information neither of those
  already carries.
- **`ETA Application Date` (Receipt & Journal Register)** — a mistaken
  duplicate of PTP Date; no separate "unapplied cash follow-up date" concept
  exists.
- **`Reference No. (BL No.)` (Sales & DN Register)** — a Bill of Lading /
  shipping reference, unrelated to the Bill Allocation Reference ("New
  Ref"); removed to avoid the naming collision.
- **A report-locking/snapshot feature for as-of-date reporting** — rejected;
  live recompute is always acceptable given how rare backdated entries are
  in practice.
- **₹500 GL-variance materiality threshold** — this was an Excel-only
  tolerance for the customer-level AR-GL tie-out check; not adopted as a
  hardcoded app rule as-is.
