"""Pre-MIS Adjustment Register - client's own later ask, after asking how
Pre-MIS Outstanding changes over time: the audited log
record_pre_mis_adjustment already writes (one row per deliberate,
Maker-reviewed resolution of a Pending Review CN/Receipt as belonging to
a pre-MIS-era invoice) had nothing reading it back. A Maker could see
today's Pre-MIS Outstanding figure in Customer Master but not how it got
there - this report is that missing view: each party's original seed
(reconstructed as current balance minus every adjustment on record, since
customer_master.pre_mis_outstanding is itself a running balance, not a
single seed value - see record_pre_mis_adjustment's own docstring), every
adjustment, and the current balance.

Deliberately not a new computation path: pre_mis_outstanding is never
touched by ordinary receipts/CNs matching a tracked invoice - only by
this one sanctioned, logged action - so this report is a second rendering
of the exact same data CustomerMasterRecord.pre_mis_outstanding and
Store.all_pre_mis_adjustments() already hold, never a recomputation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, PreMisAdjustment


@dataclass
class PreMisPartySummary:
    party_id: str
    branch_id: str
    original_seed: Decimal
    total_adjustments: Decimal
    current_balance: Decimal


def compute_pre_mis_register(
    customer_masters: dict[tuple[str, str], CustomerMasterRecord],
    adjustments: list[PreMisAdjustment],
) -> list[PreMisPartySummary]:
    """Only parties that ever carried a Pre-MIS balance (a non-zero
    current balance, or a zero one reached only after being fully
    adjusted away) are returned - most parties onboarded after go-live
    have nothing to show here, and listing every customer with two zero
    columns would just be noise.
    """
    adjustments_by_party: dict[tuple[str, str], Decimal] = {}
    for adj in adjustments:
        key = (adj.party_id, adj.branch_id)
        adjustments_by_party[key] = adjustments_by_party.get(key, Decimal("0.00")) + adj.amount

    rows: list[PreMisPartySummary] = []
    for (party_id, branch_id), customer in customer_masters.items():
        total_adjustments = adjustments_by_party.get((party_id, branch_id), Decimal("0.00"))
        if customer.pre_mis_outstanding == 0 and total_adjustments == 0:
            continue
        rows.append(
            PreMisPartySummary(
                party_id=party_id,
                branch_id=branch_id,
                original_seed=customer.pre_mis_outstanding - total_adjustments,
                total_adjustments=total_adjustments,
                current_balance=customer.pre_mis_outstanding,
            )
        )
    return sorted(rows, key=lambda r: (r.branch_id, r.party_id))
