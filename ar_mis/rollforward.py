"""Section 2.4: party-level roll-forward.

    Closing Balance = Opening + Sales + Credit Notes + Debit Notes + Receipts + Journals

All figures post-sign-flip, pure addition - no hardcoded per-voucher-type
direction. This is what lets a Journal correctly increase or decrease a
debtor's balance without special-casing (a Journal that credits the party
comes through as a negative post-flip figure and simply adds; one that
debits the party comes through positive and adds too).

A party ledger line is identified by ledger name membership in the set of
Sundry Debtors ledger names (sourced from the Section 4.2 YTD pull), not
by any heuristic on the voucher itself - a voucher's non-party lines
(income, tax, bank, cash ledgers) are never in that set and are correctly
excluded from the movement bridge.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from ar_mis.money import to_money
from ar_mis.sign import flip_sign
from ar_mis.models import Voucher, VoucherType

_MOVEMENT_TYPES = (
    VoucherType.SALES,
    VoucherType.CREDIT_NOTE,
    VoucherType.DEBIT_NOTE,
    VoucherType.RECEIPT,
    VoucherType.JOURNAL,
)


@dataclass
class PartyMovement:
    party_ledger_name: str
    opening: Decimal
    sales: Decimal
    credit_notes: Decimal
    debit_notes: Decimal
    receipts: Decimal
    journals: Decimal

    @property
    def closing_computed(self) -> Decimal:
        return to_money(
            self.opening
            + self.sales
            + self.credit_notes
            + self.debit_notes
            + self.receipts
            + self.journals
        )


_TYPE_TO_FIELD = {
    VoucherType.SALES: "sales",
    VoucherType.CREDIT_NOTE: "credit_notes",
    VoucherType.DEBIT_NOTE: "debit_notes",
    VoucherType.RECEIPT: "receipts",
    VoucherType.JOURNAL: "journals",
}


def resolve_opening_balances(store, party_ledger_names: set[str], branch_id: str) -> dict[str, Decimal]:
    """Opening Balance per Section 2.5: a party's first-ever week opens
    from Layer 1's Pre-MIS Outstanding; every week after opens from the
    prior week's stored closing_computed. Never the other way round, and
    never recomputed from scratch against Pre-MIS Outstanding once a
    prior week exists - that would let the weekly pipeline silently
    re-derive the one figure Section 2.5 requires to stay fixed.
    """
    openings: dict[str, Decimal] = {}
    for party in party_ledger_names:
        latest = store.get_latest_closing(party, branch_id)
        openings[party] = latest if latest is not None else store.get_opening_balance(party, branch_id)
    return openings


def aggregate_party_movements(
    vouchers: list[Voucher],
    party_ledger_names: set[str],
    openings: dict[str, Decimal],
) -> dict[str, PartyMovement]:
    """Sums flipped amounts per party per voucher type across all supplied
    vouchers (already filtered to one week or one YTD range upstream).

    `openings` maps party_ledger_name -> opening balance (Layer 1's
    Pre-MIS Outstanding for a first-ever run, or the prior week's closing
    for a subsequent incremental run - the caller decides which, this
    function just adds it in).
    """
    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {f: Decimal("0.00") for f in _TYPE_TO_FIELD.values()}
    )

    for voucher in vouchers:
        if voucher.voucher_type not in _MOVEMENT_TYPES:
            continue
        field = _TYPE_TO_FIELD[voucher.voucher_type]
        for entry in voucher.entries:
            if entry.party_ledger_name not in party_ledger_names:
                continue
            totals[entry.party_ledger_name][field] += flip_sign(entry.amount_as_extracted)

    movements: dict[str, PartyMovement] = {}
    for party_name in party_ledger_names:
        t = totals[party_name]
        opening = to_money(openings.get(party_name, Decimal("0.00")))
        movements[party_name] = PartyMovement(
            party_ledger_name=party_name,
            opening=opening,
            sales=t["sales"],
            credit_notes=t["credit_notes"],
            debit_notes=t["debit_notes"],
            receipts=t["receipts"],
            journals=t["journals"],
        )
    return movements
