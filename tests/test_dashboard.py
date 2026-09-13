from datetime import date
from decimal import Decimal

import pytest

from ar_mis.dashboard import (
    NOTIONAL_INTEREST_RATE,
    compute_ar_snapshot,
    compute_branch_ageing_schedule,
)
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    LedgerEntry,
    NoteType,
    PartyGrouping,
    ReceiptJournalRegisterRow,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
)
from ar_mis.registers import RegisterBuildExceptions, build_sales_dn_register_row

CUSTOMER = CustomerMasterRecord(
    party_id="ACME", party_name="Acme Corp", branch_id="B1", pre_mis_outstanding=Decimal("0.00")
)


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


# ---- compute_ar_snapshot --------------------------------------------------


def test_total_ar_includes_open_invoices_and_pre_mis_outstanding():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    customers = {
        ("ACME", "B1"): CustomerMasterRecord(
            party_id="ACME", party_name="Acme", branch_id="B1", pre_mis_outstanding=Decimal("500.00")
        )
    }
    snap = compute_ar_snapshot([invoice], [], [], customers, ptp_kept_rate=None, as_of=date(2026, 5, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.total_ar == Decimal("1500.00")  # 1000 open + 500 pre-MIS
    assert snap.pre_mis_outstanding == Decimal("500.00")


def test_open_ar_by_fy_groups_correctly():
    inv_fy25 = _invoice("INV001", "ACME", "B1", date(2026, 3, 20), Decimal("1000.00"))
    inv_fy26 = _invoice("INV002", "ACME", "B1", date(2026, 4, 5), Decimal("2000.00"))
    snap = compute_ar_snapshot([inv_fy25, inv_fy26], [], [], {}, ptp_kept_rate=None, as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.open_ar_by_fy == {"2025-26": Decimal("1000.00"), "2026-27": Decimal("2000.00")}


def test_related_party_ar_split_from_sundry_debtor():
    inv_related = _invoice(
        "INV001", "RELCO", "B1", date(2026, 1, 1), Decimal("1000.00"),
        customer=CustomerMasterRecord(party_id="RELCO", party_name="RelCo", branch_id="B1",
                                       pre_mis_outstanding=Decimal("0.00"), grouping=PartyGrouping.RELATED_PARTY),
    )
    inv_sundry = _invoice(
        "INV002", "ACME", "B1", date(2026, 1, 1), Decimal("500.00"),
        customer=CustomerMasterRecord(party_id="ACME", party_name="Acme", branch_id="B1",
                                       pre_mis_outstanding=Decimal("0.00"), grouping=PartyGrouping.SUNDRY_DEBTOR),
    )
    customers = {
        ("RELCO", "B1"): CustomerMasterRecord(party_id="RELCO", party_name="RelCo", branch_id="B1",
                                               pre_mis_outstanding=Decimal("0.00"), grouping=PartyGrouping.RELATED_PARTY),
        ("ACME", "B1"): CustomerMasterRecord(party_id="ACME", party_name="Acme", branch_id="B1",
                                              pre_mis_outstanding=Decimal("0.00"), grouping=PartyGrouping.SUNDRY_DEBTOR),
    }
    snap = compute_ar_snapshot([inv_related, inv_sundry], [], [], customers, ptp_kept_rate=None,
                                as_of=date(2026, 6, 1), latest_weekly_snapshot_closing_total=None)
    assert snap.related_party_ar == Decimal("1000.00")
    assert snap.sundry_debtor_ar == Decimal("500.00")


def test_unapplied_cash_and_cn_are_summed_across_all_parties():
    rj_rows = [
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 5), voucher_type="Receipt",
                                   voucher_number="R1", party_id="ACME", amount=Decimal("300.00"), target_doc_no=None),
        ReceiptJournalRegisterRow(branch_id="B1", txn_date=date(2026, 1, 5), voucher_type="Receipt",
                                   voucher_number="R2", party_id="RELCO", amount=Decimal("200.00"), target_doc_no=None),
    ]
    cn_rows = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 5), voucher_number="CN1",
                               party_id="ACME", cn_amount=Decimal("50.00"), bill_allocation_reference=None),
    ]
    snap = compute_ar_snapshot([], cn_rows, rj_rows, {}, ptp_kept_rate=None, as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.unapplied_cash == Decimal("500.00")
    assert snap.unapplied_cn == Decimal("50.00")


def test_overdue_ar_bad_debt_risk_and_notional_interest():
    # Invoice due 2026-01-31 (30-day credit period), fully open, as-of far overdue.
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("100000.00"))
    as_of = date(2026, 9, 12)  # 224 days past due -> 181+ bucket
    snap = compute_ar_snapshot([invoice], [], [], {}, ptp_kept_rate=None, as_of=as_of,
                                latest_weekly_snapshot_closing_total=None)
    assert snap.overdue_ar == Decimal("100000.00")
    assert snap.bad_debt_risk_180_plus == Decimal("100000.00")
    assert snap.overdue_by_bucket == {"181+": Decimal("100000.00")}
    days_past_due = (as_of - date(2026, 1, 31)).days
    expected_interest = Decimal("100000.00") * NOTIONAL_INTEREST_RATE * Decimal(days_past_due) / Decimal("365")
    assert snap.notional_interest_cost == expected_interest
    assert snap.overdue_pct == Decimal("100")


