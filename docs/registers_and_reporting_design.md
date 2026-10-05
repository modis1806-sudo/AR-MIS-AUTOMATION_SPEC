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

**Superseded again, later session — client's explicit call:** every
PTP-follow-up save and CN/Receipt reclassify action used to additionally
ask the preparer to type their own name into a free-text "Your name" box
per edit. With no real login behind the Maker/Checker picker (immediately
above), that box verified nothing — it was friction, not an audit control.
Removed entirely; every such edit now attributes to a fixed `"Maker"`
literal (`ar_mis.webapp.app.MAKER_ATTRIBUTION`), and a genuine
`InvoiceFollowUp.updated_at` timestamp (schema v13, stamped on every save
regardless of which field changed) carries the "when" half of the audit
signal alone — "who" was never a real signal here to begin with, only
"when" was. This does not touch the older, separate Register Exceptions
Review log (item 28), which still records a typed reviewer name — that
feature predates this call and was out of scope for it.

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
- **Ageing Matrix**, both a branch-level summary (one ageing-bucket row per
  branch plus a total row, all branches — this was shipped first as its own
  separate "Branch-wise Ageing Schedule" screen, then removed as a later,
  client-caught duplicate once Ageing Matrix's own branch summary, reusing
  the identical function, made it redundant; see item 38) and a customer-
  level detail view (Branch, Customer, Grouping, ageing buckets, Total
  Open), FY-scoped.
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
   `Current, 1-30, 31-60, 61-90, 91-120, 121-150, 151-180, 181+, Closed`
   (Current = within credit period, not yet due; every band after that is
   days past the due date; Closed = fully settled - client's own later
   catch, added so a paid-off invoice stops reading identically to one
   simply not yet due, which both used to show as bare "Current".)
   Implemented as `ar_mis.registers.AGEING_BUCKET_ORDER` (the full list,
   shared by every report that lists or totals buckets) and
   `ar_mis.registers.compute_ageing_bucket` (the overdue-progression math
   only - `compute_invoice_position` applies the Closed override on top,
   whenever Open Amount is zero or negative).
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

33. **NEW, resolved: a genuinely on-account CN/Receipt sitting unapplied
    for months read identically to a fresh one - "Unapplied Ageing"
    buckets this separately from invoice ageing.** Client's live-testing
    catch: a CN unapplied 181 days still showed the bare word "Current"
    on the Credit Note and Receipt & Journal registers, the same label a
    same-day CN gets. Bucketed through a new, deliberately SEPARATE
    scheme from item 3's own `AGEING_BUCKET_ORDER`:
    `ar_mis.registers.UNAPPLIED_AGEING_BUCKET_ORDER` /
    `compute_unapplied_ageing_bucket` -
    `0-30, 31-60, 61-90, 91-120, 121-150, 151-180, 181+`, with **no
    "Current" bucket at all**. Reusing item 3's own "Current" label here
    was tried first and rejected by the client: it collided with the
    Classification column's own, unrelated "Current" meaning (resolved-
    vs-pending, not ageing) sitting right next to it on the same row,
    making the two columns look like they said the same thing. The raw
    "Age Unapplied Days" count that used to sit alongside this bucket was
    dropped from both registers and their Excel exports - client's own
    call, once the bucket exists showing the exact day count too is
    redundant (the day count is still computed internally to derive the
    bucket, just no longer rendered as its own column).
34. **NEW, resolved: the CN/Receipt Resolve action's free-text "Reason"
    box was redundant - the chosen classification already states it.**
    Client's own words: "If it is Pre-MIS it is the reason, if there is
    data entry change, it is in itself the reason." Removed from both the
    single-row and batch Resolve forms on both registers; the stored
    `reason` column (still NOT NULL) is now auto-derived from
    `target_classification` via `app._RESOLUTION_REASON_BY_TARGET`
    ("Pre-MIS Adjustment" / "Data-entry correction") rather than typed.
    Trade-off flagged to the client and accepted: this loses the ability
    to record which SPECIFIC old invoice a Pre-MIS CN relates back to -
    acceptable because that detail was never surfaced anywhere in the UI
    to begin with, only ever written to a column nobody reads.
35. **NEW, resolved: a wrongly-resolved Pre-MIS Adjustment had no way
    back - added a Revert control.** Found live: a batch Resolve scoped
    by the free-text search (which matches a voucher's own number, not
    just the field the client meant to filter by - item 37 below) swept
    an unintended voucher into a Pre-MIS Adjustment, permanently reducing
    a party's real Pre-MIS Outstanding balance with no correction path
    anywhere in the app - Resolve only ever appears on a row that's
    currently Pending Review. `credit_note_revert_to_pending_review` /
    `receipt_journal_revert_to_pending_review` flip the row back to
    Pending Review and reverse the exact balance move with its own
    explicit, logged, opposite-sign `PreMisAdjustment` entry - never a
    silent rewrite of the original, same philosophy as
    `record_pre_mis_adjustment`'s own docstring (item 10). Safe by
    construction: PRE_MIS_ADJUSTMENT is never auto-assigned by the
    register-building pipeline, only ever reached via this human Resolve
    action, so any row showing it is always a legitimate revert target.
    Deliberately NOT covered yet: reverting a wrong "Current" (data-entry
    correction) resolution - neither register currently stores a signal
    distinguishing "manually resolved to Current" from "was always
    Current" (the Receipt & Journal row has no `reason` column at all),
    so a safe revert there needs a small schema addition first.
36. **NEW, resolved: Grouping column was missing from the Credit Note and
    Receipt & Journal registers.** Client's own catch - the Sales & DN
    Register has always shown and filtered by each customer's Grouping
    (item 5), but the other two registers never got it, despite every
    register being keyed off the same `(party_id, branch_id)` customer
    master record. Added the same lookup, filterable column, and Excel
    export column to both, in the same position (right after Customer)
    as Sales & DN.
37. **NEW, resolved: column-scoped search and a batch-action
    confirmation step, on all three registers.** Root cause of item 35's
    incident: the search box legitimately matches the WHOLE row (its own
    placeholder says "Search customer, voucher no..."), so scoping a
    batch Resolve by typing "25-26" to mean "target invoices from that
    FY" also matched a voucher whose OWN number happened to carry the
    same digits - confirmed by direct reproduction, not guesswork. Two
    changes, both generic in `table_tools.js` (no per-register logic):
    - `data-tt-search-scope`: an optional `<select>`, auto-populated with
      one option per table column by its header label (by cell position,
      so it works whether or not that column separately carries a
      `data-tt-col`), that pins the existing search box to a single
      column instead of the whole row - Excel's own "search this column"
      behavior, without a text box per column cluttering an already-wide
      register header (client's own first proposal, declined in favor of
      this: 15+ input boxes in one header row was judged worse to use
      than what existed, and still wouldn't have ruled out every wrong-
      selection scenario on its own).
    - Any `<form>` a `[data-tt-row-select]` checkbox targets (every
      batch-action form on all three registers) now shows a confirmation
      dialog before submitting - every selected row (by its own
      `[data-tt-row-label]` cell) and every non-blank field about to be
      applied, require an explicit OK. This is the real safety net:
      it catches a wrong selection regardless of how it happened (a
      loose search match, a leftover checked box, a misclick), which
      column-scoped search reduces but can't fully rule out by itself.
    Both live-verified end to end by reproducing item 35's exact incident
    on both the Credit Note and Receipt & Journal registers: whole-row
    search for "25-26" still matches both the intended and the
    unintended voucher; scoping to the actual reference column narrows
    correctly to just the intended one; and submitting the batch form
    with both wrongly selected shows a dialog listing both voucher
    numbers and the field being applied, blocks on Cancel, proceeds on
    Accept.
38. **NEW, resolved: removed the standalone Branch-wise Ageing Schedule
    report - an exact duplicate of Ageing Matrix's own Branch Summary.**
    Client's own catch, manually testing the Reports section: both
    screens showed the identical branch-level ageing breakdown, because
    both always called `ar_mis.dashboard.compute_branch_ageing_schedule`
    - Ageing Matrix's own docstring already said as much (item 33).
    Ageing Matrix is a strict superset (same branch summary, plus
    customer-level detail, the Tally cross-check, an FY filter, and
    search/grouping filters the standalone page never had), so the
    standalone page added nothing. Removed entirely: its route
    (`/reports/branch-ageing`), its template, its card on the Reports
    home page, and its tests. `compute_branch_ageing_schedule` itself is
    untouched and still runs exactly as before - only the dedicated page
    exposing it on its own is gone; it's reached only through Ageing
    Matrix now. Superseded item 15's report-catalog listing above, and
    item 31 ("Build Branch-wise Ageing Schedule report screen") is
    recorded here as reversed, not silently dropped.

    Same session, same screen, two more client-driven UI fixes: the
    Reports home page's 10 cards were stacked one full-width card per
    row, forcing a long scroll to see them all - now a `.report-grid`
    (same `auto-fill`/`minmax` CSS approach the KPI tile grid already
    used) lands at 3+ cards per row, each card's button anchored to the
    bottom via flexbox regardless of description length. That grid was
    also still capped at the 960px default `<main>` width while every
    register screen already used the roomier 1600px `main.wide` - client
    caught the same page looking needlessly cramped right after the grid
    shipped, so Reports now opts into `main.wide` too, the same as the
    three master registers.

