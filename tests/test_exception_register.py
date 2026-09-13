from datetime import date
from decimal import Decimal

from ar_mis.exception_register import (
    compute_negative_open_amount_invoices,
    compute_non_active_debtors,
    compute_top_overdue_customers,
    compute_unapplied_cash_exceptions,
    compute_unapplied_cn_exceptions,
    compute_unresolved_references,
)
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    LedgerEntry,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    Voucher,
    VoucherType,
)
from ar_mis.registers import RegisterBuildExceptions, build_sales_dn_register_row

CUSTOMER = CustomerMasterRecord(
    party_id="ACME", party_name="Acme Corp", branch_id="B1", pre_mis_outstanding=Decimal("0.00")
)


def _invoice(voucher_number, party_id, branch_id, invoice_date, value):
    customer = CustomerMasterRecord(
        party_id=party_id, party_name=party_id, branch_id=branch_id, pre_mis_outstanding=Decimal("0.00")
    )
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=invoice_date, voucher_number=voucher_number,
        branch_id=branch_id, party_ledger_name=party_id,
        entries=[
            LedgerEntry(party_ledger_name=party_id, amount_as_extracted=-value, bill_name=voucher_number, bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=value),
        ],
    )
    return build_sales_dn_register_row(voucher, customer, RegisterBuildExceptions())


# ---- Unapplied cash / CN exceptions ---------------------------------------


def test_unapplied_cash_exceptions_excludes_zero_and_sorts_descending():
    rows = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 1), voucher_type="Receipt",
                                   voucher_number="R1", party_id="SMALL", amount=Decimal("100.00")),
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 1), voucher_type="Receipt",
                                   voucher_number="R2", party_id="BIG", amount=Decimal("500.00")),
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 2), voucher_type="Journal",
                                   voucher_number="J1", party_id="ZERO", amount=Decimal("200.00")),
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 3), voucher_type="Journal",
                                   voucher_number="J2", party_id="ZERO", amount=Decimal("-200.00")),
    ]
    exceptions = compute_unapplied_cash_exceptions(rows)
    assert [r.party_id for r in exceptions] == ["BIG", "SMALL"]
    assert exceptions[0].amount == Decimal("500.00")


def test_unapplied_cn_exceptions_excludes_allocated_cns():
    rows = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 1), voucher_number="CN1",
                               party_id="ACME", cn_amount=Decimal("300.00"), bill_allocation_reference=None),
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 1), voucher_number="CN2",
                               party_id="OTHER", cn_amount=Decimal("100.00"), bill_allocation_reference="INV1"),
    ]
    exceptions = compute_unapplied_cn_exceptions(rows)
    assert [r.party_id for r in exceptions] == ["ACME"]


# ---- Negative Open Amount --------------------------------------------------


def test_negative_open_amount_flags_overpaid_invoice():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    receipts = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 10), voucher_type="Receipt",
                                   voucher_number="R1", party_id="ACME", amount=Decimal("1200.00"), target_doc_no="INV001"),
    ]
    rows = compute_negative_open_amount_invoices([invoice], [], receipts, as_of=date(2026, 2, 1))
    assert len(rows) == 1
    assert rows[0].open_amount == Decimal("-200.00")


def test_negative_open_amount_excludes_normal_invoices():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_negative_open_amount_invoices([invoice], [], [], as_of=date(2026, 2, 1))
    assert rows == []


# ---- Non-Active debtors -----------------------------------------------------


def test_non_active_debtor_flagged_after_180_days_of_silence():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    as_of = date(2026, 9, 1)  # ~243 days since the invoice, no other activity
    rows = compute_non_active_debtors([invoice], [], [], as_of)
    assert len(rows) == 1
    assert rows[0].party_id == "ACME"
    assert rows[0].total_open == Decimal("1000.00")
    assert rows[0].last_transaction_date == date(2026, 1, 1)
    assert rows[0].days_since_last_transaction == (as_of - date(2026, 1, 1)).days


def test_non_active_debtor_excluded_if_recent_receipt_keeps_them_active():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    receipts = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 8, 15), voucher_type="Receipt",
                                   voucher_number="R1", party_id="ACME", amount=Decimal("100.00"), target_doc_no="INV001"),
    ]
    rows = compute_non_active_debtors([invoice], [], receipts, as_of=date(2026, 9, 1))
    assert rows == []


def test_non_active_debtor_excluded_if_fully_paid():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    receipts = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 5), voucher_type="Receipt",
                                   voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no="INV001"),
    ]
    rows = compute_non_active_debtors([invoice], [], receipts, as_of=date(2026, 9, 1))
    assert rows == []


def test_non_active_debtor_not_flagged_under_the_threshold():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_non_active_debtors([invoice], [], [], as_of=date(2026, 6, 1))  # ~150 days
    assert rows == []


# ---- Unresolved references ---------------------------------------------------


def test_unresolved_references_includes_only_pending_review_and_sorts_oldest_first():
    cn_rows = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 3, 1), voucher_number="CN1",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV-X",
                               classification=RegisterClassification.PENDING_REVIEW),
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 1), voucher_number="CN2",
                               party_id="ACME", cn_amount=Decimal("50.00"), bill_allocation_reference="INV-Y",
                               classification=RegisterClassification.CURRENT),
    ]
    rj_rows = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 2, 1), voucher_type="Receipt",
                                   voucher_number="R1", party_id="ACME", amount=Decimal("300.00"),
                                   target_doc_no="INV-Z", classification=RegisterClassification.PENDING_REVIEW),
    ]
    rows = compute_unresolved_references(cn_rows, rj_rows)
    assert len(rows) == 2  # CN1 and R1 only - CN2 is Current, excluded
    assert [r.voucher_number for r in rows] == ["R1", "CN1"]  # oldest first
    assert rows[0].source == "Receipt"
    assert rows[0].attempted_reference == "INV-Z"


def test_unresolved_references_empty_when_all_resolved():
    cn_rows = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 1), voucher_number="CN1",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV-X"),
    ]
    assert compute_unresolved_references(cn_rows, []) == []


# ---- Top overdue customers ---------------------------------------------------


def test_top_overdue_customers_sums_per_party_and_sorts_descending():
    inv1 = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    inv2 = _invoice("INV002", "ACME", "B1", date(2026, 1, 1), Decimal("500.00"))
    inv3 = _invoice("INV003", "OTHER", "B1", date(2026, 1, 1), Decimal("2000.00"))
    as_of = date(2026, 9, 1)  # all well overdue (30-day default credit period)
    rows = compute_top_overdue_customers([inv1, inv2, inv3], [], [], as_of)
    assert rows[0].party_id == "OTHER"
    assert rows[0].overdue_amount == Decimal("2000.00")
    assert rows[1].party_id == "ACME"
    assert rows[1].overdue_amount == Decimal("1500.00")  # 1000 + 500 combined


def test_top_overdue_customers_excludes_not_yet_due_and_respects_limit():
    invoices = [_invoice(f"INV{i:03d}", f"PARTY{i}", "B1", date(2026, 1, 1), Decimal("1000.00")) for i in range(25)]
    as_of = date(2026, 9, 1)
    rows = compute_top_overdue_customers(invoices, [], [], as_of, limit=20)
    assert len(rows) == 20

    not_due_invoice = _invoice("INVFUTURE", "FUTURE", "B1", date(2026, 8, 20), Decimal("500.00"))
    rows2 = compute_top_overdue_customers([not_due_invoice], [], [], as_of=date(2026, 8, 25))
    assert rows2 == []
