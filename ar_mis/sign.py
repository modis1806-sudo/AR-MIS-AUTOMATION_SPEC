"""Section 2.3: the one and only sign transformation in the pipeline.

Tally's XML is signed at source: debit entries negative, credit entries
positive. The firm's reporting convention wants the opposite (Dr
positive, Cr negative). The fix is a single uniform multiply-by-negative-
one applied identically to every extracted amount, regardless of voucher
type. There is deliberately no per-voucher-type branch here - that
per-type sign logic is exactly what produced the original Collection
Efficiency Index sign-convention bug (spec Section 1).
"""
from __future__ import annotations

from decimal import Decimal

from ar_mis.money import to_money


def flip_sign(amount) -> Decimal:
    return to_money(Decimal(str(amount)) * -1)
