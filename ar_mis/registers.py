"""Builds the three master registers (docs/registers_and_reporting_design.md
item 1-2) from extracted vouchers - the confirmed replacement for
weekly_snapshot as the base of reporting.

This module is deliberately conservative about "confirmed vs. still open"
in that design doc: it builds exactly what's marked Confirmed, and where a
detail is explicitly still an open item (e.g. Round Off's exact treatment,
Ageing Bucket granularity), it makes the most defensible provisional choice
and says so in a docstring/comment, rather than silently picking one and
presenting it as settled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    InvoiceFollowUp,
    LedgerEntry,
    NoteType,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
)

# Confirmed against real client data (fixtures/real_samples/SalesReg.xml):
# actual tax ledgers are bare, EXACT-match "CGST"/"SGST"/"IGST" - many
# revenue ledgers are also "_GST"-suffixed (e.g. "Road Transport
# Services_GST_18%", "Handling Services_GST_INTER") and would be wrongly
# caught by a substring match, unlike the "contains" matching this codebase
# uses elsewhere (categorize_voucher_type) for voucher TYPE names. Tax
# ledger identification is intentionally NOT substring-based.
_TAX_LEDGER_NAMES = {"cgst": "CGST", "sgst": "SGST", "igst": "IGST"}

# Confirmed present on real invoices. Open item in the design doc: whether
# Round Off folds into Taxable Value, into Invoice Value only (after tax),
# or gets its own column. Provisional choice made here: folded into
# invoice_value after tax, since Round Off is a total-level rounding
# adjustment to the invoice's grand total, not part of the taxable base a
# GST return would report - the choice most consistent with how Tally
# itself displays it (as the final adjusting line before the invoice
# total). Revisit if the client decides otherwise.
_ROUND_OFF_LEDGER_NAME = "round off"

# Confirmed against real data: "Advance" carries a real bill reference
# (money received before the invoice existed) and is matched exactly like
# "Agst Ref" - only "New Ref" means genuinely unapplied/on-account.
_ALLOCATED_BILL_TYPES = {"agst ref", "advance"}
_UNAPPLIED_BILL_TYPE = "new ref"


def classify_tax_ledger(ledger_name: str) -> str | None:
    """Returns "CGST"/"SGST"/"IGST" for an exact (case/whitespace
    insensitive) match, else None. Deliberately not substring matching -
    see module docstring.
    """
    return _TAX_LEDGER_NAMES.get(ledger_name.strip().lower())


def is_round_off_ledger(ledger_name: str) -> bool:
    return ledger_name.strip().lower() == _ROUND_OFF_LEDGER_NAME


def _is_allocated(bill_type: str | None) -> bool:
    """True for a bill allocation that names a real invoice reference
    ("Agst Ref" or "Advance" - see _ALLOCATED_BILL_TYPES), False for
    "New Ref" (unapplied/on-account) or no bill allocation at all.
    """
    return bool(bill_type) and bill_type.strip().lower() in _ALLOCATED_BILL_TYPES


@dataclass
class RegisterBuildExceptions:
    """Vouchers this module could not confidently place into a register,
    collected rather than silently dropped or guessed at - matching this
    whole system's "never silent" principle. Each entry is a
    (voucher_number, reason) pair.
    """

    unattributable_party: list[tuple[str, str]]

    def __init__(self) -> None:
        self.unattributable_party = []


def compute_due_date(invoice_date: date, credit_period_days: int) -> date:
    """Item 6: computed once, at invoice creation, from the party's
    Customer Master credit period AS IT STOOD AT THAT MOMENT - callers
    must pass the credit period that was in effect then, not look it up
    fresh later, since a later Customer Master change must never
    retroactively recalculate an already-computed Due Date.
    """
    return invoice_date + timedelta(days=credit_period_days)


def build_sales_dn_register_row(
    voucher: Voucher,
    customer: CustomerMasterRecord,
    exceptions: RegisterBuildExceptions,
) -> SalesDNRegisterRow | None:
    """Builds one row for a Sales or Debit Note voucher. Returns None (and
    records why in `exceptions`) rather than guessing when the voucher's
    own party can't be determined - confirmed against real data that this
    genuinely happens (a voucher with no PARTYLEDGERNAME at all).

    `customer` must be the CustomerMasterRecord for voucher.party_ledger_name,
    already resolved by the caller (this function does no lookup - it only
    uses customer.credit_period_days, per item 6, at the value the caller
    supplies, which must be whatever was in effect on voucher.voucher_date,
    not necessarily today's value).
    """
    if voucher.voucher_type not in (VoucherType.SALES, VoucherType.DEBIT_NOTE):
        raise ValueError(f"build_sales_dn_register_row called with {voucher.voucher_type}, not Sales/Debit Note")

    if not voucher.party_ledger_name:
        exceptions.unattributable_party.append(
            (voucher.voucher_number, "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")
        )
        return None

    taxable_value = Decimal("0.00")
    cgst = Decimal("0.00")
    sgst = Decimal("0.00")
    igst = Decimal("0.00")
    round_off = Decimal("0.00")
    bill_allocation_reference: str | None = None

    for entry in voucher.entries:
        if entry.party_ledger_name == voucher.party_ledger_name:
            # The party's own side of the voucher - this is where the
            # invoice's own Bill Allocation Reference (New Ref) lives.
            if entry.bill_name:
                bill_allocation_reference = entry.bill_name
            continue
        tax_type = classify_tax_ledger(entry.party_ledger_name)
        magnitude = abs(entry.amount_as_extracted)
        if tax_type == "CGST":
            cgst += magnitude
        elif tax_type == "SGST":
            sgst += magnitude
        elif tax_type == "IGST":
            igst += magnitude
        elif is_round_off_ledger(entry.party_ledger_name):
            round_off += entry.amount_as_extracted  # signed - can reduce or increase the total
        else:
            taxable_value += magnitude

    invoice_value = taxable_value + cgst + sgst + igst + round_off
    note_type = NoteType.INVOICE if voucher.voucher_type == VoucherType.SALES else NoteType.DEBIT_NOTE

    return SalesDNRegisterRow(
        branch_id=voucher.branch_id,
        invoice_date=voucher.voucher_date,
        note_type=note_type,
        voucher_number=voucher.voucher_number,
        bill_allocation_reference=bill_allocation_reference or voucher.voucher_number,
        party_id=voucher.party_ledger_name,
        taxable_value=taxable_value,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        invoice_value=invoice_value,
        due_date=compute_due_date(voucher.voucher_date, customer.credit_period_days),
    )


def _resolve_bill_allocation(
    entry: LedgerEntry,
    party_ledger_name: str,
    tracked_bill_references: dict[tuple[str, str], SalesDNRegisterRow],
) -> tuple[str | None, RegisterClassification]:
    """Shared matching logic for Credit Note and Receipt/Journal lines -
    item 8's fix: match on (party, Bill Allocation Reference) as a
    composite key, never voucher_number alone, exactly mirroring how
    Tally itself scopes bill references per-ledger internally.

    Returns (target_reference_or_None, classification). A reference that
    doesn't resolve to a tracked invoice is PENDING_REVIEW (item 10) -
    never silently dropped, never silently assumed to be Pre-MIS. Only a
    human resolving the exception (outside this function - see
    Store.record_pre_mis_adjustment) can turn that into
    PRE_MIS_ADJUSTMENT or back into a corrected Current entry.
    """
    if not _is_allocated(entry.bill_type):
        return None, RegisterClassification.CURRENT
    reference = entry.bill_name
    if reference and (party_ledger_name, reference) in tracked_bill_references:
        return reference, RegisterClassification.CURRENT
    return reference, RegisterClassification.PENDING_REVIEW


def build_credit_note_register_row(
    voucher: Voucher,
    tracked_bill_references: dict[tuple[str, str], SalesDNRegisterRow],
    exceptions: RegisterBuildExceptions,
) -> CreditNoteRegisterRow | None:
    """One row per Credit Note. `tracked_bill_references` maps
    (party_ledger_name, bill_allocation_reference) -> the SalesDNRegisterRow
    it identifies - the caller builds this from every already-built Sales &
    DN row (item 8's composite key), so this function never needs to query
    storage directly and stays trivially testable.
    """
    if voucher.voucher_type != VoucherType.CREDIT_NOTE:
        raise ValueError(f"build_credit_note_register_row called with {voucher.voucher_type}, not Credit Note")
    if not voucher.party_ledger_name:
        exceptions.unattributable_party.append(
            (voucher.voucher_number, "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")
        )
        return None

    cn_amount = Decimal("0.00")
    reference: str | None = None
    classification = RegisterClassification.CURRENT
    for entry in voucher.entries:
        if entry.party_ledger_name != voucher.party_ledger_name:
            continue
        cn_amount += abs(entry.amount_as_extracted)
        ref, cls = _resolve_bill_allocation(entry, voucher.party_ledger_name, tracked_bill_references)
        if ref is not None:
            reference = ref
        if cls != RegisterClassification.CURRENT:
            classification = cls

    return CreditNoteRegisterRow(
        branch_id=voucher.branch_id,
        cn_date=voucher.voucher_date,
        voucher_number=voucher.voucher_number,
        party_id=voucher.party_ledger_name,
        cn_amount=cn_amount,
        bill_allocation_reference=reference,
        classification=classification,
    )


def build_receipt_journal_register_rows(
    voucher: Voucher,
    tracked_bill_references: dict[tuple[str, str], SalesDNRegisterRow],
    exceptions: RegisterBuildExceptions,
) -> list[ReceiptJournalRegisterRow]:
    """One row per bill-allocation line of a Receipt or Journal voucher -
    unlike Sales/DN/CN, a single Receipt or Journal voucher can
    legitimately apply against MULTIPLE different invoices (a customer
    pays several invoices in one cheque), so this returns a list, not one
    row per voucher.

    Voucher-level classification invariant (item 10): if ANY line in this
    voucher resolves to PENDING_REVIEW or is later confirmed
    PRE_MIS_ADJUSTMENT, EVERY line of this same voucher - including its
    own on-account/unapplied leg, if it has one - must carry that same
    classification, or the Unapplied Cash netting (item 9) develops a
    phantom imbalance from a pre-go-live counterpart this system never
    captured. This function enforces that: it computes each line's own
    classification first, then promotes every line to the most severe
    classification found anywhere in the voucher.
    """
    if voucher.voucher_type not in (VoucherType.RECEIPT, VoucherType.JOURNAL):
        raise ValueError(f"build_receipt_journal_register_rows called with {voucher.voucher_type}")
    if not voucher.party_ledger_name:
        exceptions.unattributable_party.append(
            (voucher.voucher_number, "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")
        )
        return []

    rows: list[ReceiptJournalRegisterRow] = []
    for entry in voucher.entries:
        if entry.party_ledger_name != voucher.party_ledger_name:
            continue
        target_doc_no, classification = _resolve_bill_allocation(
            entry, voucher.party_ledger_name, tracked_bill_references
        )
        rows.append(
            ReceiptJournalRegisterRow(
                branch_id=voucher.branch_id,
                txn_date=voucher.voucher_date,
                voucher_type=voucher.voucher_type.value,
                voucher_number=voucher.voucher_number,
                party_id=voucher.party_ledger_name,
                amount=abs(entry.amount_as_extracted),
                target_doc_no=target_doc_no,
                classification=classification,
            )
        )

    if any(r.classification == RegisterClassification.PENDING_REVIEW for r in rows):
        rows = [
            r if r.classification == RegisterClassification.PENDING_REVIEW
            else _replace_classification(r, RegisterClassification.PENDING_REVIEW)
            for r in rows
        ]
    return rows


def _replace_classification(
    row: ReceiptJournalRegisterRow, classification: RegisterClassification
) -> ReceiptJournalRegisterRow:
    return ReceiptJournalRegisterRow(
        branch_id=row.branch_id,
        txn_date=row.txn_date,
        voucher_type=row.voucher_type,
        voucher_number=row.voucher_number,
        party_id=row.party_id,
        amount=row.amount,
        target_doc_no=row.target_doc_no,
        classification=classification,
        narration=row.narration,
    )


def build_bill_reference_lookup(
    sales_dn_rows: list[SalesDNRegisterRow],
) -> dict[tuple[str, str], SalesDNRegisterRow]:
    """Item 8's composite key, built once from every tracked Sales & DN
    row so CN/Receipt/Journal matching is a plain dict lookup, not a
    linear scan or a storage query embedded in the matching logic itself.
    """
    return {(row.party_id, row.bill_allocation_reference): row for row in sales_dn_rows}


def compute_unapplied_cash_by_party(
    rows: list[ReceiptJournalRegisterRow],
) -> dict[str, Decimal]:
    """Item 9: net total per party of every CURRENT, unapplied
    (target_doc_no is None) line - a receipt and its later reversing
    journal net to zero automatically through plain addition, without
    needing to detect and exclude the specific matched pair (fragile -
    risks false matches on coincidental same-amount transactions).
    PENDING_REVIEW and PRE_MIS_ADJUSTMENT lines are excluded entirely,
    matching item 10: a Pre-MIS-flagged voucher's on-account leg has no
    real counterpart in this system's own data and would otherwise show
    as a phantom imbalance.
    """
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.target_doc_no is not None or row.classification != RegisterClassification.CURRENT:
            continue
        totals[row.party_id] = totals.get(row.party_id, Decimal("0.00")) + row.amount
    return totals


def compute_unapplied_cn_by_party(rows: list[CreditNoteRegisterRow]) -> dict[str, Decimal]:
    """Item 9 extended to Credit Notes (confirmed same session) - same
    netting principle as compute_unapplied_cash_by_party, for CNs left
    on-account (bill_allocation_reference is None) rather than receipts.
    """
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.bill_allocation_reference is not None or row.classification != RegisterClassification.CURRENT:
            continue
        totals[row.party_id] = totals.get(row.party_id, Decimal("0.00")) + row.cn_amount
    return totals


def compute_ageing_bucket(days_past_due: int) -> str:
    """CONFIRMED scheme (client's own explicit specification, resolving
    the design doc's former open item): the finer 91-180 split this
    function already used provisionally turned out to be exactly right -
    only the exact labels below are the client's own wording, not a
    guess:

        Current, 1-30, 31-60, 61-90, 91-120, 121-150, 151-180, 181+
    """
    if days_past_due <= 0:
        return "Current"
    if days_past_due <= 30:
        return "1-30"
    if days_past_due <= 60:
        return "31-60"
    if days_past_due <= 90:
        return "61-90"
    if days_past_due <= 120:
        return "91-120"
    if days_past_due <= 150:
        return "121-150"
    if days_past_due <= 180:
        return "151-180"
    return "181+"


@dataclass
class InvoicePosition:
    """The as-of-date computed position of one invoice (item 14) - always
    freshly derived from the raw registers filtered to `as_of`, never read
    from a stored/mutable total. Nothing here is persisted.
    """

    row: SalesDNRegisterRow
    linked_cn_amount: Decimal
    receipts_applied: Decimal
    open_amount: Decimal
    is_overdue: bool
    days_past_due: int
    ageing_bucket: str


def compute_invoice_position(
    row: SalesDNRegisterRow,
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
) -> InvoicePosition:
    """Item 14: filters both underlying registers to `transaction date <=
    as_of` before aggregating, so the same invoice can be correctly
    re-evaluated as of any historical date, not just "now". Only CURRENT-
    classified CN/receipt lines count against Open Amount - a
    PENDING_REVIEW or PRE_MIS_ADJUSTMENT line, by definition, isn't a real
    application against this specific tracked invoice yet.
    """
    key = (row.party_id, row.bill_allocation_reference)
    linked_cn_amount = sum(
        (
            cn.cn_amount
            for cn in credit_note_rows
            if cn.cn_date <= as_of
            and cn.classification == RegisterClassification.CURRENT
            and (cn.party_id, cn.bill_allocation_reference) == key
        ),
        Decimal("0.00"),
    )
    receipts_applied = sum(
        (
            rj.amount
            for rj in receipt_journal_rows
            if rj.txn_date <= as_of
            and rj.classification == RegisterClassification.CURRENT
            and (rj.party_id, rj.target_doc_no) == key
        ),
        Decimal("0.00"),
    )
    net_receivable = row.invoice_value - linked_cn_amount
    open_amount = net_receivable - receipts_applied
    days_past_due = (as_of - row.due_date).days
    is_overdue = open_amount > 0 and days_past_due > 0
    return InvoicePosition(
        row=row,
        linked_cn_amount=linked_cn_amount,
        receipts_applied=receipts_applied,
        open_amount=open_amount,
        is_overdue=is_overdue,
        days_past_due=max(days_past_due, 0),
        ageing_bucket=compute_ageing_bucket(days_past_due if is_overdue else 0),
    )


# ---------------------------------------------------------------------
# KPI dashboard formulas (design doc item 15) — every one of these was
# confirmed explicitly with the client rather than assumed; see design
# doc items 10 (Collection Efficiency), 12 (DSO), and 13 (PTP Kept Rate).
# Each function takes already-aggregated inputs rather than raw register
# lists, so the arithmetic itself stays trivially testable independent of
# how a caller chooses to aggregate a period or a portfolio.
# ---------------------------------------------------------------------


def compute_sales_in_window(rows: list[SalesDNRegisterRow], window_start: date, window_end: date) -> Decimal:
    """Sum of invoice_value for every Sales & DN Register row whose
    invoice_date falls within [window_start, window_end] inclusive - the
    "new sales" figure both DSO and Collection Efficiency need.
    """
    return sum(
        (row.invoice_value for row in rows if window_start <= row.invoice_date <= window_end),
        Decimal("0.00"),
    )


def compute_collections_in_window(
    rows: list[ReceiptJournalRegisterRow], window_start: date, window_end: date
) -> Decimal:
    """Sum of amount for every CURRENT-classified Receipt & Journal
    Register row whose txn_date falls within [window_start, window_end]
    inclusive - Collection Efficiency's "actually collected" figure.
    PENDING_REVIEW/PRE_MIS_ADJUSTMENT lines are excluded, matching item
    9/10's treatment everywhere else in this module - an unresolved or
    pre-go-live reference isn't a real collection this system can vouch
    for yet.
    """
    return sum(
        (
            row.amount
            for row in rows
            if window_start <= row.txn_date <= window_end and row.classification == RegisterClassification.CURRENT
        ),
        Decimal("0.00"),
    )


def compute_dso(total_open_ar: Decimal, sales_last_90_days: Decimal) -> Decimal | None:
    """Design doc item 12, client-confirmed formula: Total Open AR ÷
    average daily sales, where average daily sales is Total Sales over
    the trailing 90 days ÷ 90 - the 90-day trailing window was the
    client's explicit choice over a 12-month window, for faster reaction
    to recent changes in the business. `total_open_ar` and
    `sales_last_90_days` are caller-supplied aggregates (e.g. summing
    compute_invoice_position(...).open_amount across every tracked
    invoice, and compute_sales_in_window over the trailing 90 days)
    rather than computed here, so this function is pure arithmetic.

    Returns None when there were no sales in the window - the average
    daily sales figure is undefined (division by zero), not zero.
    """
    if sales_last_90_days == 0:
        return None
    average_daily_sales = sales_last_90_days / Decimal("90")
    return total_open_ar / average_daily_sales


def compute_collection_efficiency(
    opening_ar: Decimal, sales_in_period: Decimal, collected_in_period: Decimal
) -> Decimal | None:
    """Design doc item 10, client-confirmed formula: what was actually
    collected during the period, as a percentage of what was owed across
    the period (opening AR at the start of the period + new sales made
    during it). Returns None when the denominator is zero - undefined,
    not 0% or 100%.
    """
    denominator = opening_ar + sales_in_period
    if denominator == 0:
        return None
    return (collected_in_period / denominator) * Decimal("100")


@dataclass
class PTPOutcome:
    """Whether one specific promise was Kept or Broken, and how much was
    actually collected toward it - always derived, never stored (see
    InvoiceFollowUp's docstring: there is no status field to read this
    from instead).
    """

    kept: bool
    amount_collected_since_promise: Decimal


def compute_ptp_outcome(
    row: SalesDNRegisterRow,
    follow_up: InvoiceFollowUp,
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
) -> PTPOutcome | None:
    """Design doc item 13, client-confirmed rule: a promise is Kept if AT
    LEAST the promised amount was collected on THIS invoice, SINCE the
    promise was logged (`follow_up.logged_at`), by the promised date
    (`follow_up.ptp_date`) - never the invoice's total-ever-collected
    figure, which would let a balance the customer already paid *before*
    this promise even existed falsely count toward keeping it. The rest
    of the invoice being open (a different, unrelated portion of the
    balance) does not break this specific promise, per the client's own
    worked example when confirming this.

    Computed as the difference between receipts_applied as of ptp_date
    and receipts_applied as of the day before logged_at (so a payment
    arriving on logged_at itself still counts, but nothing from before
    the promise existed does) - both via compute_invoice_position, so
    this stays consistent with every other as-of-date computation in
    this module rather than re-deriving its own notion of "applied".

    Returns None when there is nothing to judge yet: no promise logged
    (ptp_date/ptp_amount unset) or no logged_at recorded (shouldn't
    happen once a promise exists - see Store.upsert_invoice_follow_up -
    but this function never assumes storage's own invariant held).
    """
    if follow_up.ptp_date is None or follow_up.ptp_amount is None or follow_up.logged_at is None:
        return None
    position_at_ptp_date = compute_invoice_position(
        row, credit_note_rows, receipt_journal_rows, as_of=follow_up.ptp_date
    )
    day_before_promise = follow_up.logged_at - timedelta(days=1)
    position_before_promise = compute_invoice_position(
        row, credit_note_rows, receipt_journal_rows, as_of=day_before_promise
    )
    amount_collected_since_promise = position_at_ptp_date.receipts_applied - position_before_promise.receipts_applied
    return PTPOutcome(
        kept=amount_collected_since_promise >= follow_up.ptp_amount,
        amount_collected_since_promise=amount_collected_since_promise,
    )


def compute_ptp_kept_rate(
    follow_ups: list[InvoiceFollowUp],
    sales_dn_rows_by_identity: dict[tuple[str, str, str], SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
) -> Decimal | None:
    """Design doc item 13, client-confirmed: of every promise whose date
    has already passed (`ptp_date <= as_of`), what percentage were Kept.
    A promise not yet due is excluded entirely - it counts toward
    neither Kept nor Broken, exactly as the client confirmed - and so is
    one with no PTPOutcome to judge (see compute_ptp_outcome).

    `sales_dn_rows_by_identity` keys each tracked invoice by
    (branch_id, voucher_number, party_id) - the same identity
    InvoiceFollowUp is keyed to (see that dataclass's docstring) - so a
    follow-up can be matched to its invoice without a linear scan; build
    it once via `{(r.branch_id, r.voucher_number, r.party_id): r for r
    in sales_dn_rows}`.

    Returns None when there are no eligible (already-past-due) promises
    at all - undefined, not 0% or 100%.
    """
    outcomes: list[PTPOutcome] = []
    for follow_up in follow_ups:
        if follow_up.ptp_date is None or follow_up.ptp_date > as_of:
            continue
        row = sales_dn_rows_by_identity.get((follow_up.branch_id, follow_up.voucher_number, follow_up.party_id))
        if row is None:
            continue
        outcome = compute_ptp_outcome(row, follow_up, credit_note_rows, receipt_journal_rows)
        if outcome is not None:
            outcomes.append(outcome)
    if not outcomes:
        return None
    kept_count = sum(1 for outcome in outcomes if outcome.kept)
    return (Decimal(kept_count) / Decimal(len(outcomes))) * Decimal("100")
