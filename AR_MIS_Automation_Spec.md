# AR MIS — Tally Extraction & Automation Pipeline
**Spec for build handoff — v1**
**Client context:** Logistics company, CHA and Transportation verticals, 6–7 branches, each maintained as a separate Tally company under one HQ. Centralized accounting access from Kolkata. LAN-only environment, no cloud dependency.

---

## 1. Why this exists

The current AR MIS Dashboard (Excel, manually populated weekly by client's team from Tally exports + macro) has produced repeated, material data-integrity failures:

- Frozen/duplicate weekly figures (Total AR, Unapplied Cash unchanged for 2–3 consecutive weeks)
- Collection Efficiency Index running negative for 8+ weeks, then snapping to a fixed value — sign-convention bug
- Pre-MIS Outstanding (meant to be a fixed one-time legacy balance) jumping ₹13+ crore in a single week — current-year invoices being misclassified into it
- Total AR extracted incorrectly from books due to manual, inconsistent sign-handling of customer advances
- Cross-tab inconsistencies within the same workbook: the 91–180 day bucket sub-split does not sum to the combined 91–180 bucket shown elsewhere (~₹2.24 cr gap observed); the >180-day "Bad Debt Risk" KPI disagrees with the Ageing Matrix and Movement Analysis tabs for the same date (~₹1.10 cr gap observed)
- A "Rounding Difference" plug of ~₹5.17 lakh — materially too large to be labeled as rounding

**Root cause:** manual transcription, manual sign-flipping, and manual bucket classification by the client's team, with no enforced validation gate — checks exist but are not consistently reviewed.

**Objective of this build:** remove manual transcription and classification from the pipeline entirely. Replace with a Python-based extraction and computation layer that pulls directly from Tally, applies deterministic rules, and refuses to publish when validation fails. Excel/reporting layer remains, fed by clean, validated data instead of hand-keyed figures.

---

## 2. Extraction layer

### 2.1 Method
- Use Tally's **XML/HTTP request-response interface** (not ODBC) uniformly across all five voucher types: Sales, Credit Note, Debit Note, Receipt, Journal.
- Reason: ODBC's default collections expose flat master/ledger data well, but do not flatten nested bill-wise allocations inside Receipt and Journal vouchers. XML export serializes Tally's full internal object hierarchy, including `BILLALLOCATIONS.LIST`, and is the only reliable method for allocation-level detail. Use one method for everything rather than mixing ODBC and XML.
- Language: Python. Build the request/response parser fresh — the client's existing process (manual Excel export + VBA macro) is being fully replaced, not upgraded. No existing extraction code to build on.

### 2.2 Multi-company orchestration — human-in-the-loop by design
- Tally's interface only serves whichever company is currently loaded. Fully unattended sequential company-switching across 6–7 branches was evaluated and rejected as the highest-risk, least-testable part of the pipeline (multi-user Tally, branch staff concurrently active, no one present if a load hangs or the wrong company is loaded).
- **Design:** a firm staff member (not client's team) runs this weekly, Monday, at a scheduled time, using access/credentials specifically for this purpose.
- Tool prompts: "Open [Branch X] in Tally, then continue."
- **Before extracting, the tool must programmatically query Tally to confirm the currently loaded company name matches the expected branch.** Halt with a clear error if it doesn't. Do not rely on the operator confirming this themselves.
- On completion of each branch, tool shows a binary PASS/FAIL validation readout (Section 4) before prompting to move to the next company. Operator's only decision: green → proceed, red → stop and escalate. No interpretive judgment calls.
- Expected duration: half a day for all branches when run correctly (confirmed by client); tool should not assume less.

### 2.3 Sign convention
- Confirmed: Tally's XML `LEDGERENTRIES.LIST` amount is signed at source (debit entries negative, credit entries positive, per `ISDEEMEDPOSITIVE`/`AMOUNT`).
- **Apply one uniform rule at ingestion, identically across all voucher types: multiply every extracted amount by −1.** Result: Dr = positive, Cr = negative, matching the firm's reporting convention.
- Do not apply per-voucher-type sign logic. The flip is the only sign transformation in the pipeline.

### 2.4 Roll-forward formula
With the uniform sign flip applied, the party-level movement formula is **pure addition** — no hardcoded per-type direction:

```
Closing Balance = Opening Balance + Sales + Credit Notes + Debit Notes + Receipts + Journals
```

All figures post-sign-flip. This handles Journals correctly in either direction (a journal can legitimately increase or decrease a debtor's balance) without special-casing.

### 2.5 Opening balance
- Opening Balance = **Pre-MIS Outstanding**, a one-time fixed baseline set once at the point this MIS architecture went live, per party per branch.
- **Never re-pulled or recalculated weekly.** This field must not move except through explicit, logged adjustment (collection/write-off of legacy balance), never through the weekly extraction process.

---

## 3. Data model (existing architecture — extraction feeds into this)

Per prior project architecture, unchanged by this build:

1. **Layer 1 — Static Customer Master:** party reference data + one-time Pre-MIS Outstanding field.
2. **Layer 2 — append-only Weekly Snapshot:** party-level movement bridge (Opening + Sales + CN + DN + Receipts + Journals = Closing), one row per party per week.
3. **Layer 3 — append-only Action Log:** PTP commitments, decisions, owners, escalation status, logged week-over-week.
4. **Weekly Review layer:** pivoted, one-row-per-party wide view built from Layers 1–3, for live meeting use — never the raw append tables directly.

This build's job: populate Layers 1–2 automatically and correctly, replacing manual entry.

---

## 4. Validation — enforced gates, not passive checks

All of the following must **block report generation** on failure, not merely display a number for someone to notice.

### 4.1 Party-level reconciliation (primary control)
- Compute and compare **at individual party level**, not total or branch level. Total/branch-level-only checks are structurally blind to offsetting errors (Party A overstated, Party B understated, net zero at total) — this is the exact failure class that caused the original Pre-MIS corruption.
- Branch-level and company-level figures are **roll-ups of the party-level check for review**, not the check itself.
- Formula: extracted YTD Trial Balance closing balance per party per branch, vs. Opening + Sales + CN + DN + Receipts + Journals computed from the appended registers for the same party. Tolerance: **[TBD — Saurabh to confirm absolute ₹ or % threshold before build]**.

### 4.2 YTD full-pull cross-check
- In addition to incremental weekly extraction, pull the full Sundry Debtors group from **day 1 of the financial year** on each run.
- This catches backdated entries an incremental pull would miss.
- **Source-of-truth rule (confirmed): the YTD Tally pull always wins.** If the appended weekly build disagrees with the YTD pull, the appended build is treated as the one with the error. System should isolate and surface *which week and which voucher* caused the drift — not present two conflicting totals for a human to arbitrate.
- This check should retire the old total-AR-level version once the party-level check (4.1) is live — do not run both indefinitely.

### 4.3 Per-branch extraction failure handling
- **[TBD — Saurabh to confirm]**: if one branch's extraction fails or is incomplete mid-run, does the tool skip that branch (flagging it clearly) and continue, or halt the entire weekly cycle?

### 4.4 Output gate
- If any Section 4 check fails, the weekly report/email does not go out automatically. Either it is blocked pending manual sign-off, or it ships carrying an explicit "UNVALIDATED — reconciliation failed" flag. Never a silent pass-through.

---

## 5. Rollout

- **Parallel run required.** New pipeline runs alongside the existing manual dashboard for a defined validation period before the client's team is removed from the process and before output goes to the CFO unsupervised.
- Client's team is being removed from data preparation entirely; the firm's own staff take over weekly extraction (cost already absorbed — office/infrastructure access confirmed, no separate scope/fee renegotiation needed).

---

## 6. Reporting / CFO-facing output scope

- **One consolidated tool, seven branch data sources feeding one dashboard** (not seven standalone reports).
- CFO view must show, together:
  - Company-level consolidated position
  - Branch-level reconciliation and ageing
  - Branch-and-party-level ageing and reconciliation detail (same party can span multiple branches — needs both a consolidated cross-branch figure per party and a per-branch breakup)
  - Exceptions reported as a separate section, not blended into the main figures
  - Weekly movement/trend (extends existing Movement Analysis concept — must not have blank/missing weeks; historical gaps in the current file are a defect to eliminate, not carry forward)
  - PTP Register, with the ability to record and save whether each PTP entry is currently active/in use or has broken/lapsed
- **[TBD — Saurabh to confirm]**: does the CFO consume this as a live/interactive dashboard, or a static weekly-snapshot report (matching the confirmed weekly refresh cadence) with drill-down as a secondary reference for AR Managers rather than the CFO's primary view?
- **[TBD — Saurabh to confirm]**: PTP status — is this a status flag per individual PTP entry (Active / Kept / Broken), consistent with the existing Action Log design, or a different tool-level toggle? Current assumption is per-entry status, consistent with Layer 3 above.

---

## 7. Explicitly out of scope for this build

- Fixing or reconciling the current Excel dashboard's existing figures — it is being replaced, not repaired.
- Cross-branch ledger-name standardization — not needed; each branch's Tally company is queried independently by its own party/ledger identifiers, and consolidation happens by party name/ID matching at the reporting layer, not by ledger-naming convention.
- Full unattended automation of company-switching — explicitly rejected in favor of the human-in-the-loop design in Section 2.2.

---

## 8. Open items to resolve before or during build

1. Tie-out tolerance threshold for the party-level reconciliation (Section 4.1) — absolute ₹ figure or percentage.
2. Per-branch extraction failure behavior — skip-and-flag vs. halt-entire-cycle (Section 4.3).
3. CFO consumption mode — live dashboard vs. static weekly snapshot (Section 6).
4. PTP status granularity — per-entry vs. tool-level (Section 6).
