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


@dataclass
class ConcentrationRiskParty:
    party_id: str
    branch_id: str
    outstanding: Decimal
    percent_of_total: Decimal


@dataclass
class ConcentrationRiskSummary:
    total_ar: Decimal
    top_parties: list[ConcentrationRiskParty]
    top_n_total: Decimal
    top_n_percent: Decimal


def _latest_per_party(rows: list[WeeklySnapshotRow], as_of: date | None) -> list[WeeklySnapshotRow]:
    """Shared reduction behind both compute_tb_cross_check_summary and
    compute_unreconciled_parties - only each party's most recently
    recorded week (on or before `as_of`, when given) reflects their
    CURRENT state; earlier weeks for the same party are history, not
    still-open exceptions.
    """
    eligible = rows if as_of is None else [r for r in rows if r.week_ending <= as_of]
    latest_by_party: dict[tuple[str, str], WeeklySnapshotRow] = {}
    for row in sorted(eligible, key=lambda r: r.week_ending):
        latest_by_party[(row.party_id, row.branch_id)] = row
    return list(latest_by_party.values())


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
    latest = _latest_per_party(rows, as_of)
    mismatched = [r for r in latest if not r.reconciled]
    return TBCrossCheckSummary(
        parties_with_current_difference=len(mismatched),
        total_current_absolute_difference=sum((abs(r.difference) for r in mismatched), Decimal("0.00")),
        total_debtor_as_per_books=sum((r.closing_extracted for r in latest), Decimal("0.00")),
    )


def compute_unreconciled_parties(
    rows: list[WeeklySnapshotRow], as_of: date | None = None
) -> list[WeeklySnapshotRow]:
    """The actual rows behind TBCrossCheckSummary.parties_with_current_
    difference - client's explicit ask: an "export unreconciled parties"
    action next to that tile, rather than making a Maker scroll the full
    TB Cross-Check table hunting for the non-zero Difference rows by eye.
    Same latest-per-party-as-of-`as_of` reduction as the summary tile, so
    the exported list can never disagree with the count shown on screen.
    Sorted by absolute difference, largest first - the ones most worth a
    human's attention lead the file, not whatever order the database
    happens to return.
    """
    latest = _latest_per_party(rows, as_of)
    mismatched = [r for r in latest if not r.reconciled]
    mismatched.sort(key=lambda r: abs(r.difference), reverse=True)
    return mismatched


def compute_concentration_risk(
    rows: list[WeeklySnapshotRow], as_of: date | None = None, top_n: int = 10
) -> ConcentrationRiskSummary:
    """AR concentration risk (client's explicit ask): are receivables
    spread across many customers, or does a handful of them make up most
    of the exposure - a real risk in its own right, independent of
    whether any of them are currently overdue.

    Same latest-per-party-as-of-`as_of` reduction, and the same
    `closing_extracted` figure (Tally's own stated truth, never this
    app's own workings), as compute_tb_cross_check_summary's
    `total_debtor_as_per_books` - deliberately, so this screen's "Total
    AR" can never quietly disagree with the number already shown on TB
    Cross-Check. Ranked by each party's own signed outstanding (Dr
    positive/Cr negative, Section 2.3) descending, so real debtors lead
    and a credit-balance party sorts to the bottom rather than
    inflating anyone's concentration figure.

    `percent_of_total` is computed against the full `total_ar` (every
    party, not just the ones shown) - a party can be a startling
    percentage of a small total AR base even while sitting well down
    the absolute-amount ranking, so the percentage always reflects
    reality, never just the visible top_n slice.
    """
    latest = _latest_per_party(rows, as_of)
    total_ar = sum((r.closing_extracted for r in latest), Decimal("0.00"))
    ranked = sorted(latest, key=lambda r: r.closing_extracted, reverse=True)[:top_n]

    def _percent(amount: Decimal) -> Decimal:
        if total_ar == 0:
            return Decimal("0.00")
        return (amount / total_ar * 100).quantize(Decimal("0.01"))

    top_parties = [
        ConcentrationRiskParty(
            party_id=r.party_id, branch_id=r.branch_id,
            outstanding=r.closing_extracted, percent_of_total=_percent(r.closing_extracted),
        )
        for r in ranked
    ]
    top_n_total = sum((p.outstanding for p in top_parties), Decimal("0.00"))
    return ConcentrationRiskSummary(
        total_ar=total_ar, top_parties=top_parties,
        top_n_total=top_n_total, top_n_percent=_percent(top_n_total),
    )
