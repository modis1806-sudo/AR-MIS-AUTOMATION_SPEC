"""Branch Sales + Credit Note + Debit Note Total - the client's explicit
ask this session: a simple, direct cross-check that can be eyeballed
against Tally's own P&L page for a chosen period, without leaving this
app. Unlike every other report here (as-of-a-date, balance-sheet-style),
this one is a for-a-period total, matching how a P&L is actually viewed.

Deliberately GROSS (inclusive of GST): CreditNoteRegisterRow carries only
one combined `cn_amount` (design doc item 2 never split it into taxable
value and tax), so there is no tax-exclusive figure this module can
consistently net against Sales and Debit Notes' own taxable_value. If a
client's Tally P&L view shows turnover net of GST, this total will not
match it exactly - that gap is the GST portion, not an error in this
app's figures. Compare against a gross/tax-inclusive Tally view instead,
or treat a mismatch here as "check the GST split," not "the app is
wrong."
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import CreditNoteRegisterRow, NoteType, SalesDNRegisterRow

_ALL_BRANCHES_LABEL = "All Branches"


@dataclass
class BranchTotalRow:
    branch_id: str
    sales_total: Decimal
    debit_note_total: Decimal
    credit_note_total: Decimal
    net_total: Decimal


def compute_branch_sales_cn_dn_totals(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[BranchTotalRow]:
    """One row per branch, plus a final "All Branches" total - both
    `period_start` and `period_end` are inclusive; omit either (or both)
    to total all recorded history with no lower/upper bound. Net Total =
    Sales + Debit Notes - Credit Notes, all on the same gross (GST-
    inclusive) Invoice Value basis - see this module's own docstring for
    why Credit Notes can't be split to a tax-exclusive figure.
    """

    def in_period(d: date) -> bool:
        if period_start is not None and d < period_start:
            return False
        if period_end is not None and d > period_end:
            return False
        return True

    branches = sorted({row.branch_id for row in sales_dn_rows} | {row.branch_id for row in credit_note_rows})
    rows: list[BranchTotalRow] = []
    grand_sales = grand_dn = grand_cn = Decimal("0.00")

    for branch_id in branches:
        sales_total = sum(
            (
                r.invoice_value
                for r in sales_dn_rows
                if r.branch_id == branch_id and r.note_type == NoteType.INVOICE and in_period(r.invoice_date)
            ),
            Decimal("0.00"),
        )
        debit_note_total = sum(
            (
                r.invoice_value
                for r in sales_dn_rows
                if r.branch_id == branch_id and r.note_type == NoteType.DEBIT_NOTE and in_period(r.invoice_date)
            ),
            Decimal("0.00"),
        )
        credit_note_total = sum(
            (r.cn_amount for r in credit_note_rows if r.branch_id == branch_id and in_period(r.cn_date)),
            Decimal("0.00"),
        )
        grand_sales += sales_total
        grand_dn += debit_note_total
        grand_cn += credit_note_total
        rows.append(
            BranchTotalRow(
                branch_id=branch_id,
                sales_total=sales_total,
                debit_note_total=debit_note_total,
                credit_note_total=credit_note_total,
                net_total=sales_total + debit_note_total - credit_note_total,
            )
        )

    if branches:
        rows.append(
            BranchTotalRow(
                branch_id=_ALL_BRANCHES_LABEL,
                sales_total=grand_sales,
                debit_note_total=grand_dn,
                credit_note_total=grand_cn,
                net_total=grand_sales + grand_dn - grand_cn,
            )
        )

    return rows
