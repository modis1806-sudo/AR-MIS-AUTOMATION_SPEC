from datetime import date

from ar_mis.config import snap_to_full_weeks, split_into_weeks, week_end, week_start


def test_week_start_and_end_for_a_mid_week_date():
    # Thursday 2026-09-17 -> Monday 2026-09-14 through Sunday 2026-09-20.
    d = date(2026, 9, 17)
    assert week_start(d) == date(2026, 9, 14)
    assert week_end(d) == date(2026, 9, 20)


def test_week_start_and_end_when_date_is_already_monday_or_sunday():
    monday = date(2026, 9, 14)
    sunday = date(2026, 9, 20)
    assert week_start(monday) == monday
    assert week_end(monday) == sunday
    assert week_start(sunday) == monday
    assert week_end(sunday) == sunday


def test_snap_to_full_weeks_extends_outward_never_narrows():
    # Thursday to next Wednesday - a range that starts and ends mid-week.
    from_date, to_date = date(2026, 9, 17), date(2026, 9, 23)
    snapped_from, snapped_to = snap_to_full_weeks(from_date, to_date)
    assert snapped_from == date(2026, 9, 14)  # Monday on/before from_date
    assert snapped_to == date(2026, 9, 27)  # Sunday on/after to_date
    # Never narrower than what was asked for.
    assert snapped_from <= from_date
    assert snapped_to >= to_date


def test_snap_to_full_weeks_is_a_no_op_for_an_already_aligned_range():
    from_date, to_date = date(2026, 9, 14), date(2026, 9, 20)
    assert snap_to_full_weeks(from_date, to_date) == (from_date, to_date)


def test_split_into_weeks_single_week():
    weeks = split_into_weeks(date(2026, 9, 14), date(2026, 9, 20))
    assert weeks == [(date(2026, 9, 14), date(2026, 9, 20))]


def test_split_into_weeks_multiple_weeks():
    weeks = split_into_weeks(date(2026, 9, 14), date(2026, 10, 4))
    assert weeks == [
        (date(2026, 9, 14), date(2026, 9, 20)),
        (date(2026, 9, 21), date(2026, 9, 27)),
        (date(2026, 9, 28), date(2026, 10, 4)),
    ]


def test_snap_then_split_covers_a_mid_week_range_completely_in_full_weeks():
    # An operator picking an arbitrary Thu-to-Wed range must still get
    # complete, correctly-bounded weeks out the other end - never a
    # partial week silently included.
    snapped = snap_to_full_weeks(date(2026, 9, 17), date(2026, 9, 23))
    weeks = split_into_weeks(*snapped)
    assert weeks == [
        (date(2026, 9, 14), date(2026, 9, 20)),
        (date(2026, 9, 21), date(2026, 9, 27)),
    ]
    for start, end in weeks:
        assert start.weekday() == 0  # Monday
        assert end.weekday() == 6  # Sunday
