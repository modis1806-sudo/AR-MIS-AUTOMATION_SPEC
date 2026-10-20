"""TB Reconciliation Cross-Check - the client's explicit ask this
session, redesigned again later the same session (client's further
call): Tally's own stated closing balance per party, compared against
this app's own workings, recomputed LIVE every time this is viewed -
never read back from a stored per-week history.

weekly_snapshot (the original design here) stored one row per party per
branch per week, forever - the client's own, correct objection: that
table's only real job was to cross-verify the registers' sanity and
give a current Total AR, neither of which needed a growing history to
do. The registers themselves (append-only, every voucher already
date-stamped) can reconstruct a party's cumulative position as of any
date on demand (ar_mis.rollforward.compute_party_closings_as_of), so the
only fact worth persisting at all is the one thing nothing else can
reconstruct after the fact: what Tally itself said, right now
(Store.party_tb_balance, upserted - never appended - on every
extraction).

So this module now does real computation, not just a rollup over
already-stored figures: compute_tb_cross_check_rows is the live
Workings-vs-Tally comparison itself, one row per party, always exactly
one - there is no "latest per party" reduction left to do, because
there is no history to reduce. The three summary functions below it
take that rows list directly.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.money import difference, is_match
from ar_mis.rollforward import compute_party_closings_as_of


@dataclass(frozen=True)
class TBCrossCheckRow:
    party_id: str
    branch_id: str
    as_of: date
    opening: Decimal
    sales: Decimal
    credit_notes: Decimal
    debit_notes: Decimal
    receipts: Decimal
    journals: Decimal
    closing_computed: Decimal
    closing_extracted: Decimal
    reconciled: bool
    difference: Decimal


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


def compute_tb_cross_check_rows(store, branch_id: str | None = None) -> list[TBCrossCheckRow]:
    """The live comparison itself: Tally's current reading per party
    (Store.all_party_tb_balances) against a fresh recompute of that same
    party's cumulative position, as of THAT reading's own date, from
    every register row on record (ar_mis.rollforward.
    compute_party_closings_as_of) - exactly Section 2.4's Opening+
    Movements=Closing formula, evaluated from scratch, never read back
    from storage.

    Different parties can legitimately have different `as_of` dates
    (branches extract on their own schedules) - grouped by (branch_id,
    as_of) so every party sharing one run's own reading gets recomputed
    in a single pass over that branch's registers, rather than one pass
    per party.
    """
    tb_balances = store.all_party_tb_balances(branch_id)
    groups: dict[tuple[str, date], set[str]] = defaultdict(set)
    for (party_id, b_id), balance in tb_balances.items():
        groups[(b_id, balance.as_of)].add(party_id)

    rows: list[TBCrossCheckRow] = []
    for (b_id, as_of), parties in groups.items():
        movements = compute_party_closings_as_of(store, b_id, parties, as_of)
        for party_id in parties:
            m = movements[party_id]
            closing_extracted = tb_balances[(party_id, b_id)].closing_extracted
            rows.append(
                TBCrossCheckRow(
                    party_id=party_id, branch_id=b_id, as_of=as_of,
                    opening=m.opening, sales=m.sales, credit_notes=m.credit_notes,
                    debit_notes=m.debit_notes, receipts=m.receipts, journals=m.journals,
                    closing_computed=m.closing_computed, closing_extracted=closing_extracted,
                    reconciled=is_match(m.closing_computed, closing_extracted),
                    difference=difference(m.closing_computed, closing_extracted),
                )
            )
    return rows


def compute_tb_cross_check_summary(rows: list[TBCrossCheckRow]) -> TBCrossCheckSummary:
    """Summarizes the CURRENT state - `rows` is already exactly one row
    per party (compute_tb_cross_check_rows' own guarantee), so there is
    nothing further to reduce here, unlike the old per-week-history
    version this replaces.

    `total_debtor_as_per_books` is the sum of closing_extracted (Tally's
    own stated figure, never this app's own workings) - the one number a
    Maker or Checker can immediately compare against Tally's own Sundry
    Debtors total to confirm the pull is complete and correct.
    """
    mismatched = [r for r in rows if not r.reconciled]
    return TBCrossCheckSummary(
        parties_with_current_difference=len(mismatched),
        total_current_absolute_difference=sum((abs(r.difference) for r in mismatched), Decimal("0.00")),
        total_debtor_as_per_books=sum((r.closing_extracted for r in rows), Decimal("0.00")),
    )


def compute_unreconciled_parties(rows: list[TBCrossCheckRow]) -> list[TBCrossCheckRow]:
    """The actual rows behind TBCrossCheckSummary.parties_with_current_
    difference - client's explicit ask: an "export unreconciled parties"
    action next to that tile, rather than making a Maker scroll the full
    TB Cross-Check table hunting for the non-zero Difference rows by eye.
    Sorted by absolute difference, largest first - the ones most worth a
    human's attention lead the file, not whatever order the database
    happens to return.
    """
    mismatched = [r for r in rows if not r.reconciled]
    mismatched.sort(key=lambda r: abs(r.difference), reverse=True)
    return mismatched


def compute_concentration_risk(rows: list[TBCrossCheckRow], top_n: int = 10) -> ConcentrationRiskSummary:
    """AR concentration risk (client's explicit ask): are receivables
    spread across many customers, or does a handful of them make up most
    of the exposure - a real risk in its own right, independent of
    whether any of them are currently overdue.

    Same `closing_extracted` figure as compute_tb_cross_check_summary's
    `total_debtor_as_per_books`, deliberately - this screen's "Total AR"
    can never quietly disagree with the number already shown on TB
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
    total_ar = sum((r.closing_extracted for r in rows), Decimal("0.00"))
    ranked = sorted(rows, key=lambda r: r.closing_extracted, reverse=True)[:top_n]

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