39. **NEW, resolved: TB Reconciliation Cross-Check and Branch P&L
    Cross-Check (renamed from "Branch Sales + CN + DN Total") reclassified
    as inline verification steps, not standalone reports - plus a Branch
    filter on both.** Client's own call, going through the Reports section
    screen by screen: neither of these two is really a "report" someone
    opens later - both are a cross-check a Maker needs to see every time
    data is pulled (Test & Save Extraction) or imported (Manual Upload),
    the same run that just wrote the data. A new helper,
    `_inline_cross_check_for_run` (ar_mis.webapp.app), scopes both down to
    exactly the branch/period that run just saved - reusing the identical
    `compute_tb_cross_check_summary`-feeding `weekly_snapshot` rows and
    `compute_branch_sales_cn_dn_totals` the full Reports-section versions
    already use, never a separate computation. Both `test_extraction.html`
    and `manual_upload.html` now render this scoped pair right under each
    run's own result card. The two full-history versions stay in the
    Reports section too (client: "we can keep both"), just moved to the
    last two cards instead of the first, since they're the least
    "report"-like of the nine.

    Live-verification caught a real gap before this shipped: an early
    test of the inline Branch P&L section against a manual upload showed
    "No invoices recorded for this run" even though real invoices had
    just been processed and TB-reconciled. Root cause wasn't a pipeline
    bug - `build_sales_dn_register_row` requires `voucher.party_ledger_name`
    (the voucher-level PARTYLEDGERNAME tag), which a real Tally export
    always carries (confirmed against `fixtures/real_samples/SalesReg.xml`)
    but the simplified unit-test fixture used for the live check never
    did. Fixed the test data, not the app; the two inline-cross-check
    tests were also tightened to assert the real sales figures actually
    render, not just the section headings, closing the coverage gap that
    let this slip past the test suite in the first place.

    Client's later ask, same thread: both the full-history TB Cross-Check
    and Branch P&L Cross-Check screens gained a Branch filter, defaulting
    to All Branches, where picking one branch actually changes the
    figures - not merely which rows are visible. For TB Cross-Check this
    scopes the three KPI tiles too (Total Debtor as per Books included),
    deliberately different from the existing From/To date range, which by
    design (item 20) never shrinks those tiles - a branch is a real subset
    of the business, a date window is not. For Branch P&L Cross-Check,
    selecting a branch filters the underlying register rows at the source
    (`Store.all_sales_dn_rows`/`all_credit_note_rows`'s own `branch_id`
    parameter) and drops the now-redundant "All Branches" aggregate row,
    rather than showing the same number twice.

    Client's correction, same thread: the inline pair above was first
    built tied to "the run this exact request just processed" - gone the
    moment you navigated away, and absent entirely if you hadn't just run
    or uploaded anything. Client's own clarification: these should be a
    **permanent fixture** of Test & Save Extraction and Manual Upload, not
    something that only flashes up post-action. Replaced with
    `_standing_cross_check_panel` (ar_mis.webapp.app) - a small standalone
    Branch dropdown + Apply, independent of the main extraction/upload
    form, always rendered on both pages regardless of whether anything was
    just run. After a successful test/save/upload, it auto-selects the
    branch just acted on (convenience only - its own `panel_branch_id`
    query param always wins if set), so it already reflects what just
    happened without an extra click.

    Caught and fixed before this shipped: the panel's first draft defaulted
    to financial-year-to-date, same as the full Reports-section screens -
    which would have silently hidden a deliberately backdated run (a
    first-time backfill for a prior period, which this app explicitly
    supports) the moment "today" moved into a later financial year.
    `_inline_cross_check_for_run` now takes optional `period_start`/
    `period_end` (None either side = no bound), and the standing panel
    calls it fully unbounded - all recorded history for that branch -
    since its whole job is "does this reflect what was just done,"
    whatever period that happened to be.

    Also addressed in the same round, not a code change: the client
    noticed old weeks (from real, prior extraction runs) still showing on
    the Weekly Movement Register page after testing an unrelated branch on
    an unrelated date range, and asked why. Not a bug - that register is,
    by design (item 15's catalog, "append-only history"), a whole-
    portfolio snapshot across every branch combined, never scoped to
    whatever was most recently touched elsewhere. Confirmed by reading
    `weekly_movement_report`/`weekly_movement_record` rather than guessing.

    Client's second correction, after actually seeing the standing panel
    live: it had no totals (none of the three KPI tiles the real TB
    Cross-Check screen carries), and the Branch P&L section sat buried
    below a potentially long, row-per-week TB table, easy to miss
    entirely. The client's own fix was better than the original design -
    two plain links below the extraction/upload form, straight to the
    real `tb_cross_check_report`/`branch_totals_report` screens (full KPI
    tiles, search/filter, export - all already built and tested there),
    rather than re-deriving a stripped-down copy on this page. Replaced
    `_standing_cross_check_panel` and `_inline_cross_check_for_run`
    entirely with a small `_cross_check_links(result)` helper that builds
    two URLs, `branch_id`-scoped to whichever branch `result["branch"]`
    belongs to (via each report's own branch filter, item 39 above) when
    a result exists, unscoped ("All Branches") otherwise. No table
    rendering, no extra store queries, no new report - still a permanent
    fixture of both pages (the two links always render), just pointing at
    the reports that already did this correctly instead of rebuilding a
    worse version of them inline.

40. **NEW, resolved: AR Snapshot's Total AR, Related Party AR, and
    Reporting Period AR are now Tally-sourced, not registers-sourced -
    deliberately keeping two parallel universes on one screen.** Client's
    explicit standard, reviewing this report from a CEO/CFO's seat: Total
    AR must never let management ask "my books say X, your report says
    Y" - so it has to tie exactly to Tally's own Sundry Debtors closing
    balance, not to whatever the invoice-level registers happen to
    compute. `compute_ar_snapshot` (ar_mis.dashboard) now takes
    `tally_closing_by_party` (Store.latest_weekly_snapshot_closing_by_party(as_of)
    - the same per-party, as-of-date Tally cross-check Ageing Matrix and
    TB Cross-Check already use correctly) in place of the old
    `latest_weekly_snapshot_closing_total` (which only ever compared
    against whichever ONE branch happened to be extracted most recently
    systemwide - a real, confirmed bug, not just an ambiguous definition,
    and the actual reason the old "Rounding Difference" figure the client
    flagged as wrong was in fact wrong).

    Two deliberately separate universes now coexist on this one screen,
    per the client's own split:
    - **Position - Tally-sourced**: Total AR (every Sundry Debtor, Related
      Party included - Option A, client's explicit pick over excluding it:
      "the one number that can never be second-guessed against the TB"),
      Related Party AR (a disclosure slice WITHIN Total AR via
      CustomerMasterRecord.grouping, never subtracted from it), and
      Reporting Period AR (renamed from the old, meaningless "Sundry
      Debtor AR" - Total AR minus Pre-MIS Outstanding, i.e. the slice the
      registers can actually itemize at invoice level since go-live).
      Pre-MIS Outstanding itself needed no code change - it was always a
      books-derived static figure, keyed into Customer Master once at
      go-live, never recomputed.
    - **Performance - registers-sourced, unchanged**: Open AR by FY,
      Unapplied Cash/CN, Overdue AR and its ageing buckets, Bad Debt Risk,
      Notional Interest Cost, DSO, Collection Efficiency, PTP Kept Rate,
      Average Collection Period by branch - client's own call, since none
      of these can be Tally-sourced even in principle (ageing needs an
      invoice-level due date a single ledger closing balance can't
      carry). Internally renamed the old `total_ar` local variable to
      `workings_total_ar` and repointed DSO/Overdue %'s AR input at it,
      so these metrics keep behaving exactly as before this change -
      Overdue % is now explicitly "% of the registers' own tracked AR",
      not "% of Total AR", since those two totals can now legitimately
      differ.
    - **Reconciliation Check** (renamed from "Rounding Difference",
      which undersold what it actually catches): the one deliberate
      bridge between the two universes - Tally-sourced Total AR minus the
      registers' Workings total, same date. Zero confirms the invoice-
      level registers account for everything Tally shows; non-zero is a
      real, worth-investigating gap, not noise. None (not zero) when
      there's no Tally data on record yet.

    TB Reconciliation Cross-Check gained a **Grouping column** (joined
    from Customer Master, client's own ask once the Related Party
    exclusion gap was spotted) - load-bearing now, not cosmetic, since
    AR Snapshot's Total AR / Related Party AR / Reconciliation Check
    tiles all link straight to this exact screen (`to_date` pre-scoped)
    as their "view the figures behind this number" drill-down, per the
    client's own ask that a KPI tile should open the real report behind
    it rather than nothing. Pre-MIS Outstanding links to Customer
    Master; Unapplied Cash/CN link to Exception Register (`as_of` pre-
    scoped). DSO, Collection Efficiency, PTP Kept Rate, Notional
    Interest Cost, and Average Collection Period by branch stay
    deliberately non-clickable (client's own call) - there is no
    transaction list that "is" a ratio, only the inputs that produced
    it. AR Snapshot also widened to the 1600px `main.wide` layout, same
    as every other report screen.

    `Store.latest_weekly_snapshot_closing_total` (the buggy single-
    latest-week helper) is now unused by anything and was deleted
    outright, its two tests with it, rather than left as dead code.

41. **NEW, resolved: a round of client feedback on the live AR Snapshot -
    a back-navigation link on every report, Exception Register's missing
    export, Reconciliation Check made self-explanatory, Open AR by FY as
    tiles, and a new Pre-MIS Adjustment Register.** Client reviewed the
    live page screen by screen; several items traced back to genuine
    gaps, confirmed by reading the code rather than guessing:

    - **Back to Reports link**: every report screen (nine of them) now
      carries a `← Back to Reports` link via a shared `_back_to_reports.html`
      partial - client's own catch that reaching a report meant going
      through the Reports index every time, with no way back except
      re-navigating there.
    - **Reconciliation Check made self-explanatory**: an unapplied
      receipt or CN reduces Tally's own balance but not any specific
      invoice's open amount in the registers (nothing to match it
      against) - so in an otherwise-clean book, Reconciliation Check's
      gap should equal exactly -(Unapplied Cash + Unapplied CN). Proved
      this by reproducing it live (a ₹20,000 unapplied receipt produced
      exactly a -₹20,000 Reconciliation Check, while TB Cross-Check's own
      per-row check showed "Reconciled" for the same party - two
      different "Workings" definitions, both correct, measuring different
      things). Added `ARSnapshot.reconciliation_unexplained` (=
      reconciliation_difference + unapplied_cash + unapplied_cn) and
      changed the tile's caption to show the breakdown directly: green
      and "fully explained" when the residual is zero, red and naming the
      residual (pointing at Register Exceptions Review for an unresolved
      Pending Review item as one candidate cause) when it isn't. Client's
      explicit choice to keep Reconciliation Check comparing against the
      invoice-level registers total (not TB Cross-Check's simpler
      roll-forward total) specifically because it surfaces unapplied cash
      as a real, actionable signal - the alternative would hide it.
    - **Open AR by Financial Year**: converted from a table to its own
      KPI tile row (client's own earlier ask), moved under the
      Performance heading since it's a registers-sourced figure like
      everything else there, not a Tally-sourced "Position" one.
    - **Exception Register**: gained an Export to Excel
      (`build_exception_register_workbook`, one sheet per sub-report -
      this screen had none at all, confirmed by checking, unlike every
      other report in the app) and each of its six sub-reports is now
      wrapped in its own `.card` for visual separation (client's own
      catch: inconsistent-looking stacked tables made it hard to find a
      given sub-report's own data at a glance).
    - **Pre-MIS Adjustment Register** (`ar_mis/pre_mis_register.py`, new
      report at `/reports/pre-mis-adjustments`): client asked how Pre-MIS
      Outstanding actually changes over time, worried it might be
      silently reduced by ordinary receipts. Confirmed by reading every
      write path: it never is - the only way to move it is
      `record_pre_mis_adjustment`, triggered exclusively by a Maker
      deliberately resolving a Pending Review CN/Receipt as belonging to
      a pre-MIS-era invoice, logged to a `pre_mis_adjustments` table that
      already existed but had nothing reading it back. Added
      `Store.all_pre_mis_adjustments()` and this report: one row per
      party that ever carried a Pre-MIS balance (Original Seed -
      reconstructed as current balance minus every adjustment on record,
      since the stored figure is itself a running balance, not a fixed
      starting point - Total Adjustments, Current Balance), plus the full
      adjustment log underneath. AR Snapshot's Pre-MIS Outstanding tile
      now links here instead of Customer Master.

42. **NEW, resolved: Pre-MIS Adjustment Register's "Total Adjustments"
    column split into "Receipts Applied" and "CN / Journal Written
    Off".** Client's own ask, framed as a management-visibility need:
    knowing how much of a Pre-MIS balance's movement was actual cash
    collected versus written off matters, and a single combined column
    couldn't show that. `PreMisAdjustment` has no explicit source field
    and adding one would need a schema migration for a purely display-
    only split, so `compute_pre_mis_register` instead parses the
    leading token of each adjustment's already-stored `reason` string -
    both writer routes (`credit_note_reclassify`,
    `receipt_journal_reclassify`, and their revert-to-pending-review
    counterparts in webapp/app.py) reliably start it with "CN ...",
    "Receipt ...", "Journal ...", or "Reversal of <one of those> ..."
    - no new column needed. A Journal-sourced adjustment is bucketed
    with Credit Notes, not Receipts: it is not actual cash (registers.py
    calls Journal "Tally's most generic voucher type - loan
    adjustments, creditor entries, depreciation, anything"), so it
    belongs in the non-cash/written-off column for this report's
    purpose - a judgment call, worth revisiting if it turns out Journal-
    sourced Pre-MIS adjustments are common enough in practice to deserve
    their own column.

43. **NEW, resolved: "Back to Reports" now goes back one step, not
    always to Reports home.** Client's own bug report with an exact
    reproduction: AR Snapshot → Total AR tile → TB Cross-Check →
    "Back to Reports" landed on Reports home, skipping past AR
    Snapshot - a 2-step jump when the client expected 1. The link is a
    single shared `_back_to_reports.html` partial included on every
    report screen with no per-page knowledge of where it was reached
    from, so a backend `return_to`-parameter approach would mean
    threading a new query parameter through every cross-report
    drill-down link app-wide. Used the browser's own history instead:
    the link's `onclick` calls `window.history.back()` when there's a
    prior same-origin entry in this tab's history (checked via
    `document.referrer`), and falls through to its plain `href` (Reports
    home) otherwise - a bookmarked or freshly-typed URL still lands
    somewhere sensible, while any drill-down navigation now genuinely
    goes back exactly one step, whatever that step is, without needing
    to special-case each report pair.

44. **NEW, resolved: batch processing added to Register Exceptions
    Review.** Client's own catch, after Exception Register's export and
    card-tiling landed: Register Exceptions Review had the same
    checkbox/select-all/search scaffolding (`table_tools.js`) as Credit
    Note Register and Receipt & Journal Register, but no batch action to
    go with it - every exception still had to be reviewed one row at a
    time. Added `register_exceptions_review_batch`
    (`/reports/register-exceptions/review/batch`, maker-only), the same
    pattern as `credit_note_reclassify_batch`: takes a set of selected
    exception ids, one review outcome, one reviewer name and an optional
    shared note, applies it to every selected row that is still `open`,
    and silently skips anything already reviewed (selected alongside the
    open ones, never re-reviewed or double-counted) - reported back as
    "Reviewed N of M selected... K skipped (not open)." The row
    checkboxes already existed for `table_tools.js`'s own selection
    count; they now also carry `form="regexceptions-batch-form"` so
    `table_tools.js`'s existing confirm-before-apply safety net (any
    form a `[data-tt-row-select]` checkbox targets) applies here for
    free, same as it already does for Credit Note Register.

45. **NEW, resolved: Overdue AR, Bad Debt Risk, and Notional Interest
    Cost tiles now link to Sales & DN Register, pre-filtered.** Client
    confirmed the proposal before this was built. Overdue AR and Bad
    Debt Risk (181+) link to Sales & DN Register's existing `overdue`
    and `bucket` column filters - no new plumbing needed there, both
    columns already existed with `data-tt-filter`. Notional Interest
    Cost had nothing to link to: there was no per-invoice figure
    anywhere, only the single portfolio-level total on the tile. Added
    `InvoicePosition.notional_interest` (`ar_mis/registers.py`) -
    `open_amount * NOTIONAL_INTEREST_RATE * days_past_due / 365` when
    overdue, else zero - computed once per invoice in
    `compute_invoice_position`, with `NOTIONAL_INTEREST_RATE` itself
    moved from `dashboard.py` into `registers.py` (re-exported from
    `dashboard.py` for the existing import) so the per-invoice figure
    and `ARSnapshot.notional_interest_cost`'s portfolio sum are always
    the same constant, never two copies that could drift. Shows as a
    new Sales & DN Register column (also added to its Excel export) and
    links from the tile the same way the other two do.

    Making the link actually land pre-filtered needed one small, generic
    addition to `table_tools.js` rather than a one-off for this screen:
    any filter dropdown or the search box can now be pre-set from the
    page's own URL, namespaced by the table's own `data-tt` id
    (`<id>_filter_<col>=value`, `<id>_search=text`) - so
    `?salesdn_filter_overdue=Yes` opens Sales & DN Register already
    showing only the overdue invoices, applied before the table's first
    render so the page doesn't flash unfiltered first. Verified live in
    an actual browser (Playwright), not just via the rendered-HTML
    markup a server-side test can see: a two-invoice fixture (one
    overdue, one not) down to exactly the one expected row for both
    `salesdn_filter_overdue=Yes` and `salesdn_filter_bucket=181%2B`, and
    the Notional Interest column's own figure matched the formula by
    hand (₹1,25,000 open, 224 days overdue → ₹7,671.23). This same
    mechanism is reusable for any future report that wants to link in
    pre-filtered rather than to the whole table.

46. **FIXED, client-caught real bug: a tile's URL pre-filter silently
    fell back to "show everything" when the target value had zero
    matching rows.** Confirmed live the morning after item 45 shipped:
    clicking Bad Debt Risk (181+) should show a genuinely empty register
    (their real data had nothing in that bucket - the tile itself read
    close to Nil) but instead showed every open invoice, DPD nowhere
    near 180. Root cause, found by reading `table_tools.js`'s own filter-
    dropdown code: each dropdown's options are built ONLY from values
    actually present in the currently-rendered rows (`item 45`'s own
    scan of what's on screen) - so when a tile links in with a value
    that doesn't occur in the data at all (exactly the "should be Nil"
    case), that option never existed in the dropdown to begin with. The
    URL pre-filter code's own `hasOption` guard, meant to protect against
    an invalid value, was silently skipping the filter entirely in this
    exact case instead - leaving the dropdown at "All", which shows
    every row. Fixed by adding the requested value as a dropdown option
    when it's missing, then selecting it regardless - the filter now
    always applies, and in the "nothing matches" case it correctly
    empties the table instead of showing everything. Overdue AR's own
    link never hit this because "Yes" is (almost) always present in real
    data; Bad Debt Risk and any future near-Nil tile link would hit it
    every time without this fix. Re-verified live (Playwright) with a
    fixture that has zero 181+ invoices: the drill-down now correctly
    shows "Showing 0 of 1", not "Showing 1 of 1".

47. **NEW, resolved: Pre-MIS Adjustment Register gained an Export to
    Excel.** Client's own catch: every other report screen has one,
    this didn't. `build_pre_mis_register_workbook`
    (`ar_mis/register_export.py`) - two sheets, same shape and order as
    the screen (By Party, then Every Adjustment most recent first) - a
    second rendering of the same data `compute_pre_mis_register` and
    `Store.all_pre_mis_adjustments()` already produce for the template,
    never a separate recomputation.

48. **NEW, resolved: Overdue AR / Bad Debt Risk / Notional Interest Cost
    now drill through a party-wise summary before invoice-level
    detail.** Client's own explicit structure (confirmed via two targeted
    questions rather than guessed): these 3 tiles should land on a
    management-facing "how much per party" summary first, not straight
    on the AR team's invoice-level register - the two audiences read
    different things off the same numbers. Reused Ageing Matrix's
    existing Customer Detail table as that summary layer rather than
    building a separate report: it already had exactly the right shape
    (one row per party per branch, every ageing bucket including 181+,
    Total Open, Tally cross-check) - it only needed two more columns:

    - `AgeingMatrixRow.overdue_total` (`ar_mis/ageing_matrix.py`) - every
      bucket except Current and Closed, summed directly from
      `AGEING_BUCKET_ORDER` so a bucket added there later is picked up
      automatically rather than needing a second hardcoded list.
    - `AgeingMatrixRow.notional_interest_cost` - that party's
      `InvoicePosition.notional_interest` (item 45) summed across their
      open invoices.

    The 3 AR Snapshot tiles now link to `/reports/ageing-matrix?tile=...`
    instead of straight to Sales & DN Register. Each customer row there
    carries its own "View invoices" link into Sales & DN Register,
    scoped to exactly that party (`salesdn_search=<party_id>`, reusing
    item 45's own URL-prefilter mechanism) AND the same ageing criterion
    the tile represents (`tile=bad_debt` → `salesdn_filter_bucket=181+`;
    `tile=overdue` or `tile=notional_interest` → `salesdn_filter_overdue=
    Yes`, since Notional Interest is computed only over the overdue
    population) - client's own explicit call on the second question: the
    detail view for one party should stay scoped to what the summary row
    means, not open into that party's whole invoice history. A direct
    visit to Ageing Matrix (no `tile` param) carries no extra ageing
    filter on its own per-party links. Verified live (Playwright) end to
    end: AR Snapshot's Bad Debt Risk tile → Ageing Matrix (tile=bad_debt)
    → that party's own "View invoices" link → Sales & DN Register,
    landing on a genuinely empty, correctly-filtered table for a fixture
    with nothing in the 181+ bucket; the same party's Overdue AR path
    correctly shows their one actually-overdue invoice.

    Not done, left as a flagged follow-up rather than silently decided:
    no row-level filtering was added to Ageing Matrix itself (e.g.
    "hide every party with zero 181+ exposure") - its filter dropdowns
    only support exact-value matching, and a "nonzero in this bucket"
    filter is a different kind of filter that wasn't part of what was
    confirmed. Today the view there is always every party, every bucket
    visible side by side (a cross-tab's whole point); sorting or
    filtering it by the arriving tile's own column is a small, separate
    addition if wanted next.

49. **FIXED, client-caught real bug: cross-branch reference collision in
    `compute_invoice_position`'s own matching.** Client's own challenge,
    after being told Pending Review was the likely cause of their real
    ₹15,500 Reconciliation Check residual, then checking and finding
    zero Pending Review items in either register: pushed back, correctly,
    that the explanation didn't hold for their actual data. Re-
    investigated rather than re-guessing. Found: `compute_invoice_
    position`'s matching key was `(party_id, bill_allocation_reference)`
    - no `branch_id` - while every caller (AR Snapshot, Ageing Matrix,
    Exception Register, Sales & DN Register's own display, PTP batch
    defaults) passes it CN/Receipt lists spanning every branch
    (`Store.all_credit_note_rows()`/`all_receipt_journal_rows()` with no
    branch filter). Every branch runs its own independent books and its
    own voucher numbering series, so two branches commonly reuse the same
    reference string for two entirely different invoices of the same
    party - and a receipt genuinely meant for one branch's invoice could
    silently "pay off" a same-named invoice at a different branch too,
    understating the portfolio's own Workings total by the stolen amount.
    Reproduced live in isolation before touching anything (a KOL invoice
    wrongly absorbing a MUM receipt's full amount, going negative-open in
    the process) - confirmed real, not theoretical. TB Cross-Check never
    does this invoice-level matching at all, so it stayed perfectly clean
    for both branches the whole time, which is exactly why the client's
    own challenge in item 48's discussion ("if TB Cross-Check already
    proves everything was captured, why is there still a gap neither
    Pending Review nor Unapplied Cash/CN explains") was the right
    question to keep pushing on. Fixed by adding `branch_id` into the
    match key and into both comparison tuples inside `compute_invoice_
    position` itself - a single, centralized fix that every call site
    inherits automatically, rather than patching each of them separately.
    All 592 existing tests still passed unchanged (nothing relied on the
    old cross-branch behavior); added a dedicated regression test
    reproducing the exact scenario. This is very likely the client's own
    real ₹15,500, or a meaningful part of it, if the same party transacts
    at more than one of their branches - worth re-checking Reconciliation
    Check after pulling this fix.

50. **NEW, resolved: CN/Receipt classification's "Current" label replaced
    with two self-explanatory ones.** Client's own catch: "Current"
    explained nothing about what it meant, and silently bundled two
    different cases under one word - a CN/Receipt genuinely sitting
    on-account with no reference at all (exactly what `compute_unapplied_
    cash_by_party`/`compute_unapplied_cn_by_party` already total
    elsewhere as Unapplied Cash/CN) and one whose reference correctly
    matched a tracked invoice. Client's own explicit naming: the
    on-account case must read as "Unapplied Cash"/"Unapplied CN" (the
    same words already used for its own total, not a generic "nothing to
    review" label that hides what it actually is), and the matched case
    as "Matched". The stored `RegisterClassification.CURRENT` enum value
    is unchanged - still the one thing `compute_invoice_position` and the
    unapplied-total functions key their own filtering on - only the label
    a human reads is split, via new `credit_note_classification_label`/
    `receipt_journal_classification_label` helpers (`ar_mis/registers.py`)
    registered as Jinja filters. Applied everywhere the word showed:
    both registers' pills and their "What Classification means"
    explainer cards, the Resolve dropdown's "Current (data-entry
    correction)" option (now "Matched (data-entry correction)" - the
    underlying `target_classification=current` wire value is untouched),
    the Classification column's own filter dropdown (now filters on
    "Matched"/"Unapplied Cash"/"Unapplied CN" as their own distinct
    values, not lumped under one), and both Excel exports.

51. **FIXED, client-found real bug: the actual ₹15,500 - a mixed-direction
    Receipt voucher's bill allocations summed by magnitude instead of by
    sign.** Client found and pulled up the exact live Tally voucher behind
    it: Receipt No. 9 against KAY DEE ELECTRIC CO. carries THREE bill
    allocations in one voucher - Agst Ref 5662 is a DEBIT of ₹7,750.00
    (reversing a previous wrong application against it), Agst Ref
    CIPL/5284/25-26 and CIPL/6233/25-26 are normal CREDITS of ₹5,750.00
    and ₹6,835.00 - netting to the voucher's true ₹4,835.00 against its
    bank leg. `build_receipt_journal_register_rows` stored `amount=abs(
    entry.amount_as_extracted)` for every bill allocation line, discarding
    which direction each one actually was - summing the three lines'
    magnitudes gives ₹20,335.00, not the true ₹4,835.00, an overstatement
    of exactly ₹15,500.00. This whole voucher (Pending Review, since
    "5662" doesn't match any tracked invoice - the voucher-level
    invariant then drags the other two, otherwise-matchable lines into
    Pending Review too) had been resolved by a Maker as a Pre-MIS
    Adjustment, moving `-sum(line amounts)` into Pre-MIS Outstanding -
    `-20,335.00` instead of the true `-4,835.00`, understating workings_
    total_ar by exactly ₹15,500.00 and surfacing as an unexplained
    Reconciliation Check residual with no Pending Review item or
    Unapplied Cash/CN left to blame (exactly the client's own challenge
    in item 49's discussion - this is the concrete transaction behind it,
    found by the client, not guessed at).

    This bug was already contradicted by `compute_unapplied_cash_
    exceptions`'s own documented design ("a receipt fully offset by its
    own reversing journal are excluded" - only possible if amounts net
    via plain signed addition, which `abs()` makes impossible) -
    confirming it as a real defect, not a deliberate choice, before
    touching anything. Fixed by storing the bill allocation's own raw
    signed amount instead of its magnitude. Verified end to end against
    the exact real voucher (live reproduction) before and after the fix,
    and added a permanent regression test (`tests/test_registers.py`)
    plus a second one for the Pre-MIS Adjustment path specifically
    (`tests/test_webapp.py`) proving the resolved amount is now the true
    net ₹4,835.00. One pre-existing test's own fixture (`test_receipt_
    journal_debtor_creditor_journal_attributes_to_the_debtor_leg`) had
    baked in the old, wrong assumption that a debit entry's magnitude
    should always display positive - corrected to assert the true signed
    value, which doesn't touch what that test actually exists to verify
    (party attribution, not sign).

52. **FIXED, client-caught real bug: Receipt & Journal Register's totals
    row was missing one cell, shifting every sum one column left.**
    Client's own screenshot: the Applied Amount/Unapplied Balance sums
    were rendering under "Linked Invoice Value"/"Applied Amount" instead
    of their own headers. The totals `<tr>` had 17 `<th>` cells against
    the header row's 18 - missing the empty cell for "Linked Invoice
    Value" (which has no sum) before the two sum cells. Sales & DN
    Register's and Credit Note Register's own totals rows were checked
    too and are correctly aligned (31/31, 13/13) - this was specific to
    Receipt & Journal Register. Fixed by adding the missing empty `<th>`;
    added a regression test that checks cell counts match and that each
    sum tag lands inside its own named header's cell, not just that a
    number appears somewhere in the row.

53. **Audited every other `abs()` in `ar_mis/` after item 51, per client's
    own ask** ("this is something which can always be there in data where
    a debtor is increased by way of journal entries instead of
    decreased"). Six call sites total, checked one at a time rather than
    assumed safe by pattern-matching:

    - `ar_mis/webapp/formatting.py` (`format_inr`) - **correct, no
      change.** Captures the sign (`"-" if amount < 0`) before `abs()`,
      re-prepends it to the final formatted string. Pure display
      technique; never touches the underlying signed value.
    - `ar_mis/reconciliation_report.py` (`total_current_absolute_
      difference`, TB Cross-Check's own summary tile) - **correct, no
      change.** Deliberately sums magnitudes across different PARTIES -
      the spec's own Section 4.1 is explicit that a total-level check
      done any other way is "structurally blind to offsetting errors"
      (Party A overstated ₹10,000, Party B understated ₹10,000 nets to a
      false "all clear" at ₹0 if signed). Using `abs()` here is the
      point, not a bug.
    - `ar_mis/reconciliation_report.py` (`mismatched.sort(key=lambda r:
      abs(r.difference), ...)`) - **correct, no change.** Only a sort
      key for "show the most material mismatches first" - the row's own
      `difference` field keeps its real sign; nothing about the
      underlying data is touched.
    - `ar_mis/registers.py` `build_sales_dn_register_row`'s own
      `magnitude = abs(entry.amount_as_extracted)` (CGST/SGST/IGST/
      taxable value) - **left as-is, lower-risk but not zero-risk.**
      Unlike a Receipt/Journal/CN's bill allocations (which can
      legitimately point at different invoices in opposite directions
      within one voucher), these are the tax/revenue side of a SINGLE
      Sales/Debit Note voucher - by ordinary accounting practice a
      single invoice's own tax lines don't offset each other inside the
      same document the way a correction-bearing Receipt or CN's
      allocations do. No concrete failing voucher found for this one
      (unlike the two fixed below) and not touched without one, but
      flagged here as the next place to look if a Sales/DN-side
      discrepancy is ever found the same way the Receipt/Journal one
      was - a real client voucher, not a hunch.
    - `ar_mis/registers.py` `build_receipt_journal_register_rows`'s
      `amount` field - **the bug item 51 already fixed.**
    - `ar_mis/registers.py` `build_credit_note_register_row`'s
      `cn_amount += abs(entry.amount_as_extracted)` - **FIXED, same bug
      class, client's own instinct confirmed correct.** A single Credit
      Note voucher can carry bill allocations in both directions too
      (crediting one invoice while reversing a wrong application
      against another, in the same voucher) - `abs()` would overstate or
      understate `cn_amount` exactly the way it did for Receipt/Journal.
      Fixed the same way: keep the allocation's own signed amount. No
      existing test exercised a multi-allocation or negative-amount CN
      voucher (same gap that let the Receipt/Journal bug go unnoticed);
      added a regression test with a mixed +500.00/-300.00 CN voucher
      netting to the true 200.00, not an `abs()`-summed 800.00.

      **Not fixed, flagged as a separate, larger question rather than
      silently bundled in**: `CreditNoteRegisterRow` keeps one row per
      VOUCHER, not per bill allocation (its own docstring says so
      explicitly) - a CN voucher touching more than one invoice already
      collapses to one blended `cn_amount` and only the LAST reference
      found, regardless of sign handling. The sign fix corrects that
      single row's own total (useful for Unapplied CN netting and a
      Pre-MIS Adjustment resolution's total, the same two places the
      Receipt/Journal fix mattered), but a multi-reference CN voucher's
      per-invoice matching (`compute_invoice_position`) still can't
      correctly credit more than one of the invoices it actually
      touches - that would need restructuring this register to one row
      per bill allocation, mirroring Receipt/Journal's own design. Worth
      doing if a concrete multi-reference CN voucher turns up the same
      way the Receipt one did, but a bigger, separate change (schema,
      storage, export, matching logic, template) not undertaken here
      without a real example and explicit sign-off first.

54. **NEW, resolved: TB Cross-Check's table now defaults to one row per
    party (current position), not every week ever recorded.** Client's
    own catch, from live data: the table listed 1,930+ rows once several
    months of weekly extraction had piled up, one per party per week
    forever. Client's first instinct was to replace the underlying
    weekly incremental engine itself with a single go-live-to-today
    cumulative check instead. Traced why that wouldn't actually help
    before building it: `resolve_opening_balances` already chains each
    week's Opening from the PRIOR week's own stored `closing_computed`,
    recursively, all the way back to the original Pre-MIS seed - so
    today's latest `closing_computed` is already mathematically
    identical to "go-live balance + every movement since," just computed
    incrementally. A real switch to an explicit cumulative recomputation
    would (a) produce the exact same number, given the same vouchers and
    matching rules - it fixes nothing numerically; (b) cost real,
    growing performance every single run, forever, since it would need
    to re-fetch and reprocess the ENTIRE voucher history from Tally each
    time instead of just that week's own slice (this codebase already
    has one past bug fix for exactly "large date-range fetch freezes" -
    reintroducing that shape of problem on a schedule that only gets
    slower as the business ages); and (c) permanently lose the one real
    benefit weekly rows give: when something's wrong, knowing which
    week's handful of vouchers to go looking in, instead of "somewhere
    in the last 18 months." Laid this out for the client plainly rather
    than build what was first asked for; client agreed to the
    alternative instead.

    Implemented: `compute_latest_per_party` (`ar_mis/reconciliation_
    report.py`, public - was the private `_latest_per_party` already
    backing the three KPI tiles' own "current state" figures, exposed so
    the table can reuse the identical reduction rather than a second one
    that could drift) is now the table's default (`?view=latest`,
    reusing `to_date` the same way the tiles already do - from_date
    doesn't apply in this view). `?view=history` switches back to the
    exact prior behavior (the full from_date..to_date range, every week,
    unchanged). Nothing about `weekly_snapshot` itself changed - still
    one row per party per branch per week, written and kept forever; this
    is a display-only default. The view choice threads through the
    date-range form (a hidden field, so clicking Apply from Full History
    doesn't silently snap back to Latest) and the Excel export (so the
    download always matches what's on screen, exactly as that export's
    own docstring already promised before this change).

    Two other client findings from the same live-data review, surfaced
    but not yet acted on:
    - **Journal vouchers where both legs are tracked Sundry Debtors** are
      excluded from the Receipt & Journal Register by the existing
      "touches multiple tracked debtors... needs a human look, not a
      guess" rule (confirmed, via the exact Reason text client checked,
      to be this case and not the separate "no leg touches a tracked
      debtor" one) - working as designed, but there is currently no
      action a human can take to resolve one into the registers (unlike
      Pending Review's own Resolve flow). Confirmed this does NOT affect
      TB Cross-Check's own correctness: `aggregate_party_movements` (TB
      Cross-Check's own roll-forward) has no such exclusion at all - it
      sums every voucher entry per party unconditionally, so a voucher
      excluded from the register for this reason is still counted
      correctly in TB Cross-Check's own independent total. That's also
      the answer to the client's own "is TB checking against itself"
      question from the same review: `closing_extracted` comes from a
      live, independent `client.fetch_ytd_sundry_debtors()` pull, never
      from anything this app itself computes - confirmed by tracing the
      exact call chain, not asserted. A follow-up worth doing: a resolve
      action for the multi-debtor exclusion, if the client wants one.

55. **FIXED, resolved per client's own follow-up ask: Receipt/Journal
    vouchers touching two or more tracked debtors are no longer excluded
    wholesale from the register.** Client confirmed wanting item 54's
    flagged follow-up built. Re-examined `build_receipt_journal_register_
    rows`'s own "touches multiple tracked debtors... needs a human look,
    not a guess" exclusion and found the actual guess it was protecting
    against: the function popped a SINGLE party name from the matched
    set and stamped every row with it regardless of which entry it
    actually came from - that would genuinely have been wrong for a
    voucher like the client's own real example (a debtor-to-debtor
    reallocation Journal: one party debited, a different one credited
    the same amount, in one voucher). But each entry already carries its
    own correct `party_ledger_name` - there was never anything to guess
    once entries are grouped by their own party first. Rewrote the
    function to group `matched_entries` by `entry.party_ledger_name` and
    run the exact same per-line logic (bill-reference matching, sign-
    preserving amount per item 51, classification) independently within
    EACH group, concatenating the results - removing the exclusion
    entirely rather than adding a manual "resolve" action, since once
    grouped correctly there's no ambiguity left for a human to arbitrate.

    Item 10's "whole voucher, one classification" invariant is now
    scoped per PARTY within the voucher, not across it: if Party A's own
    line in a shared voucher is Pending Review, only Party A's own lines
    (within that voucher) get promoted - Party B's own, otherwise-clean
    lines are untouched, since their own Unapplied Cash netting has
    nothing to do with Party A's own unresolved reference.

    Confirmed live, end to end, against the client's own real Joy Ray /
    Manoj Lal voucher structure: both parties now appear correctly in
    the Receipt & Journal Register, the voucher no longer sits in
    Register Exceptions Review, and TB Cross-Check (which never relied
    on the register for this voucher's own movement - see item 54) is
    unaffected either way. Added regression tests for both the
    attribution fix itself and the per-party classification scoping. The
    existing "no leg touches a tracked debtor at all" and "debtor tagged
    as the creditor leg" exclusions are untouched - this only removes
    the "touches 2+ tracked debtors" one, which is the only one that was
    ever a guess rather than a real data gap.

    The Credit Note Register's own identical-looking exclusion
    (`build_credit_note_register_row`) was deliberately NOT touched here
    - that register keeps one row per VOUCHER, not per bill allocation
    (item 53's own flagged limitation), so the same fix would need
    restructuring it to one row per allocation first, a bigger, separate
    change not undertaken without its own concrete example.

56. **NEW, in progress: TB Cross-Check redesign — "TB vs Registers" — Phase
    1 (live Sundry-Debtors-scoped voucher pull) built and tested.** Client
    declared TB Cross-Check their "Holy Gita" - the one place to tell
    whether they or the system missed something - and asked for it to
    become "TB vs REGISTERS", explicitly at branch-and-party level, never
    invoice-level (a first proposal to switch the comparison basis to
    `compute_invoice_position`'s invoice-level matching was explicitly
    rejected).

    Root cause, confirmed by re-reading the actual code rather than
    assumed: `aggregate_party_movements` (today's TB Cross-Check basis)
    and `_build_and_persist_registers` both read the SAME raw vouchers
    independently, with different exclusion rules. A voucher excluded from
    the Registers (e.g. the old "touches multiple tracked debtors"
    exclusion, item 55) was STILL counted by `aggregate_party_movements`,
    which has no exclusion logic at all - so TB Cross-Check could show
    "Reconciled: Yes" for a party even when a real voucher never made it
    into any register. TB Cross-Check was proving "our raw pull from Tally
    adds up to Tally's own total", never "the Registers correctly captured
    everything" - two different guarantees, and the client's own real
    Joy Ray/Manoj Lal case is live proof of the gap.

    Client's own correction, confirmed: the real fix isn't inventing a new
    data source, it's recognizing this app already has one - the existing
    "drift findings" feature's YTD full-pull cross-check (`isolate_drift`,
    `ytd_voucher_xml` manual-upload slot) - but it compares against
    `voucher_log`, which has the exact same blind spot as `aggregate_
    party_movements` (logs every voucher touching a tracked party
    regardless of register exclusion). The real design (client's own,
    after several rounds of "no, you got it wrong" corrections):

    - **Check 1**: compare the three Registers against an independent,
      Sundry-Debtors-only voucher-wise list from Tally - voucher-wise, not
      bill-wise (client's explicit correction: "if a voucher is missing,
      all constituents are missing" - no invoice-level matching needed).
      A voucher missing from the Registers, or present in the Registers
      but absent from this independent list, is the gap this check exists
      to catch.
    - **Check 2**: TB Cross-Check itself rebuilt so its "our side" number
      comes from Customer Master Opening Balance + what's actually sitting
      in the Registers right now - never a fresh re-sum of raw vouchers.
      Only meaningful once Check 1 shows the Registers are complete -
      Check 1 is the gate, Check 2 sits on top of it. Client's own sharp
      observation, confirmed correct: since both checks now ultimately
      read the Registers, a real register gap shows up in BOTH checks at
      once - a discrepancy in only one would itself be a red flag about
      the checks' own logic, not just the data.
    - Client's standing principle for this whole app, stated explicitly:
      any reconciliation must be between two datasets ALREADY separately
      stored in the tool - never one side silently recomputed in the
      background from data that was pulled but never formally captured.

    **The data-source question - "how does the system even get a Sundry-
    Debtors-only voucher list" - took real, live back-and-forth to settle,
    documented here because every dead end taught something:**

    - Client's own real export (Tally: Sundry Debtors → Ctrl+H → Voucher
      view) confirmed the right GRANULARITY (voucher-wise, one row per
      voucher, no bill detail) but is a "Display Report" shape (flat
      `DSPVCH*` tags) - confirmed elsewhere in this doc as something
      Tally's UI export can produce but the live HTTP gateway cannot.
    - Client explicitly rejected "pull the whole company and filter
      client-side" (Path A) on real performance grounds - wasteful for a
      company with real transaction volume, and this app has already been
      burned once by assuming pulls scale (`_VOUCHER_FETCH_CHUNK_DAYS`).
    - Three blind `REPORTNAME` guesses against a live Tally (CHARZE
      INDUSTRIES, real client data) - "Group Vouchers", "Ledger Vouchers"
      with `SVCURRENTGROUP`, "Ledger Vouchers" with `SVCURRENTLEDGER` set
      to one real customer - all returned a completely empty
      `<ENVELOPE></ENVELOPE>`, no error. Confirmed these REPORTNAMEs
      simply don't exist in this Tally installation; guessing report
      names from general Tally knowledge without TDL documentation or
      live access is not a reliable methodology, and this was explicitly
      stopped after three misses rather than continued indefinitely.
    - **What actually worked, confirmed live**: a TDL `COLLECTION` of
      `TYPE=Voucher` filtered with two official, documented TDL functions
      - `$$FilterCount:AllLedgerEntries:<filter> > 0` and
      `$$IsLedOfGrp:$LedgerName:$$GroupSundryDebtors` (the latter correct
      even if the group has been renamed). Live result: 166 vouchers for
      a 7-day range, every single one Sales/Receipt/Credit Note/Journal -
      zero Purchase/Payment/Contra/Stock Journal/anything else leaked
      through, confirming the filter genuinely scopes at the source.
    - Confirmed limitation: this COLLECTION's own requested field list
      (Date/VoucherTypeName/VoucherNumber/PartyLedgerName) is ignored -
      Tally returns the full native Voucher object instead (same shape
      `voucher_export_request` already gets), and within that,
      `BILLALLOCATIONS.LIST`'s own NAME/BILLTYPE sub-fields come back
      empty for 604 of 606 real lines - only bare AMOUNT survives. Fine
      for this check's actual requirement (voucher existence, never bill-
      level), but this must NOT replace `voucher_export_request`'s full-
      company pull, which register-building genuinely needs real bill
      references from.
    - Confirmed, usefully: the known "nested LIST fields hang" bug (see
      `voucher_export_request`'s own docstring) did NOT reproduce here
      despite full nested detail being present - the real cause of that
      hang is volume, not nested fields per se; 166 real-world-scoped
      vouchers stayed well clear of whatever threshold a whole company's
      history crosses.
    - Side-finding, unrelated to this check but real: two bill allocations
      in the live response carried `BILLTYPE="On Account"` - a value this
      app's matching logic (`_ALLOCATED_BILL_TYPES`/`_UNAPPLIED_BILL_TYPE`)
      doesn't yet recognize at all. Flagged for separate follow-up, not
      fixed here.
    - Separately confirmed and fixed while building this: Tally's own
      voucher export, for a company with User Defined Fields configured,
      emits tags like `<UDF:_UDF_788538654.LIST>` - a bare colon in the
      tag name with no xmlns declaration, which crashes ElementTree
      ("unbound prefix") outright. `ar_mis.parsers._sanitize_xml` now
      strips these namespace-looking prefixes before parsing (nothing in
      this app ever looks up a UDF field by name, so mangling it is
      harmless) - the first real voucher pull carrying UDF fields would
      otherwise have failed to parse at all.

    **Phase 1 built and tested** (the live pull itself, wired in and
    persisted - comparison logic against the Registers is Phase 3, not yet
    built): `ar_mis.xml_requests.sundry_debtor_voucher_export_request`,
    `TallyClient.fetch_sundry_debtor_vouchers` (chunked the same defensive
    way as `fetch_vouchers`, confirmed safe only for a 7-day window so
    far), the new `YtdDebtorVoucherRow` model and `ar_mis.registers.
    build_ytd_debtor_voucher_rows` (one row per voucher per debtor leg,
    net-summed - never per bill allocation, and correctly splits a voucher
    touching 2+ tracked debtors into one row each, same fix as item 55),
    and a new `ytd_debtor_voucher` storage table (schema v14) keyed by
    branch + as_of + voucher + party, so a later reporting date's pull is
    never collapsed with an earlier one. Tested end-to-end against the
    real, unmodified live response (`fixtures/real_samples/
    SundryDebtorVoucherCollection.xml`) - 166 real vouchers parse and
    build rows correctly. Full suite green (619 tests) at this point.

    **Phase 2 built and tested**: Check 1's actual comparison logic
    (`ar_mis.ytd_debtor_cross_check.compute_ytd_vs_register_comparison`)
    and the new "TB vs Registers — YTD Debtor Vouchers" report tile
    (`/reports/ytd-debtor-vouchers`). Identity is always (voucher_type
    category, branch_id, voucher_number, party_id) - voucher_number is
    scoped per TYPE (a Sales "1" and a Receipt "1" are unrelated) AND per
    BRANCH (the same cross-branch collision class item 49 already fixed in
    `compute_invoice_position` - different branches commonly reuse the
    same Tally numbering independently). Two real bugs caught and fixed
    while building this, both confirmed by test before being declared
    real:

    - `SalesDNRegisterRow.note_type`'s own stored value is "Invoice"
      (`NoteType.INVOICE`), but the YTD pull's own categorization
      (`VoucherType.SALES`) is "Sales" - without normalizing one to the
      other, every genuinely matching Sales voucher would have shown as
      "Extra in Registers" purely because the two sides' type labels never
      compared equal, never because anything was actually wrong.
    - The matching key initially had no `branch_id` at all - the exact
      same collision class item 49 fixed elsewhere, reintroduced here by
      not learning that lesson the first time. Caught before shipping, not
      after a real client report.

    Four possible outcomes per voucher, shown as the table's Match/
    Mismatch column: `Match`, `Amount Mismatch` (both sides have it, the
    amounts differ), `Missing from Registers` (Tally's own list has it,
    the registers don't), `Extra in Registers` (a register has it, Tally's
    own independent list doesn't) - covering both directions of the
    client's original ask. The tile defaults to the most recent pull on or
    before the chosen date (a pull is a whole point-in-time snapshot, never
    interpolated) and uses the same search/filter/subtotal table-tools
    convention every other register/report already has. Batch-action
    checkboxes are present in the markup but have no action wired up yet -
    deliberately deferred to the Phase 6 correction actions, since there's
    nothing to batch-apply until those exist. Full suite green (633 tests)
    at this point.

    **Phase 3 built and tested: Check 2 redefined.** TB Cross-Check's
    "our side" number (weekly_snapshot's sales/credit_notes/debit_notes/
    receipts/journals/closing_computed) no longer comes from a fresh
    re-sum of raw vouchers (`aggregate_party_movements`, which has no
    exclusion logic at all) - it now comes from exactly what the three
    master registers captured for that run
    (`ar_mis.rollforward.aggregate_party_movements_from_registers`).
    `ar_mis.pipeline.process_branch_data` was reordered so the registers
    are built FIRST, and `_build_and_persist_registers` now returns the
    rows it actually built (not just exceptions) so movements are computed
    from precisely those rows, not the branch's whole history and not the
    raw vouchers again.

    A real sign-convention bug was caught by test before being trusted:
    `SalesDNRegisterRow.invoice_value` is a DERIVED, already-positive gross
    total (built from `abs()` of the voucher's other, non-party entries -
    see `build_sales_dn_register_row`), not the party's own raw signed
    entry the way `CreditNoteRegisterRow.cn_amount` and
    `ReceiptJournalRegisterRow.amount` are. Applying `flip_sign` to all
    three uniformly (the first version written) silently inverted every
    real sale into a negative movement - caught immediately by the
    existing test suite (10 pre-existing tests failed), fixed by only
    flipping CN/Receipt/Journal amounts and using Sales/DN's invoice_value
    as-is.

    Two old test fixtures (`fixtures/voucher_collection_sales.xml`,
    `voucher_collection_receipt.xml`) turned out to predate this app's own
    reliance on voucher-level PARTYLEDGERNAME and never carried that tag -
    real Tally exports always do (confirmed: 162 occurrences in the real
    `SalesReg.xml` sample, zero in these two). Register-building has
    always required it; this was invisible before because TB Cross-Check
    never depended on the registers. Fixed the fixtures to match real
    Tally shape, not the production code.

    End-to-end regression test added confirming the actual intended
    behavior change: a debtor-to-debtor Credit Note (the same real
    scenario fixed for Receipt/Journal in item 55, still excluded wholesale
    for Credit Note per that item's own note) now correctly produces
    `reconciled: False` for both parties it touches - under the old
    raw-voucher basis, the exact same scenario would have reconciled
    clean, hiding the fact that the CN never made it into any register.
    Full suite green (640 tests) at this point.

    **Phase 4/5 built and tested: the guided post-extraction workflow.**
    Decision on "new page vs extend the existing one" was the client's own
    call, left to ease-of-use: extended the existing Extract & Save /
    Manual Upload pages rather than building a separate wizard screen, so
    there's nothing new to learn - the pages a Maker already uses just do
    more automatically after they click the button.

    `test_extraction_save` now pulls Section 4.2's Sundry-Debtors-scoped
    voucher list live, per chunk, the same way the existing YTD drift
    check already does an extra pull per chunk - this was the real trigger
    this check needed, closing the gap flagged at the end of Phase 2
    without a separate manual button. Persisted, then compared against the
    branch's accumulated registers (`compute_ytd_vs_register_comparison`)
    right there in the same request.

    One combined banner - "Cross-Check: ALL CLEAR" or "DISCREPANCY
    FOUND" - covers both checks together, per the client's own explicit
    point: since both ultimately depend on the Registers, a real gap shows
    up in both at once, so there is one status to look at, not two to
    reconcile against each other. Below it, a "Before you move on" prompt
    links to Branch P&L Cross-Check and offers Yes/No: Yes posts straight
    to the existing Weekly Movement Register's own recording action
    (inline, no navigating away); No links to Register Exceptions Review
    as where to go look right now, pending the real correction actions
    (Phase 6, not yet built). Manual Upload gets the same combined banner
    and Yes/No block, but never claims a Check 1 result, since there is no
    live Tally to pull Section 4.2's voucher list from in that path -
    stated as an honest absence, not a silent 0 or a false "clear."

    A third instance of the exact same sign-convention bug (items 51/52's
    `abs()` bug, Phase 3's `invoice_value` bug, now this one) was caught by
    the test suite before being trusted: `compute_ytd_vs_register_
    comparison` compared `YtdDebtorVoucherRow.amount` (the party's own RAW
    entry) directly against `SalesDNRegisterRow.invoice_value` (a derived,
    already-positive gross total) with no sign reconciliation - every
    genuinely matching Sales/Debit Note voucher showed as a false mismatch
    until `flip_sign` was applied to the YTD side for just those two
    categories. The three-times-repeated shape of this exact mistake (two
    different "amount" fields on two different row types, assumed to share
    a convention because they're both called amounts) is itself worth
    remembering for any future work that compares across these registers.

    Full suite green (645 tests) at this point.

    **Phase 6 scope confirmed with the client** before building it: all
    three correction actions (Add/Delete/Modify) live on the Check 1
    screen itself (`/reports/ytd-debtor-vouchers`), never on a separate
    page. The Review Register (Register Exceptions Review) has no
    inclusion action and stays out of this - the client's own correction,
    verified against the actual code rather than taken on memory. The
    separate Drift Findings page/detector is retired as a destination
    (Check 1 is strictly more complete - it compares against the
    Registers directly, not just whether a voucher was ever logged) but
    its table and historical rows are never deleted, per this app's
    never-discard-data principle - only new findings stop being generated
    the old way. Check 1 and Check 2 are confirmed to stay as separate
    screens (that was already the real build, not a change), while the
    combined Phase 4/5 banner stays combined - both per the client's own
    review of a side-by-side mockup.

    **Phase 6a built and tested: "Missing from Registers" now feeds the
    Add action's backing data.** Rather than build a new detector, this
    reuses the existing, already-proven `DriftFinding` /
    `incorporate_drift_finding` mechanism: `ar_mis.ytd_debtor_cross_check.
    drift_findings_from_missing_registers` takes Check 1's own
    `MISSING_FROM_REGISTERS` rows plus the same live Sundry-Debtors
    voucher list Check 1 just pulled, looks each row's original `Voucher`
    back up by (branch_id, voucher_number, voucher_type), and builds a
    `DriftFinding` per party exactly the way the old `isolate_drift` did.

    Wired into `test_extraction_save`'s per-chunk loop in place of the old
    `isolate_drift` call, which pulled the WHOLE company's YTD vouchers a
    second time just to feed drift detection - that pull is now gone
    entirely. Check 1's own `sd_vouchers` (already Sundry-Debtors-scoped,
    already live, already fetched) is reused as the lookup source, so this
    chunk now makes one live YTD pull instead of two. Order matters here:
    Check 1's comparison must run first so there's a `MISSING_FROM_
    REGISTERS` list to feed in - the old isolate_drift block that used to
    run before Check 1's block is now gone, replaced by this, running
    after.

    Confirmed end-to-end, not just at the unit level: extended
    `test_test_extraction_save_flags_a_ytd_vs_register_mismatch_in_the_
    combined_banner` (which already simulates a voucher present in the
    live pull but missing from the Registers) to also assert a real
    `DriftFindingRecord` now lands in storage from that same save request,
    proving the wiring through the actual route, not just the function in
    isolation.

    Manual Upload's own `isolate_drift` call (`ar_mis.manual_upload.
    process_manual_upload`) is untouched for now - it has no live
    Sundry-Debtors pull to source `drift_findings_from_missing_registers`
    from yet; fixing that is Phase 6e's job (repointing the existing
    broken `ytd_vouchers` upload slot at Check 1), not this one's.

    Full suite green (648 tests) at this point.

    **Phase 6b built and tested: the audited Delete/Modify overlay.** New
    module `ar_mis.register_corrections` - deliberately NOT inside
    `ytd_debtor_cross_check.py` or `rollforward.py`, since both of those
    now depend on it: `RegisterRowCorrection` (EXCLUDE="Delete",
    CORRECT_AMOUNT="Modify", REINSTATE undoes either), a new append-only
    `register_row_correction` storage table (schema v15,
    `Store.record_register_row_correction`/`all_register_row_
    corrections`) - same never-UPDATE/DELETE discipline as drift_finding
    and pre_mis_adjustments, since the client's rejection of silently
    editing original voucher data applies exactly the same way here. The
    SAME voucher identity can legitimately be corrected more than once
    over time (no UNIQUE constraint, unlike drift_finding) -
    `effective_corrections` reduces a full history down to the latest row
    per identity, the same latest-wins reduction this app already uses
    for `all_ytd_debtor_voucher_rows`' own as_of scoping.

    A correction's identity is (branch_id, voucher_type, voucher_number,
    party_id) using the exact same five labels Check 1 already uses
    ("Sales"/"Debit Note"/"Credit Note"/"Receipt"/"Journal") - deliberately
    the EXACT label, never a type-group, so a correction can never
    silently leak across a sibling type sharing the same register (a
    "Receipt" exclude must never touch a "Credit Note" row that happens
    to share the same voucher_number+party). The one carve-out, not a
    bug: Check 1's own combined "Sales & DN Register" scope already
    treats Sales and Debit Note as one register (pre-existing, confirmed
    in this module's own docstring, nothing new here) - a correction
    tagged either label can affect that one combined dict, mirroring how
    the register itself was already structured before corrections
    existed. `aggregate_party_movements_from_registers` (Check 2), which
    DOES keep Sales and Debit Note as separate party-movement fields,
    scopes each correction to its own exact single-label frozenset
    instead, so it never mixes the two there.

    The genuinely shared, single-source-of-truth piece moved into this
    new module: the per-(branch, voucher_number, party) reduction
    (`sales_dn_amounts`/`cn_amounts`/`rj_amounts`) that collapses however
    many physical register rows/bill-allocation lines make up one voucher
    into one figure, in each register's own NATIVE (unflipped)
    convention - built once, used by both `compute_ytd_vs_register_
    comparison` (which already needed this) and, newly, `aggregate_
    party_movements_from_registers` (which previously summed row-by-row
    directly into party totals, with no per-voucher reduction step at
    all - safe before corrections existed, since there was nothing to
    apply per-voucher, but exactly the gap a Receipt/Journal correction
    would have fallen into: a CORRECT_AMOUNT replacing only ONE of
    several bill-allocation lines for the same voucher would have been
    wrong, either double-counting or partially applying the fix). Each
    caller still applies its OWN final-convention flip (or non-flip) on
    top, exactly as before - deliberately kept out of the shared module,
    so there remains only one place, ever, that decides whether to
    flip_sign a given field.

    `compute_ytd_vs_register_comparison`'s own EXTRA_IN_REGISTERS
    direction had to change its own iteration from walking the raw
    register rows to walking the CORRECTED amounts dict's own keys -
    an EXCLUDE-d key must never show up as "extra" (or anywhere else)
    once excluded, even though the original row is never touched in
    storage. Confirmed behavior-preserving for the empty-corrections case
    by running the full existing test suite unchanged before adding any
    new correction-specific test.

    Confirmed end-to-end: a correction recorded once now changes both
    checks' own computed totals identically (new cross-module test,
    `test_same_correction_is_consistent_across_check_1_and_check_2`) -
    the real point of this phase, not just "a correction compiles."

    Full suite green (678 tests) at this point.

    **Phase 6c built and tested: Add/Delete/Modify/Reinstate live on the
    Check 1 screen itself.** `ytd_debtor_voucher_report()` now reads
    `all_register_row_corrections()` back out of storage on every page
    load and threads them into `compute_ytd_vs_register_comparison`, so
    a correction recorded a moment ago is already reflected the next time
    anyone opens this screen - never a separate "apply" step. Per-row
    action, driven by `row.status`: Missing from Registers gets an "Add
    to Registers" form (reusing `drift_finding_incorporate` outright, not
    a second mechanism - see below); Extra in Registers gets "Delete";
    Amount Mismatch gets "Modify". New maker-only route
    `register_row_correction_submit` (`POST /reports/ytd-debtor-vouchers/
    correction`) handles Exclude/Correct Amount/Reinstate uniformly -
    branch_id/voucher_type/voucher_number/party_id/action/reason/
    corrected_by always required, corrected_amount required only for
    Correct Amount, redirects back to the same `as_of` the Maker was
    looking at.

    `drift_finding_incorporate` (Phase 2's own route, previously only
    reachable from the standalone Drift Findings page) gained a small,
    additive `return_to`/`as_of` hidden-field pair, read by a new
    `_drift_finding_redirect()` helper - when the Add form on THIS screen
    posts to it, the Maker lands back on Check 1, not on Drift Findings;
    every existing call site (the Drift Findings page itself) keeps its
    old behavior unchanged, since the field is simply absent there. No
    second "add a missing voucher" mechanism was built - Phase 6a's
    `drift_findings_from_missing_registers` already creates the exact
    `DriftFindingRecord` this form incorporates, looked up here by the
    same (branch_id, voucher_type, voucher_number, party_id) identity,
    never re-detected on page load.

    **A real design gap, caught before shipping, not after**: an Exclude
    on an "Extra in Registers" row removes that voucher's only remaining
    key from the register-side amounts - and since "Extra in Registers"
    means the YTD pull never had this key either, the row has NO side
    left to appear under at all once excluded. A per-row "Reinstate"
    button attached only to that now-vanished row is therefore dead code
    for the one case Delete actually exists for. Fixed by adding a
    durable **Corrections Log** section beneath the main table - every
    correction ever recorded, oldest at the bottom, each marked Active or
    Superseded (`latest_correction_id_by_key`, the same latest-wins
    identity `effective_corrections` uses, keyed off insertion order
    rather than object equality to stay correct even if two corrections
    happen to carry identical values) - Reinstate always lives there,
    never only inline on a row that might not exist to click on. Confirmed
    with a dedicated test asserting Delete removes a row from the visible
    comparison while it still shows, correctly, in the Corrections Log.

    Verified in a real browser-equivalent flow (not just the test client):
    started the dev server, seeded data via the Store API directly, and
    drove the Correct Amount action through actual HTTP requests - the
    mismatch cleared from the comparison table and the Corrections Log
    showed it Active, exactly as the unit/integration tests already
    asserted.

    New webapp tests: maker sees Delete/Modify, a Checker sees neither;
    Exclude/Correct Amount/Reinstate each round-trip through the real
    route; all four validation failures (missing voucher identity, bad
    action, missing reason/name, invalid corrected_amount); Add renders
    only when a backing DriftFindingRecord exists and links to the
    correct finding id; `drift_finding_incorporate`'s new redirect
    override.

    Full suite green (688 tests) at this point.

    **Phase 6d built and tested: the standalone Drift Findings page is
    retired.** Removed: the `drift_findings_report` route and its
    `drift_findings.html` template, the `drift_finding_acknowledge` route,
    the Reports Home nav card linking to it, and the now-fully-orphaned
    `Store.acknowledge_drift_finding` method (zero remaining callers once
    its one route was gone). Per this app's never-discard-data principle,
    nothing about the underlying DATA moved: the `drift_finding` table,
    every historical row in it (including any `acknowledged`/
    `acknowledged_by`/`acknowledged_at` set before this retirement), and
    `DriftFindingRecord`'s own fields for reading them back all stay
    exactly as they were - only the one-way street to SET a NEW
    acknowledgement is gone, since it had no remaining UI entry point.

    `drift_finding_incorporate` (Check 1's own Add action, Phase 6a/6c)
    is UNCHANGED in what it does - only its redirect target changed,
    from the now-deleted page to Check 1 itself, unconditionally (the
    `return_to` hidden field this needed while both pages coexisted is
    gone too, along with the conditional in `_drift_finding_redirect` -
    there is only one caller now, so there is only one destination).

    Tests updated to match: the dead-page assertions (`test_drift_
    findings_renders_with_no_data`, `..._shows_outstanding_finding_and_
    count`, `test_maker_can_acknowledge_a_finding`, `test_checker_can_
    view_but_not_acknowledge_drift_findings`, the storage-level
    `test_acknowledge_drift_finding`) were removed outright rather than
    patched around a feature that no longer exists; two new tests
    (`test_drift_findings_report_page_is_retired` - a 404; `test_reports_
    home_no_longer_links_to_drift_findings`) assert the retirement itself
    rather than leaving it unverified. `test_maker_can_incorporate_a_
    finding_and_reconcile_clean`'s assertions were adjusted to check the
    flash message and the persisted record directly instead of HTML the
    deleted page used to render (who incorporated a finding and when is
    still durably stored and queryable - `DriftFindingRecord.
    incorporated_by`/`incorporated_at` - it simply isn't re-displayed in
    any table any more, an accepted, deliberate trade for one less
    screen). `test_every_report_screen_has_a_back_to_reports_link`'s path
    list dropped `/reports/drift-findings` and gained `/reports/ytd-
    debtor-vouchers`, which hadn't been in that regression test at all
    until now.

    **A genuinely new finding, surfaced rather than silently acted on**:
    `isolate_drift` (the function Phase 6a already stopped calling from
    the webapp's `test_extraction_save`) turns out to have a THIRD call
    site this session hadn't tracked until now - `ar_mis.cli.run`, the
    standalone weekly batch script (Section 2.2), which still does its
    own separate live YTD pull and feeds `isolate_drift`'s findings
    straight into `evaluate_output_gate` as a gate-blocking signal. This
    is a safety-critical, batch-mode-only code path this whole session's
    work has not touched at all. Deliberately NOT rewritten here as a
    drive-by alongside the webapp's own retirement - migrating it to
    Check 1 the way the webapp was migrated is a real, separate task
    (does the CLI's own TallyClient instance get a `fetch_sundry_debtor_
    vouchers` call added, does the gate's "clean" definition change
    shape, is this batch script even still the client's live operational
    path now that the webapp has Extract & Save) that deserves its own
    attention, not a decision made in passing. `ar_mis.manual_upload.
    process_manual_upload`'s own `isolate_drift` call (Phase 6e's actual
    job) is the second, already-known remaining call site - so "stop
    calling isolate_drift anywhere" is NOT yet fully true after this
    phase, only "stop calling it from the webapp's live-extraction path,"
    which is the part that was actually in scope here.

    Full suite green (684 tests - four removed, two added, net -4 - since
    Phase 6d retires behavior rather than adding it) at this point.

    **Phase 6e built and tested: Manual Upload's YTD voucher file finally
    works, and genuinely feeds Check 1.** Investigating the broken
    `ytd_vouchers` slot started from `fixtures/real_samples/YTDData.xml`
    (the real capture already on file, confirmed by the earlier "CONFIRMED
    BUG" finding above as Tally's "Display Report" shape, `DSPVCH*` tags) -
    inspecting it directly (247 real rows, every field's actual values)
    surfaced something the earlier finding hadn't: `DSPVCHLEDACCOUNT`,
    which looked like it might be the debtor's own name, is actually the
    voucher's CONTRA ledger - a revenue ledger for a Sale, a bank for a
    Receipt, the other debtor's name for a journal reallocating between
    two tracked debtors (confirmed by cross-tabulating all 247 rows by
    voucher type: Sale/Receipt/Credit Note/Debit Note/Tax Invoice rows
    show only non-debtor ledgers there; only Journal rows show company
    names, and only because that leg happens to be another debtor). There
    is no field anywhere in this export identifying which specific
    Sundry Debtor a Sale/Receipt/CN/DN row's own leg belongs to.

    Raised as a real blocker before writing any code - this isn't a
    parsing bug fixable by reading the file more carefully, it's a
    genuine absence in the data as captured. Client's direct, correct
    pushback: Check 1 was never supposed to need party identity in the
    first place. The client's own prior Excel cross-check template
    (`AR_MIS_Template_26-27_Final.xlsb`, `Data_Validation` sheet) was
    shown live - its SUMIFS formula filters by branch, voucher type, and
    date range only (`'Voucher Details as per Books'!A:A,...B:B,...C:C,
    ...F:F,"Sales"`), no debtor-name column anywhere - and the client's
    own explicit statement: "date and voucher type and voucher number...
    There is no need of Debtor Ledger Name in here." Confirmed sound
    independent of the client's say-so too: Tally numbers a voucher
    uniquely per type per company, never per party, so (branch_id,
    voucher_type, voucher_number) was already a complete, unambiguous
    identity - party_id in the LIVE Check 1's own key was never load-
    bearing for uniqueness, only for splitting a multi-debtor voucher's
    own movement by party, which Check 1's "does this voucher exist"
    question never actually needed.

    Built on that corrected basis, never party-ful:
    - `ar_mis.models.ManualYtdVoucherRow` - branch_id, voucher_date,
      voucher_type, voucher_number, amount. No party_id field at all,
      unlike `YtdDebtorVoucherRow`.
    - `ar_mis.parsers.parse_manual_ytd_voucher_report` - the same
      positional-pairing technique `parse_ledger_closing_balances`
      already uses for the Trial Balance's DSPACCNAME/DSPACCINFO pair,
      extended to six flat sibling tags per voucher (DSPVCHDATE/
      DSPVCHLEDACCOUNT/DSPVCHTYPE/DSPVCHDRAMT/DSPVCHCRAMT/
      DSPEXPLVCHNUMBER, confirmed exactly this repeating shape against
      the real sample - 247 complete 6-tag groups). `amount` is DR+CR
      net (exactly one is normally populated; a debtor-to-debtor journal
      populates both on one row, and netting them correctly collapses a
      balanced inter-debtor transfer to zero at this party-less
      granularity - confirmed against the real sample's own Journal
      rows, most of which net to exactly 0.00, with the genuine non-zero
      ones all confirmed to have an external, non-debtor contra ledger).
      CONFIRMED against the real sample: the file ends with one
      INCOMPLETE trailing group (a closing/adjustment line like
      "Unadjusted Forex Gain/Loss" with a blank DSPVCHTYPE and no
      DSPEXPLVCHNUMBER at all) - never a real voucher, always skipped.
    - `ar_mis.ytd_debtor_cross_check.compute_manual_ytd_vs_register_
      comparison` / `ManualYtdVsRegisterRow` - a parallel, coarser Check 1
      for this one input, identity (branch_id, voucher_type,
      voucher_number) only. Same sign-convention handling as the live
      comparison (flip_sign on the YTD side for Sales/Debit Note only);
      the register side's own per-voucher reduction drops party_id from
      the key too, so two different debtors' own register rows that
      happen to share a voucher_number (exactly the journal-split case)
      sum together, matching the YTD file's own already-netted figure
      for that voucher - not a precision loss, the correct comparison at
      this granularity.

    `ar_mis.manual_upload.process_manual_upload` now calls this instead
    of the old `isolate_drift` path; `ManualUploadResult.drift_findings`
    is gone, replaced by `ytd_vs_register_rows`. Never persisted to the
    `ytd_debtor_voucher` table or shown on `/reports/ytd-debtor-vouchers`
    - that table's schema requires a party_id this input can't supply -
    shown once, inline, on the Manual Upload result page only, the same
    way `drift_findings` was only ever shown there before. The combined
    Cross-Check banner (`_post_save_workflow.html`) now factors this in:
    `overall_reconciled` is false if either Check 2 (party reconciliation)
    or this new Check 1 shows a mismatch, matching the live path's own
    combined-banner logic from Phase 4/5.

    Tested against the real `YTDData.xml` sample throughout (not just
    synthetic fixtures) - the parser test suite confirms the exact
    247/162/46/34/4/1 row and per-type counts, and a full webapp-route
    smoke test posts the real file through `/manual-upload` end to end.

    Full suite green (698 tests) at this point.

    **Not done here, flagged, not silently skipped**: `ar_mis.manual_
    upload`'s old `isolate_drift` import is gone, so of the three
    `isolate_drift` call sites known as of Phase 6d, two are now retired
    (the webapp's live path, Phase 6a; Manual Upload, this phase) and one
    remains - `ar_mis.cli.run`, the standalone weekly batch script, still
    outside this build order pending the client's own call on it.

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
