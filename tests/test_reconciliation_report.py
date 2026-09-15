from datetime import date
from decimal import Decimal

from ar_mis.models import WeeklySnapshotRow
from ar_mis.reconciliation_report import (
    compute_concentration_risk,
    compute_tb_cross_check_summary,
    compute_unreconciled_parties,
)


def _row(party_id, branch_id, week_ending, reconciled, difference, closing_extracted=Decimal("0.00")):
    return WeeklySnapshotRow(
        party_id=party_id, branch_id=branch_id, week_ending=week_ending,
        opening=Decimal("0.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
        debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
        closing_computed=Decimal("0.00"), closing_extracted=closing_extracted,
        reconciled=reconciled, difference=difference,
    )


def test_empty_history_has_zero_summary():
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


def test_counts_only_the_latest_week_per_party():
    # P1 mismatched in week 1 but reconciled clean in week 2 - the more
    # recent, correct week must be the one that decides the summary.
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
        _row("P1", "KOL", date(2026, 1, 12), True, Decimal("0.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.parties_with_current_difference == 0
    assert summary.total_current_absolute_difference == Decimal("0.00")


def test_currently_mismatched_party_is_counted():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P1", "KOL", date(2026, 1, 12), False, Decimal("500.00")),
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


# ---- Total Debtor as per Books (client's explicit ask) -------------------


def test_total_debtor_as_per_books_sums_closing_extracted_across_latest_per_party():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-1000.00")),
        _row("P2", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-500.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.total_debtor_as_per_books == Decimal("-1500.00")


def test_total_debtor_as_per_books_uses_only_the_latest_week_per_party():
    # P1's balance changed between the two weeks - only the newer figure
    # should count, never both added together.
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-1000.00")),
        _row("P1", "KOL", date(2026, 1, 12), True, Decimal("0.00"), closing_extracted=Decimal("-1200.00")),
    ]
    summary = compute_tb_cross_check_summary(rows)
    assert summary.total_debtor_as_per_books == Decimal("-1200.00")


def test_as_of_restricts_the_summary_to_each_partys_latest_week_on_or_before_that_date():
    # A real Trial Balance run "as of" a date must reflect what was known
    # up to that date, never a week recorded after it.
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-1000.00")),
        _row("P1", "KOL", date(2026, 1, 19), False, Decimal("50.00"), closing_extracted=Decimal("-1200.00")),
    ]
    summary = compute_tb_cross_check_summary(rows, as_of=date(2026, 1, 12))
    assert summary.total_debtor_as_per_books == Decimal("-1000.00")
    assert summary.parties_with_current_difference == 0  # the later mismatch hasn't happened yet as of this date


def test_as_of_never_drops_a_party_whose_latest_week_falls_outside_a_narrower_window():
    # A party untouched again within some later display window still
    # owes their last known balance - the running total must not
    # silently shrink just because a narrower range is being browsed.
    rows = [_row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-1000.00"))]
    summary = compute_tb_cross_check_summary(rows, as_of=date(2026, 3, 1))
    assert summary.total_debtor_as_per_books == Decimal("-1000.00")


# ---- compute_unreconciled_parties (export list behind the tile) ----------


def test_unreconciled_parties_excludes_clean_parties():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P2", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
    ]
    mismatched = compute_unreconciled_parties(rows)
    assert [r.party_id for r in mismatched] == ["P2"]


def test_unreconciled_parties_uses_only_the_latest_week_per_party():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
        _row("P1", "KOL", date(2026, 1, 12), True, Decimal("0.00")),
    ]
    assert compute_unreconciled_parties(rows) == []


def test_unreconciled_parties_sorted_by_absolute_difference_descending():
    rows = [
        _row("SMALL", "KOL", date(2026, 1, 5), False, Decimal("50.00")),
        _row("BIG", "KOL", date(2026, 1, 5), False, Decimal("-9000.00")),
        _row("MEDIUM", "KOL", date(2026, 1, 5), False, Decimal("500.00")),
    ]
    mismatched = compute_unreconciled_parties(rows)
    assert [r.party_id for r in mismatched] == ["BIG", "MEDIUM", "SMALL"]


def test_unreconciled_parties_respects_as_of():
    rows = [
        _row("P1", "KOL", date(2026, 1, 5), True, Decimal("0.00")),
        _row("P1", "KOL", date(2026, 1, 19), False, Decimal("50.00")),
    ]
    assert compute_unreconciled_parties(rows, as_of=date(2026, 1, 12)) == []
    assert len(compute_unreconciled_parties(rows, as_of=date(2026, 1, 19))) == 1


# ---- Concentration Risk (client's explicit ask) --------------------------


def test_concentration_risk_empty_history():
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


def test_concentration_risk_uses_only_the_latest_week_per_party():
    rows = [
        _row("A", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("1000.00")),
        _row("A", "KOL", date(2026, 1, 12), True, Decimal("0.00"), closing_extracted=Decimal("4000.00")),
    ]
    summary = compute_concentration_risk(rows)
    assert summary.total_ar == Decimal("4000.00")
    assert summary.top_parties[0].outstanding == Decimal("4000.00")


def test_concentration_risk_as_of_excludes_a_week_recorded_after_that_date():
    rows = [
        _row("A", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("1000.00")),
        _row("A", "KOL", date(2026, 1, 19), True, Decimal("0.00"), closing_extracted=Decimal("4000.00")),
    ]
    summary = compute_concentration_risk(rows, as_of=date(2026, 1, 12))
    assert summary.total_ar == Decimal("1000.00")


def test_concentration_risk_credit_balance_party_sorts_to_the_bottom_not_inflated():
    rows = [
        _row("DEBTOR", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("5000.00")),
        _row("CREDIT_BAL", "KOL", date(2026, 1, 5), True, Decimal("0.00"), closing_extracted=Decimal("-2000.00")),
    ]
    summary = compute_concentration_risk(rows, top_n=10)
    assert [p.party_id for p in summary.top_parties] == ["DEBTOR", "CREDIT_BAL"]
    assert summary.total_ar == Decimal("3000.00")
