from datetime import date, datetime
from decimal import Decimal

from ar_mis.dashboard import ARSnapshot
from ar_mis.models import WeeklyMovementRow
from ar_mis.weekly_movement import attach_trends, build_weekly_movement_row, compute_trend

SNAPSHOT = ARSnapshot(
    as_of=date(2026, 9, 12),
    total_ar=Decimal("100000.00"),
    open_ar_by_fy={"2026-27": Decimal("100000.00")},
    pre_mis_outstanding=Decimal("5000.00"),
    unapplied_cash=Decimal("2000.00"),
    unapplied_cn=Decimal("0.00"),
    related_party_ar=Decimal("0.00"),
    sundry_debtor_ar=Decimal("100000.00"),
    rounding_difference=None,
    overdue_ar=Decimal("30000.00"),
    overdue_by_bucket={"1-30": Decimal("30000.00")},
    overdue_pct=Decimal("30.00"),
    bad_debt_risk_180_plus=Decimal("0.00"),
    notional_interest_cost=Decimal("0.00"),
    dso=Decimal("45.00"),
    collection_efficiency=Decimal("80.00"),
    ptp_kept_rate=None,
)


def _row(week_ending, total_ar, overdue_ar, dso, cei, unapplied_cash):
    return WeeklyMovementRow(
        week_ending=week_ending,
        recorded_at=datetime(2026, 9, 12, 10, 0),
        total_ar=total_ar,
        open_ar_by_fy={},
        pre_mis_outstanding=Decimal("0.00"),
        overdue_ar=overdue_ar,
        overdue_by_bucket={},
        dso=dso,
        collection_efficiency=cei,
        unapplied_cash=unapplied_cash,
    )


# ---- build_weekly_movement_row --------------------------------------------


def test_build_weekly_movement_row_maps_snapshot_fields():
    row = build_weekly_movement_row(SNAPSHOT, week_ending=date(2026, 9, 12), recorded_at=datetime(2026, 9, 12, 9, 30))
    assert row.week_ending == date(2026, 9, 12)
    assert row.recorded_at == datetime(2026, 9, 12, 9, 30)
    assert row.total_ar == Decimal("100000.00")
    assert row.open_ar_by_fy == {"2026-27": Decimal("100000.00")}
    assert row.pre_mis_outstanding == Decimal("5000.00")
    assert row.overdue_ar == Decimal("30000.00")
    assert row.overdue_by_bucket == {"1-30": Decimal("30000.00")}
    assert row.dso == Decimal("45.00")
    assert row.collection_efficiency == Decimal("80.00")
    assert row.unapplied_cash == Decimal("2000.00")


# ---- compute_trend ----------------------------------------------------------


def test_compute_trend_up():
    assert compute_trend(Decimal("110"), Decimal("100")) == "up"


def test_compute_trend_down():
    assert compute_trend(Decimal("90"), Decimal("100")) == "down"


def test_compute_trend_flat():
    assert compute_trend(Decimal("100"), Decimal("100")) == "flat"


def test_compute_trend_none_when_no_previous():
    assert compute_trend(Decimal("100"), None) is None


def test_compute_trend_none_when_current_none():
    assert compute_trend(None, Decimal("100")) is None


# ---- attach_trends -----------------------------------------------------------


def test_attach_trends_first_row_has_no_trend():
    rows = [_row(date(2026, 1, 5), Decimal("100000"), Decimal("20000"), Decimal("40"), Decimal("80"), Decimal("1000"))]
    display = attach_trends(rows)
    assert len(display) == 1
    assert display[0].total_ar_trend is None
    assert display[0].overdue_ar_trend is None
    assert display[0].dso_trend is None
    assert display[0].collection_efficiency_trend is None
    assert display[0].unapplied_cash_trend is None


def test_attach_trends_second_row_compares_to_first():
    rows = [
        _row(date(2026, 1, 5), Decimal("100000"), Decimal("20000"), Decimal("40"), Decimal("80"), Decimal("1000")),
        _row(date(2026, 1, 12), Decimal("120000"), Decimal("15000"), Decimal("40"), Decimal("85"), Decimal("500")),
    ]
    display = attach_trends(rows)
    assert display[1].total_ar_trend == "up"
    assert display[1].overdue_ar_trend == "down"
    assert display[1].dso_trend == "flat"
    assert display[1].collection_efficiency_trend == "up"
    assert display[1].unapplied_cash_trend == "down"


def test_attach_trends_handles_none_dso_gracefully():
    rows = [
        _row(date(2026, 1, 5), Decimal("100000"), Decimal("20000"), None, None, Decimal("1000")),
        _row(date(2026, 1, 12), Decimal("120000"), Decimal("15000"), Decimal("40"), Decimal("85"), Decimal("500")),
    ]
    display = attach_trends(rows)
    assert display[1].dso_trend is None
    assert display[1].collection_efficiency_trend is None
