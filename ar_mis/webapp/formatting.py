"""Indian-context number formatting for the webapp.

Every amount in this app is Indian Rupees, and a plain "%.2f" (or a raw
Decimal) groups digits the Western way every 3 digits throughout
(12,345,678.00) - not how any Indian reader of a financial report
expects to see a number. The Indian/lakh-crore convention groups the
last 3 digits together, then every 2 digits after that
(1,23,45,678.00), so a reader can tell a lakh from a crore at a glance
instead of counting digits. Every amount rendered in a template should
go through format_inr (registered as the `inr` Jinja filter in
ar_mis.webapp.app.create_app), not a bare "%.2f" or an unformatted
Decimal.
"""
from __future__ import annotations

from decimal import Decimal


def format_inr(value, decimals: int = 2) -> str:
    """Formats `value` with Indian digit grouping. `None` renders as an
    empty string, matching how templates already treat a missing amount
    - this must be a safe drop-in for a bare `{{ value }}`.
    """
    if value is None:
        return ""
    amount = Decimal(str(value))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)

    if decimals:
        amount = amount.quantize(Decimal(1).scaleb(-decimals))
        int_part, _, frac_part = f"{amount:f}".partition(".")
        frac_part = frac_part.ljust(decimals, "0")
    else:
        amount = amount.quantize(Decimal(1))
        int_part, frac_part = f"{amount:f}", ""

    if len(int_part) <= 3:
        grouped = int_part
    else:
        last_three, rest = int_part[-3:], int_part[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ",".join(groups) + "," + last_three

    return f"{sign}{grouped}.{frac_part}" if decimals else f"{sign}{grouped}"
