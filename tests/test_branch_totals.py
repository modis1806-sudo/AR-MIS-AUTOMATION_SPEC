from datetime import date
from decimal import Decimal

from ar_mis.branch_totals import compute_branch_sales_cn_dn_totals
from ar_mis.models import CreditNoteRegisterRow, CustomerMasterRecord, LedgerEntry, Voucher, VoucherType
from ar_mis.registers import RegisterBuildExceptions, build_sales_dn_register_row


def _invoice(voucher_number, party_id, branch_id, invoice_date, value, voucher_type=VoucherType.SALES):
    customer = CustomerMasterRecord(
        party_id=party_id, party_name=party_id, branch_id=branch_id, pre_mis_outstanding=Decimal("0.00")
    )
    voucher = Voucher(
        voucher_type=voucher_type, voucher_date=invoice_date, voucher_number=voucher_number,
        branch_id=branch_id, party_ledger_name=party_id,
        entries=[
            LedgerEntry(party_ledger_name=party_id, amount_as_extracted=-value, bill_name=voucher_number, bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=value),
        ],
    )
    return build_sales_dn_register_row(voucher, customer, RegisterBuildExceptions())


def _cn(voucher_number, party_id, branch_id, cn_date, amount):
    return CreditNoteRegisterRow(
        branch_id=branch_id, cn_date=cn_date, voucher_number=voucher_number, party_id=party_id, cn_amount=amount
    )


def test_empty_registers_returns_no_rows():
    assert compute_branch_sales_cn_dn_totals([], []) == []


def test_single_branch_sales_only():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_branch_sales_cn_dn_totals([inv], [])
    assert len(rows) == 2  # B1 + All Branches
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.sales_total == Decimal("1000.00")
    assert b1.debit_note_total == Decimal("0.00")
    assert b1.credit_note_total == Decimal("0.00")
    assert b1.net_total == Decimal("1000.00")


def test_debit_notes_add_and_credit_notes_subtract():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    dn = _invoice("DN001", "ACME", "B1", date(2026, 1, 2), Decimal("200.00"), voucher_type=VoucherType.DEBIT_NOTE)
    cn = _cn("CN001", "ACME", "B1", date(2026, 1, 3), Decimal("300.00"))
    rows = compute_branch_sales_cn_dn_totals([inv, dn], [cn])
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.sales_total == Decimal("1000.00")
    assert b1.debit_note_total == Decimal("200.00")
    assert b1.credit_note_total == Decimal("300.00")
    assert b1.net_total == Decimal("900.00")  # 1000 + 200 - 300


def test_all_branches_row_sums_every_branch():
    inv1 = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    inv2 = _invoice("INV002", "BETA", "B2", date(2026, 1, 1), Decimal("500.00"))
    rows = compute_branch_sales_cn_dn_totals([inv1, inv2], [])
    all_branches = next(r for r in rows if r.branch_id == "All Branches")
    assert all_branches.sales_total == Decimal("1500.00")
    assert all_branches.net_total == Decimal("1500.00")


def test_period_filter_excludes_out_of_range_invoices():
    jan_inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 15), Decimal("1000.00"))
    feb_inv = _invoice("INV002", "ACME", "B1", date(2026, 2, 15), Decimal("500.00"))
    rows = compute_branch_sales_cn_dn_totals(
        [jan_inv, feb_inv], [], period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.sales_total == Decimal("1000.00")


def test_period_filter_is_inclusive_of_both_boundaries():
    start_inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("100.00"))
    end_inv = _invoice("INV002", "ACME", "B1", date(2026, 1, 31), Decimal("200.00"))
    rows = compute_branch_sales_cn_dn_totals(
        [start_inv, end_inv], [], period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.sales_total == Decimal("300.00")


def test_no_period_filter_includes_all_history():
    old_inv = _invoice("INV001", "ACME", "B1", date(2020, 1, 1), Decimal("1000.00"))
    new_inv = _invoice("INV002", "ACME", "B1", date(2026, 1, 1), Decimal("2000.00"))
    rows = compute_branch_sales_cn_dn_totals([old_inv, new_inv], [])
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.sales_total == Decimal("3000.00")


def test_credit_note_period_filter_uses_its_own_cn_date():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 15), Decimal("1000.00"))
    cn_in_period = _cn("CN001", "ACME", "B1", date(2026, 1, 20), Decimal("100.00"))
    cn_out_of_period = _cn("CN002", "ACME", "B1", date(2026, 3, 1), Decimal("50.00"))
    rows = compute_branch_sales_cn_dn_totals(
        [inv], [cn_in_period, cn_out_of_period], period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )
    b1 = next(r for r in rows if r.branch_id == "B1")
    assert b1.credit_note_total == Decimal("100.00")
