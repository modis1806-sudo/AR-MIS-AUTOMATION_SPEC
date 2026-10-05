"""Manual XML upload fallback for when the live Tally gateway isn't
reachable - built after real testing against the client's Tally showed
its background gateway service (tallygatewayserver.exe) can go down for
extended periods independent of anything in this codebase.

Seven files per branch per reporting period, matching how an operator
would actually export them from Tally's own UI (one voucher type per
report view):

  1-5. Sales, Credit Note, Debit Note, Receipt, Journal vouchers for the
       reporting week (From Date -> To Date)
  6. Trial Balance / Sundry Debtors closing balance as-on the To Date
  7. Voucher-wise detail from day 1 of the financial year through the
     To Date - Check 1's own YTD cross-check (docs/registers_and_
     reporting_design.md item 56), satisfied from an uploaded file
     instead of a live pull. Optional: if not supplied, the weekly
     figures are still processed, just without this cross-check this
     run. Exported from Tally as "Sundry Debtors -> Ctrl+H -> Voucher
     view" (the one shape a real operator's Tally UI can actually
     produce here - see ar_mis.parsers.parse_manual_ytd_voucher_report's
     own docstring) - a Display Report shape that carries no party field
     at all, so this comparison is scoped one dimension coarser than the
     live pull's own Check 1 (branch + voucher type + voucher number,
     never party - see ar_mis.ytd_debtor_cross_check.compute_manual_ytd_
     vs_register_comparison's own docstring for why that's not a gap).

These are ordinary Tally-exported XML files - the same shape
ar_mis.parsers already handles, since parsing never cared whether XML
arrived over HTTP or as a file. Vouchers are categorized by content
(parsers.categorize_voucher_type), not by which upload slot they were
put in, so a file placed in the wrong slot still gets classified
correctly.

Conflict rule: refuses outright if this branch/week already has any
recorded data, from live extraction or an earlier manual upload - first
one in wins, the same append-only principle as everywhere else in this
pipeline. A genuine correction is a deliberate, separate action
(Store.record_pre_mis_adjustment for Pre-MIS Outstanding; there is no
equivalent "just overwrite it" path for weekly figures by design).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ar_mis.models import Voucher
from ar_mis.orchestration import BranchRunOutcome
from ar_mis.parsers import parse_ledger_closing_balances, parse_manual_ytd_voucher_report, parse_voucher_collection
from ar_mis.pipeline import process_branch_data
from ar_mis.sign import flip_sign
from ar_mis.storage import Store
from ar_mis.ytd_debtor_cross_check import ManualYtdVsRegisterRow, compute_manual_ytd_vs_register_comparison

WEEKLY_VOUCHER_SLOTS = ["Sales", "Credit Note", "Debit Note", "Receipt", "Journal"]


class ManualUploadRefused(Exception):
    """This branch/week already has recorded data - refuse, never
    silently overwrite (the conflict rule)."""


@dataclass
class ManualUploadResult:
    outcome: BranchRunOutcome
    ytd_vs_register_rows: list[ManualYtdVsRegisterRow] = field(default_factory=list)


def process_manual_upload(
    store: Store,
    branch_id: str,
    branch_name: str,
    to_date: date,
    weekly_voucher_xml: dict[str, str],
    trial_balance_xml: str,
    ytd_voucher_xml: str = "",
    from_date: date | None = None,
) -> ManualUploadResult:
    """`weekly_voucher_xml` maps slot name (any/all of WEEKLY_VOUCHER_SLOTS)
    to raw XML text - a blank or missing entry is simply skipped, since a
    branch legitimately might have zero vouchers of some type in a given
    week. `to_date` doubles as the week_ending this data is recorded
    under, matching how the live path uses its reporting date. `from_date`
    is that same run's own start date (the operator's own From Date field)
    - threaded through as period_start so Store.delete_branch_week can
    later recover exactly which rows this upload covers, same reason the
    live path now needs it (see ar_mis.config's own docstring).
    """
    if store.has_weekly_snapshot_for_branch_week(branch_id, to_date):
        raise ManualUploadRefused(
            f"Branch '{branch_name}' already has recorded data for the week ending "
            f"{to_date.isoformat()} (from live extraction or an earlier manual upload). "
            "Refusing to overwrite - a genuine correction needs a deliberate, separate action."
        )

    all_vouchers: list[Voucher] = []
    for raw_xml in weekly_voucher_xml.values():
        if raw_xml and raw_xml.strip():
            all_vouchers.extend(parse_voucher_collection(raw_xml, branch_id))

    closing_extracted = {
        name: flip_sign(balance) for name, balance in parse_ledger_closing_balances(trial_balance_xml).items()
    }

    outcome = process_branch_data(
        store, branch_id, branch_name, to_date, all_vouchers, closing_extracted, period_start=from_date
    )

    # data is now always written (see process_branch_data's own docstring
    # for this session's never-discard reversal), so this only needs to
    # gate on whether a YTD file was actually supplied. Never persisted
    # anywhere (unlike the live pull's own ytd_debtor_voucher table) -
    # there is no party dimension to key a stored row on, so this is
    # shown once on this upload's own result page, not added to any
    # permanent, filterable report.
    ytd_vs_register_rows: list[ManualYtdVsRegisterRow] = []
    if ytd_voucher_xml.strip():
        manual_ytd_rows = parse_manual_ytd_voucher_report(ytd_voucher_xml, branch_id)
        ytd_vs_register_rows = compute_manual_ytd_vs_register_comparison(
            manual_ytd_rows,
            store.all_sales_dn_rows(branch_id),
            store.all_credit_note_rows(branch_id),
            store.all_receipt_journal_rows(branch_id),
        )

    return ManualUploadResult(outcome=outcome, ytd_vs_register_rows=ytd_vs_register_rows)
