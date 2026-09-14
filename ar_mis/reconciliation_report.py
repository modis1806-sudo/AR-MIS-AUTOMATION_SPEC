"""TB Reconciliation Cross-Check - the client's explicit ask this
session: the Trial Balance / Sundry Debtors closing balance this app
already extracts from Tally (weekly_snapshot.closing_extracted) kept
visible in its own sheet, one row per party per branch per week,
alongside this app's own workings (opening + Sales + DN + CN + Receipts
+ Journals = closing_computed) and the difference between the two.

This module does no independent computation - every field it surfaces is
already stored by ar_mis.pipeline.process_branch_data on every run
(reconciled and unreconciled alike, since data is never discarded - see
that module's own docstring). It only adds the summary rollup: how many
parties currently show an unresolved difference, and how much, so a
Maker or Checker can tell at a glance whether today's consolidated
figures are trustworthy without scanning every row by hand.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import WeeklySnapshotRow


@dataclass
class TBCrossCheckSummary:
    parties_with_current_difference: int
    total_current_absolute_difference: Decimal
    total_debtor_as_per_books: Decimal


def compute_tb_cross_check_summary(
    rows: list[WeeklySnapshotRow], as_of: date | None = None
) -> TBCrossCheckSummary:
    """Summarizes CURRENT state, not a count across all of history: a
    party who mismatched three months ago and has reconciled clean every
    week since must not still count as "currently wrong" - only that
    party's most recently recorded week decides whether they're counted
    here. `rows` should be every recorded week for every party (full
    history, e.g. Store.all_weekly_snapshot_rows) - this function does
    its own latest-per-party reduction, so the caller doesn't need to
    pre-filter.

    `as_of` (client's explicit ask: a single running total they can hold
    up against a real Trial Balance run in Tally for a chosen date) scopes
    "latest" to each party's most recent week ON OR BEFORE that date,
    rather than the true latest regardless of date - None (the default)
    keeps the original all-time-latest behavior every existing caller
    already relies on. Always reduce over the FULL row history passed in,
    never a range-filtered subset: a party not touched again within a
    narrow display window still owes whatever their last known closing
    was, and dropping them would understate this total, not just narrow
    which weeks are shown.

    `total_debtor_as_per_books` is the sum of closing_extracted (Tally's
    own stated figure, never this app's own workings) across that same
    latest-per-party reduction - the one number a Maker or Checker can
    immediately compare against Tally's own Sundry Debtors total for the
    same date to confirm the pull is complete and correct.
    """
    eligible = rows if as_of is None else [r for r in rows if r.week_ending <= as_of]
    latest_by_party: dict[tuple[str, str], WeeklySnapshotRow] = {}
    for row in sorted(eligible, key=lambda r: r.week_ending):
        latest_by_party[(row.party_id, row.branch_id)] = row

    mismatched = [r for r in latest_by_party.values() if not r.reconciled]
    return TBCrossCheckSummary(
        parties_with_current_difference=len(mismatched),
        total_current_absolute_difference=sum((abs(r.difference) for r in mismatched), Decimal("0.00")),
        total_debtor_as_per_books=sum(
            (r.closing_extracted for r in latest_by_party.values()), Decimal("0.00")
        ),
    )
