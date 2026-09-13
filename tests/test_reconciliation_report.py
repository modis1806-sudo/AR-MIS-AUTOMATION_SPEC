from datetime import date
from decimal import Decimal

from ar_mis.models import WeeklySnapshotRow
from ar_mis.reconciliation_report import compute_tb_cross_check_summary


def _row(party_id, branch_id, week_ending, reconciled, difference):
    return WeeklySnapshotRow(
        party_id=party_id, branch_id=branch_id, week_ending=week_ending,
        opening=Decimal("0.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
        debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
        closing_computed=Decimal("0.00"), closing_extracted=Decimal("0.00"),
        reconciled=reconciled, difference=difference,
    )


def test_empty_history_has_zero_summary():
    summary = compute_tb_cross_check_summary([])
    assert summary.parties_with_current_difference == 0
    assert summary.total_current_absolute_difference == Decimal("0.00")


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
