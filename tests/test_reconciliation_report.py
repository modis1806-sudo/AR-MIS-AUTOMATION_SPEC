from datetime import date
from decimal import Decimal

from ar_mis.models import WeeklySnapshotRow
from ar_mis.reconciliation_report import compute_tb_cross_check_summary, compute_unreconciled_parties


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
