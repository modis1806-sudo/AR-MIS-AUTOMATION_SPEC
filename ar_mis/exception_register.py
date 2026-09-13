"""Exception Register (design doc item 15) - six sub-reports surfacing
exactly the data situations this whole design exists to catch rather
than bury: money sitting unapplied, an invoice overpaid, a customer gone
quiet while still owing money, a reference nobody resolved, and the
customers most worth chasing right now. Every function here takes
already-fetched register lists (matching ar_mis.registers/ar_mis.
dashboard's own convention) and is as-of-date selectable where the
underlying position depends on a date (item 14).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
)
from ar_mis.registers import (
    compute_invoice_position,
    compute_unapplied_cash_by_party,
    compute_unapplied_cn_by_party,
)

# Design doc item 15: "Non-Active debtors (180+ days with no transaction
# but balance still open)".
_NON_ACTIVE_THRESHOLD_DAYS = 180
_TOP_OVERDUE_LIMIT = 20


@dataclass
class UnappliedAmountRow:
    party_id: str
    amount: Decimal


def compute_unapplied_cash_exceptions(receipt_journal_rows: list[ReceiptJournalRegisterRow]) -> list[UnappliedAmountRow]:
    """Item 9: net unapplied receipt/journal total per party, largest
    first. Zero-net parties (a receipt fully offset by its own reversing
    journal) are excluded - there's nothing exceptional to show.
    """
    totals = compute_unapplied_cash_by_party(receipt_journal_rows)
    rows = [UnappliedAmountRow(party_id=p, amount=a) for p, a in totals.items() if a != 0]
    return sorted(rows, key=lambda r: r.amount, reverse=True)


def compute_unapplied_cn_exceptions(credit_note_rows: list[CreditNoteRegisterRow]) -> list[UnappliedAmountRow]:
    """Item 9 extended to Credit Notes - same netting principle."""
    totals = compute_unapplied_cn_by_party(credit_note_rows)
    rows = [UnappliedAmountRow(party_id=p, amount=a) for p, a in totals.items() if a != 0]
    return sorted(rows, key=lambda r: r.amount, reverse=True)


@dataclass
class NegativeOpenAmountRow:
    row: SalesDNRegisterRow
    open_amount: Decimal


def compute_negative_open_amount_invoices(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
) -> list[NegativeOpenAmountRow]:
    """An invoice whose linked CN + applied receipts exceed its own
    invoice value - an overpayment or an over-issued credit note against
    it, worth a human's attention rather than silently netting away
    somewhere else in a total.
    """
    rows = []
    for row in sales_dn_rows:
        position = compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of)
        if position.open_amount < 0:
            rows.append(NegativeOpenAmountRow(row=row, open_amount=position.open_amount))
    return sorted(rows, key=lambda r: r.open_amount)


@dataclass
class NonActiveDebtorRow:
    party_id: str
    branch_id: str
    total_open: Decimal
    last_transaction_date: date
    days_since_last_transaction: int


def compute_non_active_debtors(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
    inactivity_threshold_days: int = _NON_ACTIVE_THRESHOLD_DAYS,
) -> list[NonActiveDebtorRow]:
    """A party who still owes money but hasn't shown up in ANY register
    (a new sale, a credit note, a receipt/journal) in over the threshold
    - not merely an old unpaid invoice (that's just "overdue"), but one
    where nothing at all has moved for this party in a long time, which
    is a materially different kind of risk. `total_open` sums every
    tracked invoice's Open Amount for the party - a party can be "non-
    active" while still holding a balance across several old invoices,
    not just one.
    """
    parties = {(row.party_id, row.branch_id) for row in sales_dn_rows}
    rows = []
    for party_id, branch_id in parties:
        party_invoices = [row for row in sales_dn_rows if row.party_id == party_id and row.branch_id == branch_id]
        total_open = sum(
            (compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of).open_amount for row in party_invoices),
            Decimal("0.00"),
        )
        if total_open <= 0:
            continue

        transaction_dates = [row.invoice_date for row in party_invoices if row.invoice_date <= as_of]
        transaction_dates += [
            cn.cn_date for cn in credit_note_rows
            if cn.party_id == party_id and cn.branch_id == branch_id and cn.cn_date <= as_of
        ]
        transaction_dates += [
            rj.txn_date for rj in receipt_journal_rows
            if rj.party_id == party_id and rj.branch_id == branch_id and rj.txn_date <= as_of
        ]
        if not transaction_dates:
            continue

        last_transaction_date = max(transaction_dates)
        days_since = (as_of - last_transaction_date).days
        if days_since > inactivity_threshold_days:
            rows.append(
                NonActiveDebtorRow(
                    party_id=party_id, branch_id=branch_id, total_open=total_open,
                    last_transaction_date=last_transaction_date, days_since_last_transaction=days_since,
                )
            )
    return sorted(rows, key=lambda r: r.days_since_last_transaction, reverse=True)


@dataclass
class UnresolvedReferenceRow:
    source: str  # "Credit Note", "Receipt", or "Journal"
    branch_id: str
    voucher_number: str
    party_id: str
    voucher_date: date
    amount: Decimal
    attempted_reference: str | None


def compute_unresolved_references(
    credit_note_rows: list[CreditNoteRegisterRow], receipt_journal_rows: list[ReceiptJournalRegisterRow]
) -> list[UnresolvedReferenceRow]:
    """Item 10's exception routing: every CN/receipt/journal line
    classified Pending Review - an "Against Ref" allocation that didn't
    match any invoice this system tracks, awaiting a human's call on
    whether it's a Pre-MIS-era reference or a typo. Oldest first, since
    an old unresolved reference is the more urgent one to chase down.
    """
    rows = [
        UnresolvedReferenceRow(
            source="Credit Note", branch_id=cn.branch_id, voucher_number=cn.voucher_number, party_id=cn.party_id,
            voucher_date=cn.cn_date, amount=cn.cn_amount, attempted_reference=cn.bill_allocation_reference,
        )
        for cn in credit_note_rows
        if cn.classification == RegisterClassification.PENDING_REVIEW
    ]
    rows += [
        UnresolvedReferenceRow(
            source=rj.voucher_type, branch_id=rj.branch_id, voucher_number=rj.voucher_number, party_id=rj.party_id,
            voucher_date=rj.txn_date, amount=rj.amount, attempted_reference=rj.target_doc_no,
        )
        for rj in receipt_journal_rows
        if rj.classification == RegisterClassification.PENDING_REVIEW
    ]
    return sorted(rows, key=lambda r: r.voucher_date)


@dataclass
class TopOverdueCustomerRow:
    party_id: str
    branch_id: str
    overdue_amount: Decimal


def compute_top_overdue_customers(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
    limit: int = _TOP_OVERDUE_LIMIT,
) -> list[TopOverdueCustomerRow]:
    """Every overdue invoice's Open Amount, summed per (party, branch),
    largest total first, capped at `limit` (20 per the design doc)."""
    totals: dict[tuple[str, str], Decimal] = {}
    for row in sales_dn_rows:
        position = compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of)
        if not position.is_overdue:
            continue
        key = (row.party_id, row.branch_id)
        totals[key] = totals.get(key, Decimal("0.00")) + position.open_amount

    rows = [
        TopOverdueCustomerRow(party_id=party_id, branch_id=branch_id, overdue_amount=amount)
        for (party_id, branch_id), amount in totals.items()
    ]
    rows.sort(key=lambda r: r.overdue_amount, reverse=True)
    return rows[:limit]
