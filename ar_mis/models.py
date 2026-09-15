"""Core data model.

Mirrors the spec's Section 3 architecture:
  Layer 1 - static customer master (+ one-time Pre-MIS Outstanding)
  Layer 2 - append-only weekly snapshot (party-level movement bridge)
  Layer 3 - append-only action log (PTP commitments + status)

Extraction-side types (Voucher, LedgerEntry) are the raw shape a Tally XML
response is parsed into, before sign-flip and roll-forward are applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class VoucherType(str, Enum):
    SALES = "Sales"
    CREDIT_NOTE = "Credit Note"
    DEBIT_NOTE = "Debit Note"
    RECEIPT = "Receipt"
    JOURNAL = "Journal"


class PTPStatus(str, Enum):
    ACTIVE = "Active"
    KEPT = "Kept"
    BROKEN = "Broken"


class PartyGrouping(str, Enum):
    """Section 6 (registers design) item 5 — set manually per party, never
    inferred. A party with no grouping yet is represented by the absence
    of a CustomerMasterRecord.grouping value (None), not a third enum
    member — "unclassified" is a data-entry gap to close, not a category.
    """

    SUNDRY_DEBTOR = "Sundry Debtor"
    RELATED_PARTY = "Related Party"


class RegisterClassification(str, Enum):
    """Registers design items 9/10 — set per voucher (all lines of that
    voucher share one value), never per individual ledger line. CURRENT
    is the default; PENDING_REVIEW means an "Against Ref"/"Advance"
    allocation didn't match any invoice this system tracks and a human
    must say whether it's a Pre-MIS-era reference or a typo (item 10).
    """

    CURRENT = "Current"
    PRE_MIS_ADJUSTMENT = "Pre-MIS Adjustment"
    PENDING_REVIEW = "Pending Review"


class NoteType(str, Enum):
    """Registers design item 2 — the Sales & Debit Note Register combines
    both voucher categories in one register, so each row needs to say
    which one it actually is."""

    INVOICE = "Invoice"
    DEBIT_NOTE = "Debit Note"


@dataclass(frozen=True)
class LedgerEntry:
    """One bill-wise allocation line from BILLALLOCATIONS.LIST (or a plain
    ledger entry when no bill allocation exists), already carrying the raw
    Tally-signed amount. Sign-flip is applied downstream in ar_mis.sign, not
    here — this type is the untouched extraction result.

    `bill_type` is Tally's own BILLTYPE field ("New Ref" / "Agst Ref" /
    "Advance", confirmed against real exports) — the registers design's
    item 8 fix (matching on Party + bill_name, i.e. the Bill Allocation
    Reference) needs this to tell an allocated reference from an unapplied
    one; "Advance" is treated the same as "Agst Ref" for matching purposes
    (confirmed against real data — it still carries a real bill reference,
    just tagged differently because the money arrived before the invoice
    existed) rather than as its own special case.
    """

    party_ledger_name: str
    amount_as_extracted: Decimal  # Tally sign convention: Dr negative, Cr positive
    bill_name: str | None = None
    bill_type: str | None = None


@dataclass(frozen=True)
class Voucher:
    """`voucher_type` is the CATEGORY this voucher was matched into
    (Section 2.1's five categories), not necessarily Tally's literal
    VOUCHERTYPENAME - real Tally deployments commonly customize voucher
    type names with prefixes/suffixes (e.g. "Sales - Export") that still
    belong to one of the five categories. `raw_voucher_type_name` keeps
    the original name Tally reported, for audit/debugging.

    `party_ledger_name` is Tally's own voucher-level PARTYLEDGERNAME -
    which specific ledger among `entries` is "the customer", as opposed
    to a revenue/tax/round-off line. This is genuinely needed, not
    redundant with each entry's own `party_ledger_name`: a Sales voucher's
    entries include the customer AND several revenue/tax ledgers, each
    with their own (different) `party_ledger_name` value naming *that*
    ledger - there is no other reliable way to tell which entry is the
    customer without this voucher-level field (see ar_mis.registers,
    which needs exactly this split to compute Taxable Value).
    """

    voucher_type: VoucherType
    voucher_date: date
    voucher_number: str
    branch_id: str
    party_ledger_name: str = ""
    entries: list[LedgerEntry] = field(default_factory=list)
    raw_voucher_type_name: str = ""


@dataclass(frozen=True)
class Party:
    party_id: str  # stable identifier used for cross-branch consolidation
    party_name: str
    branch_id: str


@dataclass(frozen=True)
class CustomerMasterRecord:
    """Layer 1. `pre_mis_outstanding` is the fixed baseline (Section 2.5) —
    set once, changed only via a logged PreMisAdjustment, never by the
    weekly extraction/ingestion path.

    `grouping` (registers design item 5) is set manually per party — None
    for a party not yet classified (auto-created new parties start here;
    surfaced for the user to resolve, never guessed). `credit_period_days`
    (item 6) defaults to 30 and is overridable per party; a change here
    only affects invoices created after the change — it never
    retroactively recalculates an already-computed Due Date.
    """

    party_id: str
    party_name: str
    branch_id: str
    pre_mis_outstanding: Decimal
    grouping: PartyGrouping | None = None
    credit_period_days: int = 30


@dataclass(frozen=True)
class PreMisAdjustment:
    """An explicit, logged, one-off change to a party's Pre-MIS Outstanding
    baseline (e.g. legacy-balance write-off or collection). Never produced
    by the weekly pipeline — only by a deliberate manual action.
    """

    party_id: str
    branch_id: str
    amount: Decimal  # signed delta applied to pre_mis_outstanding
    reason: str
    adjusted_by: str
    adjusted_at: date


@dataclass(frozen=True)
class WeeklySnapshotRow:
    """Layer 2. One row per party per branch per run. All amounts are
    post-sign-flip. `closing_extracted` is the YTD Trial Balance closing
    balance pulled directly from Tally for this party; `closing_computed`
    is Opening + Sales + CN + DN + Receipts + Journals. Section 4.1
    compares the two — this row carries both so the reconciliation result
    is reproducible from stored data alone, not recomputed silently later.

    `week_ending` is the run's own end date — no longer required to fall
    on a Sunday (client's explicit reversal: extraction runs on exactly
    the range asked for, never snapped to a calendar week - see
    ar_mis.config's own docstring). `period_start` is that same run's own
    start date, needed to recover exactly which date range this run
    covered (Store.delete_branch_week's register-row cleanup depends on
    it) now that it can no longer be derived by subtracting 6 days from
    week_ending. Optional/None only for rows that predate this field
    existing at all - see storage.py's own migration for how those are
    backfilled.
    """

    party_id: str
    branch_id: str
    week_ending: date
    opening: Decimal
    sales: Decimal
    credit_notes: Decimal
    debit_notes: Decimal
    receipts: Decimal
    journals: Decimal
    closing_computed: Decimal
    closing_extracted: Decimal
    reconciled: bool
    difference: Decimal
    period_start: date | None = None


@dataclass(frozen=True)
class WeeklyMovementRow:
    """Design doc item 15's Weekly Movement Register — one row per
    week-ending date, portfolio-wide (every branch combined, matching
    Total AR's own scope on the AR Snapshot dashboard). Append-only
    stored history (Open Item 4's resolution): a Maker explicitly
    records the current position as a given week's row, and it is kept
    exactly as computed forever after — never silently recomputed, even
    if later corrections would change the answer. `recorded_at` is the
    real wall-clock moment this row was recorded, distinct from
    `week_ending` (the business period it represents) for the same
    reason extraction_log keeps that distinction.

    The KPI fields mirror a subset of ar_mis.dashboard.ARSnapshot exactly
    — this row is that dashboard's own numbers, persisted at a point in
    time, not a separately-computed figure.
    """

    week_ending: date
    recorded_at: datetime
    total_ar: Decimal
    open_ar_by_fy: dict[str, Decimal]
    pre_mis_outstanding: Decimal
    overdue_ar: Decimal
    overdue_by_bucket: dict[str, Decimal]
    dso: Decimal | None
    collection_efficiency: Decimal | None
    unapplied_cash: Decimal


@dataclass(frozen=True)
class PTPEntry:
    """Layer 3 creation record — immutable once logged."""

    ptp_id: str
    party_id: str
    branch_id: str
    created_week: date
    promised_amount: Decimal
    promised_date: date
    owner: str
    notes: str = ""


@dataclass(frozen=True)
class PTPStatusLogRow:
    """Layer 3 append-only status history for a PTPEntry. The current
    status for a PTP as of a given week is the latest row for that
    ptp_id with week_ending <= that week.
    """

    ptp_id: str
    week_ending: date
    status: PTPStatus
    updated_by: str
    notes: str = ""


# ---------------------------------------------------------------------
# Registers (docs/registers_and_reporting_design.md) — invoice/voucher-
# level master registers, the confirmed replacement for weekly_snapshot
# as the base of reporting (design doc item 1). These types deliberately
# store only facts that are independent and non-derivable; every rollup
# or aggregate (Linked CN Amount, Receipts Applied, Open Amount, DPD,
# Ageing Bucket, PTP Status, invoice-fin-year-of-application) is computed
# live in ar_mis.registers from these raw rows, filtered to a selected
# as-of date (item 14) — never stored as a mutable/redundant field that
# could drift out of sync with the facts it's derived from. The one
# deliberate exception is InvoiceFollowUp below, which is genuinely
# editable human input with no other source of truth (item 7).
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SalesDNRegisterRow:
    """One row per Sales invoice or Debit Note (design doc item 2) — the
    two share a register because both are receivables. Row identity is
    Branch + Voucher Number + Party (+ Date as a safety check) — never
    `bill_allocation_reference` (item 8's scoping note: that field is a
    lookup used only when a receipt/CN needs to find this row, not an
    identity key for this register itself, since it can collide across
    parties in real Tally data).

    `bill_allocation_reference` is Tally's own BILLTYPE=New Ref name for
    this invoice's own bill (confirmed against real data to sometimes
    differ from `voucher_number` - a duplicated-invoice data-entry
    mistake left the wrong bill reference on a real client invoice, which
    is exactly why matching must use this field, not voucher_number).

    `due_date` is computed once from the party's Customer Master credit
    period *at the moment this row was created* and frozen from then on
    (item 6) — a later Customer Master change never retroactively
    recalculates it. Tally's own real per-bill BILLCREDITPERIOD field
    (confirmed present in real exports) is deliberately NOT used as an
    alternative source for this - the client's explicit decision.

    `taxable_value`/`cgst`/`sgst`/`igst` are computed by classifying this
    invoice's own ledger entries: `cgst`/`sgst`/`igst` come only from
    ledger entries whose name is an EXACT match on "CGST"/"SGST"/"IGST"
    (confirmed against real data that many revenue ledgers are also
    "_GST"-suffixed, e.g. "Road Transport Services_GST_18%" - a substring
    match would wrongly catch these); everything else non-tax and
    non-party sums into `taxable_value`, with "Round Off" (confirmed
    present on real invoices) folded into `invoice_value` after tax
    rather than into `taxable_value` - see ar_mis.registers for the exact
    split logic. Confirmed with the client this session: this is the
    correct, permanent treatment (not provisional) - Round Off is a
    total-level adjustment to the invoice's grand total exactly as Tally
    itself treats it, never part of the GST-relevant Taxable Value. Kept
    as its own visible field (`round_off`) rather than silently absorbed
    into `invoice_value` with no trace, so the TB Reconciliation
    Cross-Check sheet's workings are auditable line by line.

    `round_off` defaults to 0.00 (not required) so every existing caller
    that builds this row directly - test fixtures included - keeps
    working unchanged; only code that cares about a nonzero Round Off
    needs to pass it explicitly.
    """

    branch_id: str
    invoice_date: date
    note_type: NoteType
    voucher_number: str
    bill_allocation_reference: str
    party_id: str
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    invoice_value: Decimal
    due_date: date
    job_id: str | None = None
    round_off: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class CreditNoteRegisterRow:
    """One row per Credit Note (design doc item 2). `bill_allocation_reference`
    is None when Tally's own BILLTYPE is "New Ref" (genuinely on-account/
    unapplied, mirroring the Receipt & Journal Register's same concept —
    item 9 extended to CN). When present, it's matched against a tracked
    SalesDNRegisterRow via (party_id, bill_allocation_reference) - item 8's
    composite key, never voucher_number alone.

    `classification` is set once per voucher (never per line) — see item
    10: an "Agst Ref" reference that doesn't match any tracked invoice is
    PENDING_REVIEW until a human says whether it's a Pre-MIS-era
    reference or a data-entry typo; confirmed as Pre-MIS routes through
    Store.record_pre_mis_adjustment(), not into this register's own
    unapplied total.
    """

    branch_id: str
    cn_date: date
    voucher_number: str
    party_id: str
    cn_amount: Decimal
    bill_allocation_reference: str | None = None
    classification: RegisterClassification = RegisterClassification.CURRENT
    reason: str = ""


@dataclass(frozen=True)
class ReceiptJournalRegisterRow:
    """One row per bill-allocation line of a Receipt or Journal voucher
    (design doc item 2) — a voucher/journal can allocate against a
    specific invoice ("Agst Ref"/"Advance", both treated identically for
    matching per LedgerEntry.bill_type's docstring) or be left unapplied
    ("New Ref"), and this applies to Journals exactly like Receipts.

    `target_doc_no` is None when unapplied. When present, it's matched
    against a tracked SalesDNRegisterRow the same way CreditNoteRegisterRow
    does. `classification` carries the same Pre-MIS/Pending Review meaning
    as the CN register, and per item 10 is set for the *entire voucher* —
    if any one line of a Receipt/Journal is classified PRE_MIS_ADJUSTMENT,
    every line of that same voucher (including its own on-account/New-Ref
    leg, if the voucher has one) must share that classification, or the
    Unapplied Cash netting (item 9) develops a phantom imbalance from a
    pre-go-live counterpart this system never captured. Enforcing that
    invariant is ar_mis.registers' job, not this dataclass's.
    """

    branch_id: str
    txn_date: date
    voucher_type: str  # "Receipt" or "Journal" (VoucherType.value)
    voucher_number: str
    party_id: str
    amount: Decimal
    target_doc_no: str | None = None
    classification: RegisterClassification = RegisterClassification.CURRENT
    narration: str = ""


@dataclass
class InvoiceFollowUp:
    """The one genuinely mutable/editable record in the registers design
    (item 3/7) — PTP Date, PTP Amount, Next Action, and Expected
    Collection Date, edited directly by the AR team against a specific
    invoice. Not frozen, and not append-only: unlike every other register
    here, this row is legitimately upserted in place, because it's human
    judgment with no other source of truth to derive it from or replay it
    against. Keyed to the same (branch_id, voucher_number, party_id)
    identity as the SalesDNRegisterRow it follows up on.

    `logged_at` is the date the CURRENT (ptp_date, ptp_amount) pair was
    set — not row-creation time, and not touched by an edit that only
    changes next_action/expected_collection_date. This exists for the
    PTP Kept Rate calculation (design doc item 13): judging whether "the
    promised amount was paid by the promised date" requires knowing how
    much was collected on this invoice *since the promise was made*, not
    the invoice's total-ever-collected figure — otherwise a balance the
    customer already paid before this promise existed would falsely
    count toward keeping it. Storage.upsert_invoice_follow_up is
    responsible for stamping this only when the promise itself actually
    changes, never on every edit.
    """

    branch_id: str
    voucher_number: str
    party_id: str
    ptp_date: date | None = None
    ptp_amount: Decimal | None = None
    next_action: str = ""
    expected_collection_date: date | None = None
    updated_by: str = ""
    logged_at: date | None = None


@dataclass(frozen=True)
class FYRolloverSnapshot:
    """Design doc item 4 — a frozen, point-in-time record of an invoice's
    Open Amount at the moment of year-end rollover (the "Carried Forward
    Open Amount" column), created only by a deliberate rollover action
    (never automatically by the calendar). Display/audit only: never an
    input back into the live Open Amount computation, which always stays
    freshly derived from the raw registers regardless of whether a
    rollover has happened (item 14).
    """

    branch_id: str
    voucher_number: str
    party_id: str
    from_financial_year: str
    to_financial_year: str
    open_amount_at_rollover: Decimal
    rolled_over_by: str
    rolled_over_at: date


@dataclass(frozen=True)
class RegisterBuildExceptionRecord:
    """A registers.RegisterBuildExceptions.unattributable_party entry as
    persisted (Store.append_register_build_exceptions) - the same real
    gap DriftFindingRecord fixed for backdated entries, now fixed here
    too: this was previously shown once on the result page of the run
    that found it, then gone.

    `status` is a human review marker, exactly like DriftFindingRecord's
    own `acknowledged`/`incorporated` split - an audit trail only, never
    itself touching weekly_snapshot or any register:
      - "open": not yet reviewed.
      - "reviewed_no_action": a human checked Tally and confirmed the
        exclusion is genuinely correct (e.g. the ledger really is a
        Sundry Creditor) - zero effect on any figure, just a record that
        someone looked.
      - "resolved_via_catchup": the party was onboarded through Catch Up
        a Party (ar_mis.webapp.app.catch_up_party) - the real fix is
        that separate run; this only records that the call was made,
        by whom, and why (`reviewed_note`).
    """

    id: int
    branch_id: str
    week_ending: date
    voucher_number: str
    party_ledger_name: str
    reason: str
    logged_at: datetime
    status: str = "open"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    reviewed_note: str | None = None
