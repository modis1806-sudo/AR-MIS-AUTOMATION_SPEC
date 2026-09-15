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
the full AR team after a couple of months of success. This originally meant
**real login/authentication with roles is required** — not a single-user
toggle — with two roles: AR team members (day-to-day register use, PTP
entry, filtering) and Administrator (override capability for genuinely
unanticipated data situations), and a non-negotiable requirement that every
administrator override be logged (who, when, which field, old value, new
value, ideally a reason).

**Superseded — client's explicit, permanent decision:** password-based
login is deliberately left out of this project, full stop, not deferred as
future work. In its place: a two-role session picker with no password
behind it — **Maker** (full access: Extraction, Registers, Reports) and
**Checker** (Registers and Reports only, read-only, no Extraction) — a
rename of what shipped earlier this session as "Preparer/Viewer." Anyone
with access to the machine can still pick either role themselves; that is
an accepted consequence of this decision, not an oversight. This reverses
the "non-negotiable" real-login requirement above — recorded here rather
than silently dropped, per this document's own standard for anything a
prior decision changes. The Administrator-override/audit-trail concept
above was never built and remains out of scope; if a real override feature
is added later it still needs its own audit trail regardless of how login
is handled.

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
  store. **Resolved this session (see Open Items): append-only stored
  history, not live recompute.**
