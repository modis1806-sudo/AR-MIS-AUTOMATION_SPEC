from datetime import date
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, NoteType, SalesDNRegisterRow
from ar_mis.reconciliation_report import (
    TBCrossCheckRow,
    compute_concentration_risk,
    compute_tb_cross_check_rows,
    compute_tb_cross_check_summary,
    compute_unreconciled_parties,
)
from ar_mis.storage import Store


def _row(party_id, branch_id, as_of, reconciled, difference, closing_extracted=Decimal("0.00")):
    return TBCrossCheckRow(
        party_id=party_id, branch_id=branch_id, as_of=as_of,
        opening=Decimal("0.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
        debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
        closing_computed=Decimal("0.00"), closing_extracted=closing_extracted,
        reconciled=reconciled, difference=difference,
    )


# ---- compute_tb_cross_check_summary ---------------------------------------


def test_empty_rows_has_zero_summary():
    summary = compute_tb_cross_check_summary([])
    assert summary.parties_with_current_difference == 0
    assert summary.total_current_absolute_difference == Decimal("0.00")
    assert summary.total_debtor_as_per_books == Decimal("0.00")


def test_all_reconciled_parties_show_zero_summary():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P2", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.parties_with_current_difference == 0
    assert summary.total_current_absolute_difference == Decimal("0.00")


def test_currently_mismatched_party_is_counted():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P2", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.parties_with_current_difference == 1
    assert summary.total_current_absolute_difference == Decimal("500.00")


def test_difference_is_summed_as_absolute_value():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), False, Decimal("-500.00")),
        _row("P2", "KOL", date(2026, 1, 5), False, Decimal("300.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.parties_with_current_difference == 2
    assert summary.total_current_absolute_difference == Decimal("800.00")


def test_same_party_different_branches_tracked_separately():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), False, Decimal("100.00")),
        _row("P1", "MUM", date(2026, 1, 5), False, Decimal("200.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.parties_with_current_difference == 2
    assert summary.total_current_absolute_difference == Decimal("300.00")


def test_total_debtor_as_per_books_sums_closing_extracted_across_every_row():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-1000.00")),
        _row("P2", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-500.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.total_debtor_as_per_books == Decimal("-1500.00")


# ---- compute_unreconciled_parties (export list behind the tile) ----------


def test_unreconciled_parties_excludes_clean_parties():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P2", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
    ]
    mismatched = compute_unreconciled_parties(rows)
    assert [r.party_id for r in mismatched] == ["P2"]


def test_unreconciled_parties_sorted_by_absolute_difference_descending():
    rows = [
        _row("SMALL", "KOL", date(2026, 1, 5), False, Decimal("50.00")),
        _row("BIG", "KOL", date(2026, 1, 5), False, Decimal("-9000.00")),
        _row("MEDIUM", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
    ]
    mismatched = compute_unreconciled_parties(rows)
    assert [r.party_id for r in mismatched] == ["BIG", "MEDIUM", "SMALL"]


# ---- Concentration Risk (client's explicit ask) --------------------------


def test_concentration_risk_empty_rows():
    summary = compute_concentration_risk([])
    assert summary.total_ar == Decimal("0.00")
    assert summary.top_parties == []
    assert summary.top_n_total == Decimal("0.00")
    assert summary.top_n_percent == Decimal("0.00")


def test_concentration_risk_ranks_by_outstanding_descending():
    rows = [
        _row("SMALL", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("1000.00")),
        _row("BIG", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("9000.00")),
        _row("MEDIUM", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("5000.00")),
    ]
    summary = compute_concentration_risk(rows, top_n=10)
    assert [p.party_id for p in summary.top_parties] == ["BIG", "MEDIUM", "SMALL"]
    assert summary.total_ar == Decimal("15000.00")


def test_concentration_risk_percent_of_total_is_correct():
    rows = [
        _row("A", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("7500.00")),
        _row("B", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("2500.00")),
    ]
    summary = compute_concentration_risk(rows, top_n=10)
    a = next(p for p in summary.top_parties if p.party_id == "A")
    b = next(p for p in summary.top_parties if p.party_id == "B")
    assert a.percent_of_total == Decimal("75.00")
    assert b.percent_of_total == Decimal("25.00")
    assert summary.top_n_total == Decimal("10000.00")
    assert summary.top_n_percent == Decimal("100.00")


def test_concentration_risk_respects_top_n_limit():
    rows = [
        _row(f"P{i}", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal(str(i * 100)))
        for i in range(1, 6)
    ]
    summary = compute_concentration_risk(rows, top_n=2)
    assert len(summary.top_parties) == 2
    assert [p.party_id for p in summary.top_parties] == ["P5", "P4"]
    # top_n_percent reflects only the top 2, but against the FULL total AR.
    full_total = sum(Decimal(str(i * 100)) for i in range(1, 6))
    assert summary.total_ar == full_total
    assert summary.top_n_total == Decimal("900.00")  # 500 + 400


def test_concentration_risk_credit_balance_party_sorts_to_the_bottom_not_inflated():
    rows = [
        _row("DEBTOR", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("5000.00")),
        _row("CREDIT_BAL", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-2000.00")),
    ]
    summary = compute_concentration_risk(rows, top_n=10)
    assert [p.party_id for p in summary.top_parties] == ["DEBTOR", "CREDIT_BAL"]
    assert summary.total_ar == Decimal("3000.00")


# ---- compute_tb_cross_check_rows: the live comparison itself -------------


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_compute_tb_cross_check_rows_is_empty_with_nothing_on_record(tmp_path):
    store = _store(tmp_path)
    assert compute_tb_cross_check_rows(store) == []
    store.close()


def test_compute_tb_cross_check_rows_matches_pre_mis_outstanding_with_no_registers(tmp_path):
    store = _store(tmp_path)
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("1000.00")))
    store.upsert_party_tb_balance("Acme", "KOL", date(2026, 4, 7), Decimal("1000.00"))
    rows = compute_tb_cross_check_rows(store)
    store.close()
    assert len(rows) == 1
    assert rows[0].party_id == "Acme"
    assert rows[0].as_of == date(2026, 4, 7)
    assert rows[0].closing_computed == Decimal("1000.00")
    assert rows[0].reconciled is True
    assert rows[0].difference == Decimal("0.00")


def test_compute_tb_cross_check_rows_flags_a_real_mismatch(tmp_path):
    store = _store(tmp_path)
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    store.upsert_party_tb_balance("Acme", "KOL", date(2026, 4, 7), Decimal("99999.00"))
    rows = compute_tb_cross_check_rows(store)
    store.close()
    assert rows[0].reconciled is False
    assert rows[0].difference == Decimal("-99999.00")


def test_compute_tb_cross_check_rows_recomputes_fresh_not_from_storage(tmp_path):
    # The whole point of the redesign: nothing about the Workings figure
    # is ever stored - a register row appended AFTER the TB reading was
    # taken must still be picked up the next time this is called,
    # because it's always a live recompute, never a frozen snapshot.
    store = _store(tmp_path)
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    store.upsert_party_tb_balance("Acme", "KOL", date(2026, 4, 7), Decimal("20000.00"))
    assert compute_tb_cross_check_rows(store)[0].reconciled is False

    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.INVOICE,
            voucher_number="SB/1", bill_allocation_reference="SB/1", party_id="Acme",
            taxable_value=Decimal("20000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("20000.00"), due_date=date(2026, 5, 6),
        )
    )
    assert compute_tb_cross_check_rows(store)[0].reconciled is True
    store.close()


def test_compute_tb_cross_check_rows_scopes_each_party_to_its_own_as_of_date(tmp_path):
    # Different branches extract on different schedules - a party whose
    # own reading is a week behind another party's must still be
    # recomputed against its OWN as_of, not whichever is most recent.
    store = _store(tmp_path)
    store.upsert_customer_master(CustomerMasterRecord("Early", "Early", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("Late", "Late", "KOL", Decimal("0.00")))
    store.upsert_party_tb_balance("Early", "KOL", date(2026, 4, 7), Decimal("10000.00"))
    store.upsert_party_tb_balance("Late", "KOL", date(2026, 4, 14), Decimal("30000.00"))
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.INVOICE,
            voucher_number="SB/EARLY", bill_allocation_reference="SB/EARLY", party_id="Early",
            taxable_value=Decimal("10000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("10000.00"), due_date=date(2026, 5, 6),
        )
    )
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 13), note_type=NoteType.INVOICE,
            voucher_number="SB/LATE", bill_allocation_reference="SB/LATE", party_id="Late",
            taxable_value=Decimal("30000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("30000.00"), due_date=date(2026, 5, 13),
        )
    )
    rows = {r.party_id: r for r in compute_tb_cross_check_rows(store)}
    store.close()
    assert rows["Early"].reconciled is True
    assert rows["Late"].reconciled is True


def test_compute_tb_cross_check_rows_filters_by_branch(tmp_path):
    store = _store(tmp_path)
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("P2", "P2", "MUM", Decimal("0.00")))
    store.upsert_party_tb_balance("P1", "KOL", date(2026, 4, 7), Decimal("100.00"))
    store.upsert_party_tb_balance("P2", "MUM", date(2026, 4, 7), Decimal("200.00"))
    kol_rows = compute_tb_cross_check_rows(store, "KOL")
    store.close()
    assert [r.party_id for r in kol_rows] == ["P1"]
