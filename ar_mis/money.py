"""Decimal-based money handling.

The client picked zero-tolerance reconciliation (Section 4.1, Section 8
item 1): any mismatch fails. That decision only makes sense if the
underlying arithmetic can't manufacture its own mismatches. `float` can:
0.1 + 0.2 != 0.3 in IEEE-754. Every monetary figure in this pipeline is
therefore a `Decimal`, rounded to paise (2 places) at the point it enters
the system, and compared for exact equality after rounding.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


def to_money(value) -> Decimal:
    """Coerce a raw value (str/float/int/Decimal) to a 2-decimal Decimal.

    Tally's XML export represents amounts as plain decimal strings (e.g.
    "-125000.50"); parsing is always via str, not float, to avoid binary
    floating-point round-trip error before the value ever reaches this
    function.
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def is_match(a: Decimal, b: Decimal) -> bool:
    """Zero-tolerance equality: exact match once both sides are in paise."""
    return to_money(a) == to_money(b)


def difference(a: Decimal, b: Decimal) -> Decimal:
    return to_money(a) - to_money(b)
