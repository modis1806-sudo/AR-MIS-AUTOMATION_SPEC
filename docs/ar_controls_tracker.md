# AR Internal Controls Tracker

Working list from the "what's missing to make this a control tool, not an
overpriced ageing system" discussion. Update this file *before* starting the
next item, not after — that's the whole point of it existing separately from
the design doc's narrative log (`registers_and_reporting_design.md`), which
records *why* a decision was made, not *what's left*.

Status values: **Done** / **Pending** / **Deferred** (with the reason and who
made that call).

## AR-process / performance controls

The client's own priority category — is the AR function measurably
performing and accountable — ranked above IT/data governance controls
specifically because this tool is a secondary reporting layer, not the
system of record.

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | AR Concentration Risk | **Done** | Reports → AR Concentration Risk. Top 5/10/20 by outstanding, % of total AR. |
| 2 | Credit Limit | **Done** | Customer Master, editable, defaults to Rs 1,00,00,000. Reference only — no breach report yet (see #8). |
| 3 | Accountability by owner | **Pending** | PTP already carries an `owner` field; needs a rollup view (kept rate, count) by owner. **Next up.** |
| 4 | Targets/benchmarks + variance | **Pending** | DSO, Collection Efficiency, PTP Kept Rate are all raw actuals today — no benchmark to hold them against. Needs a decision on where a target lives (per branch? company-wide? who sets it?) before building. |
| 5 | Ageing-bucket-transition escalation | **Pending** | Needs per-party ageing history to detect "moved to a worse bucket since last week" — doesn't exist as stored data yet (only portfolio-wide history does). Real prerequisite, not just a screen. |
| 6 | Dispute/hold classification | **Pending** | An open invoice is either paid or not today — no way to flag "customer is disputing this" distinct from "hasn't paid yet." Most consequential to design right, since it changes what counts in ageing/DSO downstream. |
| 7 | Next Action due date | **Pending** | Receipt/Journal register's free-text "Next Action" has no due date attached. Check against the existing `expected_collection_date` field first — may already partly cover this. |
| 8 | Credit Limit breach report | **Pending** | Follow-on to #2 — the limit exists but nothing yet reports who's currently over it. |
| 9 | Provisioning tied to ageing | **Deferred** | Client's own call — raised, not rejected. Would compute what should actually be provisioned per ageing bucket against an adopted policy; ageing itself is already shown. |

## IT / data governance controls

Real gaps, correctly reframed by the client as generic software hygiene
rather than AR-specific controls — lower priority than the list above for
that reason, not rejected.

| # | Item | Status | Notes |
|---|------|--------|-------|
| 10 | Real authentication | **Pending** | Maker/Checker is a session toggle, not a login — every `reviewed_by`/`adjusted_by`/`incorporated_by` field is a free-text box. The gap everything else here depends on. |
| 11 | Maker-proposes/Checker-approves gate | **Pending** | Currently a viewing-permission split, not an approval gate — a Maker's action is final the instant they click. |
| 12 | Monetary approval threshold | **Pending** | No second signature required on anything, including a write-off. |
| 13 | Bank-statement tie-out | **Pending** | A Receipt voucher is trusted at Tally's own word; nothing confirms it hit a real bank account. |
| 14 | Period locking | **Deferred** | Client's own call — revisit after roughly 3 months of live operation. |
| 15 | Encryption / backup / DR for the SQLite file | **Pending** | No documented backup story beyond the automatic pre-migration copy; no encryption at rest. |

## Already verified — not gaps

- **Altered-voucher detection.** Client's own question, checked rather than
  assumed: `resolve_opening_balances` anchors each week's opening to the
  *prior week's own recorded* `closing_extracted`, so an alteration to an
  already-reconciled voucher necessarily produces a fresh mismatch on the
  next cycle's TB Cross-Check. Confirmed working, not a gap.
- **Related-party AR segregation.** AR Snapshot already shows Related Party
  AR separately from Sundry Debtor AR.
- **PTP tracking.** Already built (Active/Kept/Broken per entry, Kept Rate
  on AR Snapshot).

## How to use this file

1. Before picking up a new item, mark it **In Progress** here first (this
   file, then the code).
2. When shipped, flip it to **Done** with a one-line pointer to where it
   lives (screen name, module).
3. Cross-reference the design doc's own numbered decision log for *why* —
   this file only tracks *what's left*, not the reasoning.
