from datetime import date
from decimal import Decimal

from ar_mis.ageing_matrix import compute_ageing_matrix
from ar_mis.models import CustomerMasterRecord, LedgerEntry, PartyGrouping, Voucher, VoucherType
from ar_mis.registers import RegisterBuildExceptions, build_sales_dn_register_row


def _invoice(voucher_number, party_id, branch_id, invoice_date, value, customer=None):
    customer = customer or CustomerMasterRecord(
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


def test_one_row_per_party_branch_with_bucketed_open_amount():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=date(2026, 1, 1))
    assert len(rows) == 1
    row = rows[0]
    assert (row.party_id, row.branch_id) == ("ACME", "B1")
    assert row.total_open == Decimal("1000.00")
    assert row.buckets["Current"] == Decimal("1000.00")
    assert sum(row.buckets.values()) == row.total_open


def test_grouping_pulled_from_customer_master():
    customer = CustomerMasterRecord(
        party_id="RELCO", party_name="RelCo", branch_id="B1", pre_mis_outstanding=Decimal("0.00"),
        grouping=PartyGrouping.RELATED_PARTY,
    )
    inv = _invoice("INV001", "RELCO", "B1", date(2026, 1, 1), Decimal("500.00"), customer=customer)
    rows = compute_ageing_matrix([inv], [], [], {("RELCO", "B1"): customer}, {}, as_of=date(2026, 1, 1))
    assert rows[0].grouping == "Related Party"


def test_unclassified_party_has_none_grouping():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("500.00"))
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=date(2026, 1, 1))
    assert rows[0].grouping is None


def test_no_tally_balance_on_record_is_none_not_zero():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=date(2026, 1, 1))
    assert rows[0].tally_closing_balance is None
    assert rows[0].difference is None


def test_closed_bucket_is_present_and_does_not_raise():
    from ar_mis.models import ReceiptJournalRegisterRow

    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    receipt_rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 15), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no="INV001",
        ),
    ]
    rows = compute_ageing_matrix([inv], [], receipt_rows, {}, {}, as_of=date(2026, 9, 12))
    assert "Closed" in rows[0].buckets
    assert rows[0].buckets["Closed"] == Decimal("0.00")
    assert rows[0].buckets["Current"] == Decimal("0.00")
    assert rows[0].total_open == Decimal("0.00")


def test_difference_computed_when_tally_balance_present():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix(
        [inv], [], [], {}, {("ACME", "B1"): Decimal("950.00")}, as_of=date(2026, 1, 1)
    )
    assert rows[0].tally_closing_balance == Decimal("950.00")
    assert rows[0].difference == Decimal("50.00")


def test_difference_is_shown_even_when_tiny_no_materiality_tolerance():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix(
        [inv], [], [], {}, {("ACME", "B1"): Decimal("999.99")}, as_of=date(2026, 1, 1)
    )
    assert rows[0].difference == Decimal("0.01")


def test_multiple_parties_and_branches_each_get_own_row():
    inv1 = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    inv2 = _invoice("INV002", "BETA", "B2", date(2026, 1, 1), Decimal("2000.00"))
    rows = compute_ageing_matrix([inv1, inv2], [], [], {}, {}, as_of=date(2026, 1, 1))
    keys = {(r.party_id, r.branch_id) for r in rows}
    assert keys == {("ACME", "B1"), ("BETA", "B2")}


def test_empty_portfolio_returns_no_rows():
    assert compute_ageing_matrix([], [], [], {}, {}, as_of=date(2026, 1, 1)) == []


def test_ageing_bucket_reflects_days_past_due():
    inv = _invoice("INV001", "ACME", "B1", date(2025, 1, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=date(2026, 1, 1))
    row = rows[0]
    assert row.buckets["181+"] == Decimal("1000.00")
    assert row.buckets["Current"] == Decimal("0.00")


def test_same_party_multiple_invoices_summed_per_bucket():
    inv1 = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    inv2 = _invoice("INV002", "ACME", "B1", date(2026, 1, 2), Decimal("500.00"))
    rows = compute_ageing_matrix([inv1, inv2], [], [], {}, {}, as_of=date(2026, 1, 2))
    assert len(rows) == 1
    assert rows[0].total_open == Decimal("1500.00")


def test_overdue_total_excludes_current_and_closed_buckets():
    # One not-yet-due invoice (Current, excluded) and one far overdue
    # (181+, included) - overdue_total must count only the latter, same
    # population as AR Snapshot's own Overdue AR tile.
    not_yet_due = _invoice("INV001", "ACME", "B1", date(2026, 9, 1), Decimal("1000.00"))
    overdue = _invoice("INV002", "ACME", "B1", date(2025, 1, 1), Decimal("2000.00"))
    rows = compute_ageing_matrix([not_yet_due, overdue], [], [], {}, {}, as_of=date(2026, 9, 12))
    assert len(rows) == 1
    assert rows[0].total_open == Decimal("3000.00")
    assert rows[0].overdue_total == Decimal("2000.00")


def test_notional_interest_cost_summed_per_party_matches_per_invoice_formula():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("100000.00"))
    as_of = date(2026, 9, 12)
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=as_of)
    days_past_due = (as_of - date(2026, 1, 31)).days
    expected = Decimal("100000.00") * Decimal("0.10") * Decimal(days_past_due) / Decimal("365")
    assert rows[0].notional_interest_cost == expected


def test_notional_interest_cost_is_zero_for_a_not_yet_due_invoice():
    inv = _invoice("INV001", "ACME", "B1", date(2026, 9, 1), Decimal("1000.00"))
    rows = compute_ageing_matrix([inv], [], [], {}, {}, as_of=date(2026, 9, 12))
    assert rows[0].notional_interest_cost == Decimal("0.00")
