"""Weekly Movement Register - design doc item 15's fifth and final report
catalog piece, and the concrete, working form of the `weekly_snapshot`
table's original intent (item 1): one row per week-ending date, tracking
the portfolio's big numbers over time so a trend is visible, not just a
single as-of-date snapshot.

Resolved this session (design doc Open Item 4): **append-only stored
history**, not live recompute. A Preparer explicitly records the current
position as a given week's row (ar_mis.storage.Store.record_weekly_movement),
and that row is kept exactly as computed forever after - even if a later
correction to the underlying data would produce a different answer for
that same week if recomputed today. This is a deliberate departure from
every other report in this codebase (AR Snapshot, Branch Ageing, Exception
Register, Ageing Matrix), which all recompute live for whatever as-of
date is chosen - here, the historical record itself is the point.

Every KPI this table stores is one this codebase already computes, via
ar_mis.dashboard.compute_ar_snapshot - this module does no independent
calculation, it only picks out the subset of ARSnapshot's fields the
design doc names for this report and packages them for persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ar_mis.dashboard import ARSnapshot
from ar_mis.models import WeeklyMovementRow


def build_weekly_movement_row(snapshot: ARSnapshot, week_ending: date, recorded_at: datetime) -> WeeklyMovementRow:
    """`snapshot` should be computed with as_of=week_ending - this
    function trusts the caller on that rather than re-deriving week_ending
    from the snapshot itself, since ARSnapshot.as_of and the business
    week_ending being recorded are conceptually the same date but kept as
    separate parameters so a caller could deliberately record a week using
    a slightly different as_of if a real reason to ever arose (e.g.
    recording Friday's position as covering a week ending Sunday).
    """
    return WeeklyMovementRow(
        week_ending=week_ending,
        recorded_at=recorded_at,
        total_ar=snapshot.total_ar,
        open_ar_by_fy=dict(snapshot.open_ar_by_fy),
        pre_mis_outstanding=snapshot.pre_mis_outstanding,
        overdue_ar=snapshot.overdue_ar,
        overdue_by_bucket=dict(snapshot.overdue_by_bucket),
        dso=snapshot.dso,
        collection_efficiency=snapshot.collection_efficiency,
        unapplied_cash=snapshot.unapplied_cash,
    )


def compute_trend(current: Decimal | None, previous: Decimal | None) -> str | None:
    """"up" / "down" / "flat" comparing `current` to the prior week's
    value for the same field, or None when either side is missing (the
    very first recorded week has no prior week to compare against; DSO/
    CEI can themselves be None when there's no sales activity to divide
    by). Deliberately direction-only, not good/bad-colored - "up" means
    more for AR and less for Collection Efficiency alike, and coloring
    one direction as bad across every field here would silently assert a
    judgment (that rising AR is always bad, say) this design was never
    asked to make.
    """
    if current is None or previous is None:
        return None
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


@dataclass
class WeeklyMovementDisplayRow:
    row: WeeklyMovementRow
    total_ar_trend: str | None
    overdue_ar_trend: str | None
    dso_trend: str | None
    collection_efficiency_trend: str | None
    unapplied_cash_trend: str | None


def attach_trends(rows: list[WeeklyMovementRow]) -> list[WeeklyMovementDisplayRow]:
    """`rows` must already be sorted oldest-first (Store.all_weekly_movement_rows
    guarantees this) - each row's trend compares it to the row immediately
    before it in this list, i.e. the previous recorded week, not
    necessarily exactly seven days earlier (a gap in recording is shown
    as a trend against whatever the last recorded week actually was, not
    silently interpolated).
    """
    display_rows: list[WeeklyMovementDisplayRow] = []
    previous: WeeklyMovementRow | None = None
    for row in rows:
        display_rows.append(
            WeeklyMovementDisplayRow(
                row=row,
                total_ar_trend=compute_trend(row.total_ar, previous.total_ar if previous else None),
                overdue_ar_trend=compute_trend(row.overdue_ar, previous.overdue_ar if previous else None),
                dso_trend=compute_trend(row.dso, previous.dso if previous else None),
                collection_efficiency_trend=compute_trend(
                    row.collection_efficiency, previous.collection_efficiency if previous else None
                ),
                unapplied_cash_trend=compute_trend(row.unapplied_cash, previous.unapplied_cash if previous else None),
            )
        )
        previous = row
    return display_rows
