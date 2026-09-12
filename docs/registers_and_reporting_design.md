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
spanning every branch, appended to on every extraction run or manual upload:

**Sales & Debit Note Register** — one row per invoice (DNs included here
since both are receivables). Columns agreed so far:

| Column | Notes |
|---|---|
| Branch | |
| Date | |
| Customer Name | |
| Group (Sundry Debtor / Related Party) | Set manually per party; see item 5 |
| Taxable Value | |
| CGST | Needs sample XML to confirm tax-ledger naming — see Open Items |
| SGST | |
| IGST | |
| Total Invoice Value | |
| Voucher Number | The human-facing invoice number, printed/sent to customer |
| Bill Allocation Reference ("New Ref") | The *actual* Tally field receipts/CNs match against internally — see item 8, this is NOT always equal to Voucher Number |
| CN Ref No. | The CN number if exactly one CN against this invoice; "Multiple credit notes issued" if more than one |
| CN Amount | Sum of all CNs against this invoice (rollup from CN Register) |
| Receipt Amount | Sum of all receipts against this invoice (rollup from Receipt & Journal Register) |
| Open Amount | Total Invoice Value − CN Amount − Receipt Amount |
| Due Date | Invoice Date + that customer's Credit Period (see item 6) |
| Status | Open / Closed |
| Due Days | Only meaningful when Open |
| Ageing Bucket | Derived from Due Days |
| PTP Date | **Editable** — see item 7 |
| PTP Amount | **Editable** — see item 7 |

**Credit Note Register** — voucher-wise, all branches combined, date-wise.
Columns: Branch, Date, Customer, CN Number, Original Invoice Ref, Total CN
Value. No tax split needed here (client's explicit instruction).

**Receipt & Journal Register** — voucher-wise, all branches combined,
bill-allocated. Columns: Branch, Date, Customer (from the receipt/journal
entry itself), Voucher Type, Voucher Number, Invoice Ref it's allocated
against (or "Unapplied"), Total Amount, **Classification** (Current / Pre-MIS
Adjustment / Pending Review — see item 9). No tax split needed here either.

A voucher/journal can allocate against a specific invoice ("Agst Ref") or be
left unapplied ("New Ref" / on-account) — this applies to Journals exactly
like Receipts, not just Receipts (any voucher type can carry a bill
allocation if the ledger has bill-by-bill tracking on).

### 3. PTP stays inside the Sales & DN Register — not a separate editable register

PTP Date/Amount are edited directly against the relevant invoice row by the
AR team (who filter the register to Open invoices and fill them in). A
separate "PTP Report" is just a filtered extract of rows where PTP Date/
Amount are populated — a view, not a second data-entry surface.

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
+ that specific customer's credit period.

### 7. Editable fields — general principle

**Extracted fields (from Tally) and computed fields must never be freely
editable.** This was pushed back on hard: making everything editable
recreates the exact defect class this tool exists to eliminate (bucket
sub-splits not summing to totals, Bad Debt Risk disagreeing with the Ageing
Matrix — the original six defects named in the top-level spec/README).

Fields that ARE legitimately editable, because there's no other source of
truth for them:
- PTP Date, PTP Amount (Sales & DN Register)
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
register table above.

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
- New column on the Receipt & Journal Register: **Classification** — Current
  / Pre-MIS Adjustment / Pending Review — set per voucher, all lines of that
  voucher share the same value.

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

## Open items — bring next session

1. **Sample XML files** (all five voucher types) — needed to confirm real
   tax-ledger naming (for CGST/SGST/IGST parsing) and the actual
   `BILLALLOCATIONS.LIST` shape (New Ref, BILLTYPE, due date field if
   present) against a real export rather than assumptions.
2. **Client's current column headers** — cross-check against the register
   designs above; reconcile any differences.
3. **PTP register columns** — dedicated review (rough shape assumed so far:
   Branch, Customer, Invoice Ref, PTP Date, PTP Amount, status
   Active/Kept/Broken — needs confirming against how the AR team actually
   works today).
4. **Validate the Pre-MIS Classification/exception logic** (item 10) against
   real data once the sample XML is available.
5. **Existing AR MIS Dashboard's current report list** — client to share
   what's currently produced (Exception Register, a single-snapshot "AR
   Control Board", Weekly Movement Register, and others) so nothing already
   relied upon gets dropped or missed in the redesign.
6. **Live Tally connectivity / hosting question — parked, not resolved.**
   Client's Tally setup may involve a third-party "Tally on Cloud" style
   host running multiple companies' Tally instances on shared infrastructure
   reachable over the public internet. Real security question (Tally's
   gateway has no authentication of its own) — client is checking the
   hosting arrangement themselves (dedicated VM per tenant vs. shared
   Windows Server session) before this is discussed further. See prior
   session's discussion for the full reasoning; nothing to build here yet,
   and this should not be assumed resolved just because it's parked.

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