def test_rounding_difference_is_none_without_a_weekly_snapshot_baseline():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    snap = compute_ar_snapshot([invoice], [], [], {}, ptp_kept_rate=None, as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.rounding_difference is None


def test_rounding_difference_compares_against_the_legacy_total():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    snap = compute_ar_snapshot([invoice], [], [], {}, ptp_kept_rate=None, as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=Decimal("998.50"))
    assert snap.rounding_difference == Decimal("1.50")


def test_ptp_kept_rate_is_passed_through_unchanged():
    snap = compute_ar_snapshot([], [], [], {}, ptp_kept_rate=Decimal("75.00"), as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.ptp_kept_rate == Decimal("75.00")


def test_average_collection_period_computed_per_branch():
    inv_b1 = _invoice("INV001", "ACME", "B1", date(2026, 4, 1), Decimal("900000.00"))
    inv_b2 = _invoice("INV002", "ACME", "B2", date(2026, 4, 1), Decimal("450000.00"))
    as_of = date(2026, 6, 30)
    snap = compute_ar_snapshot([inv_b1, inv_b2], [], [], {}, ptp_kept_rate=None, as_of=as_of,
                                latest_weekly_snapshot_closing_total=None)
    assert set(snap.average_collection_period_by_branch) == {"B1", "B2"}
    assert snap.average_collection_period_by_branch["B1"] is not None
    assert snap.average_collection_period_by_branch["B2"] is not None


def test_empty_portfolio_produces_no_crash_with_sensible_nones():
    snap = compute_ar_snapshot([], [], [], {}, ptp_kept_rate=None, as_of=date(2026, 6, 1),
                                latest_weekly_snapshot_closing_total=None)
    assert snap.total_ar == Decimal("0.00")
    assert snap.overdue_pct is None
    assert snap.dso is None
    assert snap.collection_efficiency is None


# ---- compute_branch_ageing_schedule ---------------------------------------


def test_branch_ageing_schedule_one_row_per_branch_plus_total():
    inv_b1 = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    inv_b2 = _invoice("INV002", "ACME", "B2", date(2026, 1, 1), Decimal("500.00"))
    as_of = date(2026, 9, 12)
    rows = compute_branch_ageing_schedule([inv_b1, inv_b2], [], [], as_of)
    branch_ids = [r.branch_id for r in rows]
    assert branch_ids == ["B1", "B2", "All Branches"]

    b1_row = rows[0]
    assert b1_row.buckets["181+"] == Decimal("1000.00")
    assert b1_row.total == Decimal("1000.00")

    total_row = rows[-1]
    assert total_row.buckets["181+"] == Decimal("1500.00")
    assert total_row.total == Decimal("1500.00")


def test_branch_ageing_schedule_every_bucket_present_even_when_empty():
    invoice = _invoice("INV001", "ACME", "B1", date(2026, 1, 1), Decimal("1000.00"))
    rows = compute_branch_ageing_schedule([invoice], [], [], date(2026, 1, 15))  # not yet due -> Current
    b1_row = rows[0]
    assert set(b1_row.buckets) == {"Current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "181+"}
    assert b1_row.buckets["Current"] == Decimal("1000.00")
    assert b1_row.buckets["1-30"] == Decimal("0.00")


def test_branch_ageing_schedule_empty_portfolio_has_only_total_row():
    rows = compute_branch_ageing_schedule([], [], [], date(2026, 1, 1))
    assert len(rows) == 1
    assert rows[0].branch_id == "All Branches"
    assert rows[0].total == Decimal("0.00")
