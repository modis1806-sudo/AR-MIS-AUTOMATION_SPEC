"""AR Snapshot (KPI dashboard) and the branch-level ageing summary - design
doc item 15's report catalog, built on top of ar_mis.registers' per-
invoice formulas. The branch-level summary (compute_branch_ageing_schedule)
no longer has its own standalone report page - client's later call: it was
an exact duplicate of the Branch Summary table Ageing Matrix already shows
via the same function, so the dedicated page was removed and this is now
reached only through Ageing Matrix. Every figure here is as-of-date
selectable (item 14):
nothing is a stored total, everything is recomputed from the raw
registers for the selected date. Functions take already-fetched register
lists rather than a Store, matching ar_mis.registers' own convention, so
this stays testable independent of Flask/SQLite.

Client's explicit later call on this dashboard's single most important
number: Total AR must be bulletproof against "my books say X, your report
says Y" - so it is sourced from Tally's own closing balance per party
(Store.latest_weekly_snapshot_closing_by_party), not recomputed from the
invoice-level registers. Two parallel universes deliberately coexist on
this one screen:

- **"Position" figures - Tally-sourced, ties exactly to the books**:
  Total AR, Related Party AR, and Reporting Period AR (Total AR minus
  Pre-MIS Outstanding - the slice the registers can actually itemize at
  invoice level, as opposed to the Pre-MIS lump sum Tally's balance also
  carries but this app never itemized). Related Party AR is a disclosure
  slice WITHIN Total AR (via CustomerMasterRecord.grouping), never
  subtracted from it - Total AR is the whole Sundry Debtors control
  account, every party included.
- **"Performance" figures - registers-sourced, unchanged**: Open AR by
  FY, Unapplied Cash/CN, Overdue AR and its ageing buckets, Bad Debt
  Risk, Notional Interest Cost, DSO, Collection Efficiency, PTP Kept
  Rate, and Average Collection Period by branch. None of these can be
  Tally-sourced even in principle - ageing needs an invoice-level due
  date, which a single ledger closing balance doesn't carry - so they
  keep using the registers' own "Workings" total (_total_ar_as_of, the
  old `total_ar` computation before this rename) as their internal AR
  input wherever one is needed, never the new Tally-sourced `total_ar`.

**Reconciliation Check** (renamed from "Rounding Difference", which
undersold what it actually catches) is the one deliberate bridge between
these two universes: Tally-sourced Total AR minus the registers' own
Workings total, as of the same date. Zero confirms the invoice-level
registers account for everything Tally shows; non-zero is a real, worth-
investigating gap (a missed voucher, a misclassification), not noise to
round away. None (not zero) when there's no Tally data on record yet to
compare against.

One figure not explicitly pinned down when the report catalog was
confirmed, documented rather than presented as settled: **Collection
Efficiency's period** - no period length was confirmed for this
dashboard tile specifically (only the formula itself was, per design doc
item 10). Uses the same trailing-90-day window as DSO, since introducing
a second, different window on the same dashboard without a stated reason
would be its own unexplained inconsistency - not a claim that 90 days is
definitely what the client wants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    PartyGrouping,
    ReceiptJournalRegisterRow,
    SalesDNRegisterRow,
)
from ar_mis.registers import (
    AGEING_BUCKET_ORDER,
    compute_collection_efficiency,
    compute_collections_in_window,
    compute_dso,
    compute_invoice_position,
    compute_sales_in_window,
    compute_unapplied_cash_by_party,
    compute_unapplied_cn_by_party,
    financial_year_label,
)

# Design doc item 15, confirmed this session: interest rate assumed flat
# at 10% for now, applied over the relevant overdue period (days past due
# / 365, simple interest - no compounding assumption was stated either,
# so simple interest is the more conservative, defensible default).
NOTIONAL_INTEREST_RATE = Decimal("0.10")
_DSO_WINDOW_DAYS = 90


def _total_ar_as_of(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    pre_mis_outstanding_total: Decimal,
    as_of: date,
) -> Decimal:
    """Total AR = sum of Open Amount across every tracked invoice, as of
    the given date, PLUS the Pre-MIS Outstanding baseline - invoices
    predating go-live were never captured individually (Section 2.5),
    only as that one lump-sum figure per party, so it's a real,
    additional component of Total AR, not already included in the
    invoice-level sum.
    """
    open_ar = sum(
        (
            compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of).open_amount
            for row in sales_dn_rows
        ),
        Decimal("0.00"),
    )
    return open_ar + pre_mis_outstanding_total


@dataclass
class ARSnapshot:
    as_of: date
    total_ar: Decimal
    open_ar_by_fy: dict[str, Decimal]
    pre_mis_outstanding: Decimal
    unapplied_cash: Decimal
    unapplied_cn: Decimal
    related_party_ar: Decimal
    reporting_period_ar: Decimal
    reconciliation_difference: Decimal | None
    overdue_ar: Decimal
    overdue_by_bucket: dict[str, Decimal]
    overdue_pct: Decimal | None
    bad_debt_risk_180_plus: Decimal
    notional_interest_cost: Decimal
    dso: Decimal | None
    collection_efficiency: Decimal | None
    ptp_kept_rate: Decimal | None
    average_collection_period_by_branch: dict[str, Decimal | None] = field(default_factory=dict)


def compute_ar_snapshot(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    customer_masters: dict[tuple[str, str], CustomerMasterRecord],
    ptp_kept_rate: Decimal | None,
    as_of: date,
    tally_closing_by_party: dict[tuple[str, str], Decimal],
) -> ARSnapshot:
    """`customer_masters` is keyed (party_id, branch_id), matching
    SalesDNRegisterRow's own (party_id, branch_id) fields - needed for
    Related Party AR (item 5's Grouping) and the Pre-MIS Outstanding
    total. `ptp_kept_rate` is passed in already computed (via
    registers.compute_ptp_kept_rate, which needs InvoiceFollowUp rows
    this module has no reason to also depend on) rather than recomputed
    here.

    `tally_closing_by_party` (Store.latest_weekly_snapshot_closing_by_party(as_of),
    same keying) is each party's own latest known Tally closing balance on
    or before `as_of` - the books' own number, and now the source of
    Total AR, Related Party AR, and Reporting Period AR (see this
    module's own docstring for why). A party absent from this dict has no
    Tally balance on record yet and simply isn't counted - there's
    nothing to sum, not a zero to assume.
    """
    pre_mis_outstanding_total = sum((c.pre_mis_outstanding for c in customer_masters.values()), Decimal("0.00"))

    # Headline Total AR: the books' own number (client's explicit call),
    # every party included - Related Party is a disclosure slice WITHIN
    # this total below, never subtracted out of it.
    total_ar = sum(tally_closing_by_party.values(), Decimal("0.00"))

    related_party_ar = sum(
        (
            balance
            for (party_id, branch_id), balance in tally_closing_by_party.items()
            if (customer := customer_masters.get((party_id, branch_id))) is not None
            and customer.grouping == PartyGrouping.RELATED_PARTY
        ),
        Decimal("0.00"),
    )

    # Reporting Period AR (renamed from the old, meaningless "Sundry
    # Debtor AR"): how much of the books' Total AR the registers can
    # actually itemize at invoice level since go-live, as opposed to the
    # Pre-MIS lump sum Tally's own balance also carries but this app
    # never captured invoice-by-invoice.
    reporting_period_ar = total_ar - pre_mis_outstanding_total

    # The registers' own "Workings" total - everything below this point
    # (Open AR by FY, Overdue/ageing, Bad Debt Risk, Notional Interest
    # Cost, DSO, Collection Efficiency, Average Collection Period) stays
    # on this number deliberately, per the client's own call: none of it
    # can be Tally-sourced even in principle, since ageing needs an
    # invoice-level due date a single ledger closing balance can't carry.
    # Reconciliation Check (below) is the one deliberate bridge back to
    # the Tally-sourced total_ar above.
    workings_total_ar = _total_ar_as_of(
        sales_dn_rows, credit_note_rows, receipt_journal_rows, pre_mis_outstanding_total, as_of
    )

    positions = [
        (row, compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of)) for row in sales_dn_rows
    ]

    open_ar_by_fy: dict[str, Decimal] = {}
    for row, pos in positions:
        fy = financial_year_label(row.invoice_date)
        open_ar_by_fy[fy] = open_ar_by_fy.get(fy, Decimal("0.00")) + pos.open_amount

    unapplied_cash = sum(compute_unapplied_cash_by_party(receipt_journal_rows).values(), Decimal("0.00"))
    unapplied_cn = sum(compute_unapplied_cn_by_party(credit_note_rows).values(), Decimal("0.00"))

    overdue_ar = Decimal("0.00")
    overdue_by_bucket: dict[str, Decimal] = {}
    bad_debt_risk_180_plus = Decimal("0.00")
    notional_interest_cost = Decimal("0.00")
    for _, pos in positions:
        if not pos.is_overdue:
            continue
        overdue_ar += pos.open_amount
        overdue_by_bucket[pos.ageing_bucket] = overdue_by_bucket.get(pos.ageing_bucket, Decimal("0.00")) + pos.open_amount
        if pos.ageing_bucket == "181+":
            bad_debt_risk_180_plus += pos.open_amount
        notional_interest_cost += pos.open_amount * NOTIONAL_INTEREST_RATE * Decimal(pos.days_past_due) / Decimal("365")

    overdue_pct = (overdue_ar / workings_total_ar * Decimal("100")) if workings_total_ar != 0 else None

    window_start = as_of - timedelta(days=_DSO_WINDOW_DAYS)
    sales_in_window = compute_sales_in_window(sales_dn_rows, window_start, as_of)
    dso = compute_dso(workings_total_ar, sales_in_window)

    opening_ar_for_ce = _total_ar_as_of(
        sales_dn_rows, credit_note_rows, receipt_journal_rows, pre_mis_outstanding_total, window_start
    )
    collected_in_window = compute_collections_in_window(receipt_journal_rows, window_start, as_of)
    collection_efficiency = compute_collection_efficiency(opening_ar_for_ce, sales_in_window, collected_in_window)

    reconciliation_difference = total_ar - workings_total_ar if tally_closing_by_party else None

    branches = {row.branch_id for row in sales_dn_rows}
    average_collection_period_by_branch: dict[str, Decimal | None] = {}
    for branch_id in branches:
        branch_rows = [row for row in sales_dn_rows if row.branch_id == branch_id]
        branch_open_ar = sum(
            (
                compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of).open_amount
                for row in branch_rows
            ),
            Decimal("0.00"),
        )
        branch_sales_in_window = compute_sales_in_window(branch_rows, window_start, as_of)
        average_collection_period_by_branch[branch_id] = compute_dso(branch_open_ar, branch_sales_in_window)

    return ARSnapshot(
        as_of=as_of,
        total_ar=total_ar,
        open_ar_by_fy=open_ar_by_fy,
        pre_mis_outstanding=pre_mis_outstanding_total,
        unapplied_cash=unapplied_cash,
        unapplied_cn=unapplied_cn,
        related_party_ar=related_party_ar,
        reporting_period_ar=reporting_period_ar,
        reconciliation_difference=reconciliation_difference,
        overdue_ar=overdue_ar,
        overdue_by_bucket=overdue_by_bucket,
        overdue_pct=overdue_pct,
        bad_debt_risk_180_plus=bad_debt_risk_180_plus,
        notional_interest_cost=notional_interest_cost,
        dso=dso,
        collection_efficiency=collection_efficiency,
        ptp_kept_rate=ptp_kept_rate,
        average_collection_period_by_branch=average_collection_period_by_branch,
    )


_AGEING_BUCKET_ORDER = AGEING_BUCKET_ORDER


@dataclass
class BranchAgeingRow:
    branch_id: str
    buckets: dict[str, Decimal]
    total: Decimal


def compute_branch_ageing_schedule(
    sales_dn_rows: list[SalesDNRegisterRow],
    credit_note_rows: list[CreditNoteRegisterRow],
    receipt_journal_rows: list[ReceiptJournalRegisterRow],
    as_of: date,
) -> list[BranchAgeingRow]:
    """Design doc item 15: one ageing-bucket row per branch, plus a final
    "All Branches" total row. Every bucket in _AGEING_BUCKET_ORDER is
    always present (0.00 if empty), so every row has the same shape
    regardless of which buckets that branch actually has open invoices
    in - a report column that silently disappears when empty is exactly
    the kind of inconsistency this design is meant to close.
    """
    branches = sorted({row.branch_id for row in sales_dn_rows})
    rows: list[BranchAgeingRow] = []
    grand_total_buckets: dict[str, Decimal] = {b: Decimal("0.00") for b in _AGEING_BUCKET_ORDER}

    for branch_id in branches:
        buckets: dict[str, Decimal] = {b: Decimal("0.00") for b in _AGEING_BUCKET_ORDER}
        for row in sales_dn_rows:
            if row.branch_id != branch_id:
                continue
            position = compute_invoice_position(row, credit_note_rows, receipt_journal_rows, as_of)
            buckets[position.ageing_bucket] += position.open_amount
            grand_total_buckets[position.ageing_bucket] += position.open_amount
        rows.append(BranchAgeingRow(branch_id=branch_id, buckets=buckets, total=sum(buckets.values(), Decimal("0.00"))))

    rows.append(
        BranchAgeingRow(
            branch_id="All Branches", buckets=grand_total_buckets, total=sum(grand_total_buckets.values(), Decimal("0.00"))
        )
    )
    return rows
