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
from decimal import Decimal

from ar_mis.models import WeeklySnapshotRow


@dataclass
class TBCrossCheckSummary:
    parties_with_current_difference: int
    total_current_absolute_difference: Decimal


def compute_tb_cross_check_summary(rows: list[WeeklySnapshotRow]) -> TBCrossCheckSummary:
    """Summarizes CURRENT state, not a count across all of history: a
    party who mismatched three months ago and has reconciled clean every
    week since must not still count as "currently wrong" - only that
    party's most recently recorded week decides whether they're counted
    here. `rows` should be every recorded week for every party (full
    history, e.g. Store.all_weekly_snapshot_rows) - this function does
    its own latest-per-party reduction, so the caller doesn't need to
    pre-filter.
    """
    latest_by_party: dict[tuple[str, str], WeeklySnapshotRow] = {}
    for row in sorted(rows, key=lambda r: r.week_ending):
        latest_by_party[(row.party_id, row.branch_id)] = row

    mismatched = [r for r in latest_by_party.values() if not r.reconciled]
    return TBCrossCheckSummary(
        parties_with_current_difference=len(mismatched),
        total_current_absolute_difference=sum((abs(r.difference) for r in mismatched), Decimal("0.00")),
    )
