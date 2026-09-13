"""Ageing Matrix - design doc item 15's customer-level detail report,
sitting alongside the Branch-wise Ageing Schedule (ar_mis.dashboard). Two
views:

- Branch-level summary: this module just reuses
  ar_mis.dashboard.compute_branch_ageing_schedule directly - no need to
  duplicate it. FY-scoping (see compute_ageing_matrix's docstring) is
  applied by the caller pre-filtering sales_dn_rows before calling either
  function, so compute_branch_ageing_schedule's own signature never
  changes.
- Customer-level detail (this module's own compute_ageing_matrix): one
  row per (party_id, branch_id) with the full ageing-bucket breakdown of
  its Open Amount, PLUS an independent cross-check against Tally's own
  ledger closing balance for that party as of the same date
  (weekly_snapshot.closing_extracted - the same figure the zero-tolerance
  reconciliation itself is built on, via
  Store.latest_weekly_snapshot_closing_by_party). A party with no
  weekly_snapshot row on or before as_of has tally_closing_balance=None
  and difference=None - "no Tally figure on record" is not the same as
  "matches exactly" and must never be displayed as 0.00.

No materiality tolerance is applied anywhere in this module - the
client's own Excel used a ₹500 "close enough" threshold, confirmed as an
Excel-only convenience for a human eyeballing a printout, not a rule to
carry into this system. Every difference, however small, is shown as-is;
whether a given difference is worth investigating is a judgment call left
to whoever reads the report, not a decision this code makes for them by
hiding numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import CreditNoteRegisterRow, CustomerMasterRecord, ReceiptJournalRegisterRow, SalesDNRegisterRow
from ar_mis.registers import compute_invoice_position

_AGEING_BUCKET_ORDER = ["Current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "181+"]


@dataclass
class AgeingMatrixRow:
    party_id: str
    branch_id: str
    grouping: str | None
    buckets: dict[str, Decimal]
    total_open: Decimal
    tally_closing_balance: Decimal | None
    difference: Decimal | None


def compute_ageing_matrix(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    customer_masters: dict[tuple[str, str], CustomerMasterRecord],
    tally_closing_by_party: dict[tuple[str, str], Decimal],
    as_of: date,
) -> list[AgeingMatrixRow]:
    """One row per (party_id, branch_id) that has at least one tracked
    Sales/DN invoice. `sales_dn_rows` should already be FY-filtered by the
    caller when a single financial year's view is wanted (filter on
    ar_mis.registers.financial_year_label(row.invoice_date) before
    calling) - this function itself is FY-agnostic, it just totals
    whatever rows it's given, exactly like compute_branch_ageing_schedule.

    `customer_masters` and `tally_closing_by_party` are both keyed
    (party_id, branch_id), matching every other register-level dict in
    this codebase.
    """
    parties = sorted({(row.party_id, row.branch_id) for row in sales_dn_rows})
    rows: list[AgeingMatrixRow] = []

    for party_id, branch_id in parties:
        buckets: dict[str, Decimal] = {b: Decimal("0.00") for b in _AGEING_BUCKET_ORDER}
        for row in sales_dn_rows:
            if row.party_id != party_id or row.branch_id != branch_id:
                continue
            position = compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of)
            buckets[position.ageing_bucket] += position.open_amount
        total_open = sum(buckets.values(), Decimal("0.00"))

        customer = customer_masters.get((party_id, branch_id))
        grouping = customer.grouping.value if customer is not None and customer.grouping is not None else None

        tally_balance = tally_closing_by_party.get((party_id, branch_id))
        difference = (total_open - tally_balance) if tally_balance is not None else None

        rows.append(
            AgeingMatrixRow(
                party_id=party_id,
                branch_id=branch_id,
                grouping=grouping,
                buckets=buckets,
                total_open=total_open,
                tally_closing_balance=tally_balance,
                difference=difference,
            )
        )

    return rows