- **NEW this session — TB Reconciliation Cross-Check.** The client's
  explicit ask, directly answering "how does a Maker or Checker know the
  data is correct at all": exposes `weekly_snapshot` itself as its own
  visible sheet, one row per party per branch per week (full history,
  mismatches included — see Open Items 15's never-discard reversal), showing
  Tally's own TB/Sundry Debtors closing balance alongside this app's own
  workings (Opening + Sales + DN − CN + Receipts + Journals) and the exact
  difference, with a summary at the top: how many parties currently show
  an unresolved difference (their most recently recorded week only — a
  past mismatch since reconciled clean doesn't count) and the total
  absolute amount.
- **NEW this session — Branch Sales + CN + DN Total.** A simple, direct
  per-branch turnover total (Sales + Debit Notes − Credit Notes) for a
  chosen period, deliberately plain enough to eyeball against Tally's own
  P&L page. Gross/GST-inclusive by necessity (Credit Note Register rows
  carry one combined amount, never split into taxable value and tax) — a
  gap against a tax-exclusive Tally P&L view is the GST portion, not an
  error in this app's figures.

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
8. ~~`Round Off` ledger handling~~ — **resolved this session**: folds
   into Invoice Value after tax (never into Taxable Value, which must
   stay GST-clean), AND is kept as its own visible
   `SalesDNRegisterRow.round_off` column on the Sales & DN Register /
   its Excel export, so it's auditable in the TB Reconciliation
   Cross-Check sheet's workings rather than silently absorbed.
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
12. ~~DSO (average days to collect payment) formula~~ — **resolved**: Total
    Open AR (as of the selected reporting date) ÷ average daily sales over
    the trailing 90 days (i.e. Total Sales in the last 90 days ÷ 90).
    Client's explicit choice of the 90-day trailing window over a 12-month
    one, for faster reaction to recent changes in the business.
13. **NEW, resolved with an implementation note: PTP Kept vs Broken.**
    Client confirmed: a promise counts as Kept if AT LEAST the promised
    amount was paid, by the promised date — the invoice's other, unrelated
    balance being still open does not break that specific promise. This is
    always derived, never manually marked (there is no status field on
    InvoiceFollowUp - see item 7). Implementation note this raised: judging
    "was the promised amount paid" needs a *delta* — how much was collected
    on this invoice **since the promise was logged**, not the invoice's
    total-ever-collected figure (which would let an already-paid balance
    from before the promise falsely count toward it). This requires
    recording *when* a PTP was logged, which `InvoiceFollowUp` didn't
    previously carry — added a `logged_at` field for exactly this (a
    generically useful audit fact regardless, not a new open design
    question put back to the client).
14. **NEW, resolved: password-based login is permanently out of scope,
    superseding item 11.** Client's explicit call: no password, ever — the
    session-based role picker (renamed this session from Preparer/Viewer to
    **Maker/Checker**) is the permanent access-control mechanism, not a
    stopgap. See item 11 above for the full reasoning and what this
    reverses.
15. **NEW, resolved: reconciliation mismatches never block data from being
    written — reverses Section 4.1/2.2's original all-or-nothing halt.**
    Client's explicit instruction: "even if reconciliation does not work,
    the data must not be thrown away." Previously, one party's mismatch
    rejected the ENTIRE branch's data for that week (every other party
    included) and halted the whole weekly cycle (`EscalationRequired`,
    now removed). Now: every party's data is written regardless of that
    party's own reconciliation result — `ar_mis.pipeline.process_branch_data`
    always returns PASS, reporting mismatched parties via
    `BranchRunOutcome.failed_parties`. `ar_mis.gate.evaluate_output_gate`
    is now the sole mechanism that turns a mismatch into "not clean" (the
    CLI's weekly report is still stamped UNVALIDATED and held for manual
    sign-off — that part is unchanged, only the halt is gone). A party's
    NEXT week now rolls forward from Tally's own stated closing balance
    (`closing_extracted`), not this app's own workings
    (`closing_computed`) — client's explicit choice, so an unresolved gap
    is a contained, visible flag for that one week rather than something
    that silently compounds forward. See the new TB Reconciliation
    Cross-Check sheet (item 15's report catalog) for where a mismatch is
    actually surfaced to a human.
16. **NEW, resolved: the webapp had no live-Tally commit action at all —
    found and fixed this session.** "Test Extraction" is, and remains,
    diagnostic-only (confirmed connectivity/counts, writes nothing); the
    only path that ever wrote data through the browser was Manual Upload
    (file-based). Added a genuine "Extract & Save This Week" route,
    built the same way Manual Upload is (same `process_branch_data` call,
    same conflict rule, same Section 4.2 YTD drift check), sourced from a
    live Tally pull instead of an uploaded file.
17. **NEW, resolved: the Section 4.2 YTD drift check (backdated-entry
    detection) was missing from the live extraction path.** It was wired
    into the CLI batch path and Manual Upload, but never into the webapp
    - meaning the one ingestion path used day-to-day had no defense
    against backdated/edited vouchers at all. Fixed as part of item 16's
    new route.
18. **NEW, resolved (backdated entries only - bill-misallocation still
    deferred): a controlled, audited correction mechanism for a YTD drift
    finding.** Once item 19's drift-finding persistence surfaces a real
    backdated/edited voucher, a Maker can now "incorporate" it
    (`ar_mis.drift_correction.incorporate_drift_finding`, wired to
    `POST /reports/drift-findings/<id>/incorporate`) - deliberately NOT an
    edit to any existing, already-locked `weekly_snapshot` row, which
    would break this codebase's append-only principle. Instead the
    finding's full original voucher (persisted whole for exactly this -
    see `DriftFinding.voucher`) is replayed through the same
    `process_branch_data` pipeline every other extraction uses, under a
    NEW week_ending the Maker picks, keeping the voucher's own real
    historical date for ageing/DSO - the same "prior period adjustment"
    pattern real accounting uses: post the correction now, referencing
    what it corrects, rather than reopening a closed period. Requires
    that one party's real, Tally-sourced Sundry Debtors closing balance
    as of the correction week, typed in by the Maker - same trust level
    as Manual Upload's own Trial Balance file, never invented - because
    reconciliation is not suspended for a correction; a wrong figure
    doesn't get silently accepted, it just shows up as a fresh mismatch
    on the TB Cross-Check sheet. `incorporated` (the fix) and
    `acknowledged` (item 19's "someone has seen it" note) are two
    independent, never-conflated signals on the same finding. Maker-only,
    matching every other write action in this app. Verified end-to-end
    against real fixture data: the corrected invoice appears on the
    Sales & DN Register under its true original date, and the party's
    correction-week row reconciles clean on the TB Cross-Check sheet.
    Bill-misallocation correction (a receipt pointed at the wrong
    invoice, surfaced via Negative Open Amount) is a different fix -
    editing an existing row's reference, not inserting a missing voucher
    - and remains explicitly out of scope for a future round.
19. **NEW, resolved: drift findings are now persisted, not just shown
    once.** A finding used to appear only on the result page of the run
    that found it (Manual Upload or Extract & Save) and vanish once that
    page was gone. Now every finding, from every path (CLI, Manual
    Upload, Extract & Save alike), is written to a new `drift_finding`
    table and stays visible on its own report page until a Maker
    explicitly acknowledges it. A still-unresolved finding re-detected on
    a later extraction does not spawn a duplicate row (the table's own
    UNIQUE constraint on the finding's identity handles this). Explicitly
    NOT the correction mechanism built in item 18: acknowledging is an
    audit note only - who saw it and when - it never touches
    weekly_snapshot or any register.
20. **NEW, resolved: TB Cross-Check gained a From/To range and a "Total
    Debtor as per Books" tile.** Client's explicit ask: a single running
    total to hold up against a real Trial Balance run in Tally for a
    chosen date. The tile always uses each party's latest recorded week
    ON OR BEFORE the selected date across FULL history, never the
    display range - a party untouched again inside a narrow browsing
    window still owes their last known balance, and scoping the total to
    that window would silently understate it. `compute_tb_cross_check_
    summary` gained an `as_of` parameter for this
    (`ar_mis/reconciliation_report.py`).
21. **NEW, resolved: real Sundry Debtors closing-balance date-scoping bug,
    found live against the client's own Tally.** A raw `<FETCH>` on a
    Ledger Collection returns a cached object attribute that completely
    ignores `SVFROMDATE`/`SVTODATE` - confirmed by pulling the same real
    ledger for two genuinely different dates and getting the identical
    figure both times. Four request shapes were tried and disproven live
    before the fix (client-supplied, cross-checked independently via
    ChatGPT): keep the same ledger selection (`CHILDOF`/`BELONGSTO`
    Sundry Debtors, separately confirmed correct throughout) but replace
    `<FETCH>` with `<NATIVEMETHOD>` per field, which genuinely invokes
    the object's own computation against the period context rather than
    serializing a cached value. See `xml_requests.ytd_sundry_debtors_
    request`'s own docstring for the full v1-v5 trail - required reading
    before touching this function again, since the wrong-looking v1 shape
    is the one that looks simplest. One knock-on fix: a NATIVEMETHOD
    field renders in the exact case given in the request (`ClosingBalance`)
    rather than FETCH's always-uppercase (`CLOSINGBALANCE`) -
    `parsers.parse_ledger_closing_balances` was hardened with a
    case-insensitive lookup to handle both shapes, since Manual Upload's
    real fixtures still use the all-caps form.
22. **NEW, resolved: Monday-Sunday week-snapping removed entirely -
    client's explicit reversal, prompted by a real incident.** Extracting
    "1st April" (the actual go-live date) silently pulled in 30-31 March
    too, because the old code snapped every requested range to its
    containing calendar week regardless of what was typed. Client's own
    reasoning: this tool's data must be continuous from whatever date is
    fed into the register - the only genuine "weekly" requirements are
    extraction cadence (an operational choice, not a data-model
    constraint) and the Weekly Movement Register (already its own
    separately-recorded, independently-computed history per item 4).
    Neither requires the underlying extraction to snap to a calendar
    grid. Fix: `ar_mis.config.split_into_chunks` treats "~7-day chunks"
    as purely a practical choice about Tally request size, starting
    exactly at the requested range's own first day - never a calendar
    boundary. `WeeklySnapshotRow` gained `period_start` to store each
    run's own real start date explicitly, since it can no longer be
    derived from `week_ending` once chunks aren't fixed weeks; existing
    rows were safely backfilled by the old formula (`week_ending - 6
    days`), since every row up to that point genuinely was saved under
    the old fixed-week assumption. Live re-tested against the client's
    real Tally after the fix: "2026-04-01 to 2026-04-02" no longer pulls
    in March.
23. **NEW, resolved: ledger-name-stripping inconsistency (RAASHI
    ENTERPRISES).** A real ledger's NAME attribute carried embedded CRLF
    characters inside Tally itself. Voucher-side parsing already stripped
    names via `_text()`; the ledger-list-side `parse_ledger_closing_
    balances` didn't. Client's first instinct was "stop stripping
    everywhere" (in case stripping loses real data); corrected to
    "strip consistently everywhere instead" - the CRLF garbage carries no
    accounting data, only a matching-key string, and removing stripping
    broadly would reopen a previously-fixed crash (a whitespace-only
    `<AMOUNT>` tag).
24. **NEW, resolved: Pre-MIS Outstanding webapp upload, and a `party_id`
    design mistake caught and fixed same-session.** Client's ask: the
    one-time Pre-MIS Outstanding load should be offered right on Branch
    Master's create/edit form as a CSV upload, not only via the CLI tool.
    First version mirrored the CLI's own CSV shape
    (`party_id,party_name,pre_mis_outstanding`) - client immediately
    caught that this was wrong: `pipeline.process_branch_data` always
    uses the raw Tally ledger name as BOTH `party_id` and `party_name`
    for any auto-discovered party, so a separately-typed `party_id` here
    could mismatch the real ledger name and silently orphan the seeded
    balance forever (the exact failure mode this feature exists to
    prevent). Fixed: the webapp-scoped upload
    (`seed.parse_seed_rows_for_branch`) has no `party_id` column at all -
    `party_name` alone is the join key, and must be typed exactly as the
    ledger appears in Tally. The CLI's own multi-branch CSV format is
    unchanged (kept for backward compatibility with existing seed files).
    A second real gap surfaced during live testing right after: the
    webapp upload always called `seed()` with `force=False`, so a party
    auto-created at a placeholder 0.00 by an earlier extraction (exactly
    the client's real Charze scenario) could never be corrected through
    the browser at all - `has_weekly_snapshots()` already protects the
    one case that actually matters (a party with real extraction history)
    regardless of `force`, so the webapp upload now always passes
    `force=True`, closing the gap without weakening that protection.
25. **NEW, resolved: Export Unreconciled Parties.** Client's ask,
    following up on item 20's summary tile: an "Export unreconciled
    parties" action next to the "Parties currently showing a difference"
    tile, rather than scrolling the full TB Cross-Check table by eye.
    `reconciliation_report.compute_unreconciled_parties` shares the same
    latest-per-party-as-of reduction as the summary tile (so the export
    can never disagree with the count shown), sorted by absolute
    difference descending.
26. **NEW, resolved: search, filter, live totals, and row selection
    across every large register/report table.** Client's explicit ask,
    driven by a real usability wall: 964 real ledgers made a plain list
    (Customer Master) unusable. One shared, dependency-free
    `ar_mis/webapp/static/table_tools.js`, applied via `data-tt-*`
    attributes: live search across the whole row; column filter
    dropdowns auto-populated from values actually present; a "SUMIFS-
    style" live total that recomputes from currently visible rows only
    (kept as a clearly separate line near the table, never repurposing
    TB Cross-Check's own top KPI tiles, which are deliberately as-of-a-
    date across full history per item 20 and must never be conflated
    with what's scrolled into view); checkbox-per-row selection with
    select-all-visible (Gmail-style: filter first, then select - the
    client's own reference point), laying groundwork for batch actions
    not yet wired to anything (see Open Items below). Verified beyond
    markup-presence tests: the JS Indian-number-grouping reimplementation
    checked byte-for-byte against Python's own `format_inr`, and the full
    search/filter/live-sum/select-all interaction path exercised against
    a real DOM via jsdom.
27. **NEW, resolved: Catch Up a Party - the general mechanism for a
    misclassified or never-tracked debtor.** Client's real example: a
    Sundry Debtor mistakenly filed as a Sundry Creditor in Tally never
    gets its opening balance or its vouchers (sales, receipts, a Bad Debt
    write-off Journal) pulled by the normal weekly extraction at all.
    Design arrived at only after two rejected shapes: (a) a quick form
    typing in a corrected closing balance - rejected by the client, since
    it only patches the TB total and leaves the real registers (Sales,
    Receipt) wrong for however long the party was excluded; (b) a
    per-voucher-type "quick form" (opening + closing balance) -
    recognized as still too narrow once the client posed a second example
    (a Bad Debt write-off Journal, not a Sales/Receipt voucher) that the
    same shape couldn't handle without new code per voucher type. The
    actual fix: don't invent new math per exception - pull the party's
    real voucher history since an operator-supplied anchor balance/date
    and replay it through the exact same `process_branch_data` every
    normal week already uses, so any mix of voucher types is handled by
    the one general mechanism that already understands all five, not a
    new one invented per case. Refuses if the party already has
    weekly_snapshot history (onboarding only, not a correction path for
    an already-tracked party) or if Tally's Sundry Debtors pull still
    doesn't show the party (classification not actually fixed yet, or a
    name mismatch). Replays ONLY vouchers touching the target party, not
    the full company-wide pull for that date range - verified by a test
    that plants another party's own already-recorded register row in the
    same historical window and confirms it survives untouched (the real
    risk: replaying the unfiltered pull would duplicate every other
    party's rows for that window).
28. **NEW, resolved: Register Exceptions Review - the same
    persistence gap as item 19, for a different exception type.**
    `registers.RegisterBuildExceptions.unattributable_party` ("vouchers
    not added to a register") was shown once on the result page of the
    run that found it, then gone - exactly what item 19 fixed for drift
    findings. Widened the exception tuple from `(voucher_number, reason)`
    to `(voucher_number, party_ledger_name, reason)` throughout
    `registers.py`, `pipeline.py`, `orchestration.py`, and both result-
    page templates, using the voucher's own PARTYLEDGERNAME hint where
    one exists - needed so a reviewer knows WHICH party an exception is
    about, not just which voucher number. New `register_build_exception`
    table, populated by every SAVE run (live extraction and Manual
    Upload both share `process_branch_data`, so both get this for free).
    Two dispositions, mirroring item 19's acknowledge/incorporate split:
    "Reviewed - no action needed" (a human confirmed Tally's exclusion is
    genuinely correct - audit note only) or "Resolved via catch-up"
    (links straight into item 27's tool with the party name pre-filled;
    only records that the call was made, the actual fix is that separate
    run).
29. **NEW, resolved: Credit Limit.** Client's explicit ask: an editable
    `credit_limit` column on Customer Master, defaulting to Rs
    1,00,00,000 (one crore). Purely a reference figure in this secondary
    reporting layer - it has no power to block a sale in Tally, the
    actual system of record, and does not yet feed any breach report
    (see AR internal controls discussion below).
30. **NEW: AR internal controls discussion - what makes this a control
    tool rather than "an overpriced ageing system," client's own framing.**
    Client asked directly what's missing to call this a genuine AR
    internal-controls tool. First pass (identity/authentication, a real
    Maker-proposes/Checker-approves gate instead of a viewing-permission
    split, monetary approval thresholds especially on write-offs, bank-
    statement tie-out for receipts, period locking, encryption/DR for the
    SQLite file) was correctly pushed back on by the client as "usual
    data control measures, not AR controls" - important reframing given
    this is explicitly a SECONDARY reporting layer sitting on Tally, not
    the system of record, so IT general controls matter less here than
    whether the AR function itself is measurably performing and
    accountable. Client's own follow-up question ("our YTD pull will do
    just that", re: whether an ALTERED-not-just-new voucher would be
    caught) was verified, not just accepted: `resolve_opening_balances`
    anchors each week's opening to the PRIOR week's own recorded
    `closing_extracted` (Tally's real stated figure at that point in
    time), so any later alteration to an already-reconciled voucher
    necessarily produces a fresh mismatch on the next cycle's TB
    Cross-Check - confirmed as a genuine, already-working detective
    control, not a gap. Second pass, the AR-process/performance category
    the client asked for specifically, agreed and sequenced one at a
    time rather than built all at once: **Concentration Risk (item 31,
    built)**, then pending in order: accountability by owner (PTP's
    existing `owner` field rolled up into a performance view), targets/
    benchmarks with variance against DSO/Collection Efficiency/PTP Kept
    Rate, an ageing-bucket-transition escalation trigger, dispute/hold
    classification distinct from plain "unpaid", a due date on the
    Receipt/Journal register's free-text "Next Action" field (check
    against the existing `expected_collection_date` first - may already
    do part of this job). Provisioning (tying ageing buckets to an actual
    accounting provision policy) was raised and explicitly deferred by
    the client, not rejected - see Deferred section below. Period locking
    (item 6 of the first-pass list) was explicitly deferred by the client
    to be revisited after roughly 3 months of live operation, not
    rejected either.
31. **NEW, resolved: AR Concentration Risk.** First of the sequenced
    AR-process controls from item 30. Is receivables exposure spread
    across many customers or concentrated in a handful of them - a risk
    in its own right, independent of whether any of them are currently
    overdue. Top 5/10/20 parties by current outstanding, each as a % of
    total AR, plus a combined "top N = X% of Total AR" headline
    (`reconciliation_report.compute_concentration_risk`). Deliberately
    reuses the exact same latest-per-party-as-of reduction and the same
    `closing_extracted` figure as item 20's "Total Debtor as per Books"
    tile, so the two screens' Total AR can never quietly disagree.
    Ranked by each party's own signed outstanding (Dr positive/Cr
    negative) descending, so a credit-balance party sorts to the bottom
    rather than inflating anyone's concentration figure - caught and
    fixed a real test-data sign-convention mistake while verifying this
    (test data used Tally's raw sign instead of this app's own post-flip
    convention; the feature code itself was correct throughout).

32. **NEW, resolved: TB Cross-Check's live total was summing the wrong
    column, plus a full-report export and a Catch Up a Party name
    safeguard - closing out this thread, client's own call, "for the
    time being."** Three fixes verified together in one live session
    against real TallyPrime, not just unit tests:
    - The table's live "SUMIFS-style" total (item 26) was wired to
      Difference - 0 for every reconciled row, so filtering to a subset
      of parties and reading the total told you nothing. Every other
      screen sums its own core balance column (Open Amount, CN Amount,
      Total Open); TB Cross-Check now also sums Closing (TB/Tally), the
      actual debtor balance, alongside the existing Difference total.
    - The only export on this screen was the narrow unreconciled-
      parties list (item 25). Added a second, separate "Export to
      Excel" for the complete table for the current From/To range -
      reconciled and mismatched rows alike - so a Maker can filter/pivot
      it themselves rather than being limited to the on-screen
      search/filter.
    - Catch Up a Party's (item 27) Party Name field was free text with
      only a placeholder hint - risky precisely because it's the
      identity everything the tool builds gets keyed to, and this app
      already has one real near-duplicate-ledger case in production
      data (AARTH ELECTRICALS vs AARTH ELECTRICALS (GSTIN)). Now
      suggests the live Sundry Debtors ledger list from Tally (the same
      names the route already validated against on submit) as the
      operator types.
    - **Live-verified end to end**, not just against test fixtures: a
      real party (Kolkata branch) mistakenly filed as a Sundry Creditor
      in Tally, invisible to every normal weekly extraction, was fixed
      in Tally and run through Catch Up a Party - real voucher history
      pulled, TB Cross-Check reconciled clean, registers populated.
      Confirms the item 27 design (never a hand-typed closing figure)
      holds up against a genuine misclassification, not only the
      fixture the tests describe.

    **Explicitly not resolved by this entry** - still open, tracked
    separately in `docs/ar_controls_tracker.md`: Catch Up a Party has no
    `performed_by`/reason field, unlike every other correction mechanism
    in this app; extending it to an already-tracked party (today it
    refuses outright) is an unresolved design fork between gap-fill-only
    and a logged correction row; the Add Party "quick form" idea
    discussed this session (typing both an Opening and a Closing figure)
    directly conflicts with item 27's own rejected-shape (b) above and
    was not adopted; and the THE outstanding item (controlled batch
    actions on selected rows) remains entirely unbuilt. TB Cross-Check
    itself, and the Catch Up a Party correction path, are considered
    functionally complete for now - revisit only if something surfaces
    in further use, not on a schedule.

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
- **Period locking** (item 30) — client's explicit call: revisit after
  roughly 3 months of live operation, not rejected. Until then every
  period stays open to a correction indefinitely.
- **Provisioning tied to ageing** (item 30) — computing what should
  actually be provisioned against each ageing bucket per an adopted
  policy, rather than just showing the ageing itself. Explicitly deferred
  by the client alongside the other AR-process controls (item 30), not
  rejected.
- **The rest of the AR-process control sequence from item 30, pending in
  order**: accountability-by-owner rollup, DSO/Collection Efficiency/PTP
  Kept Rate targets with variance, an ageing-bucket-transition escalation
  trigger, dispute/hold classification, a due date on the Receipt/Journal
  register's "Next Action" field.
- **Real authentication, an approval gate (Maker proposes/Checker
  approves) instead of a viewing-permission split, and monetary approval
  thresholds** (item 30's first-pass list) — real gaps, correctly
  reframed by the client as IT/data governance rather than AR-specific
  controls, and lower priority than the AR-process sequence above for
  that reason; not rejected, just not next.

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
