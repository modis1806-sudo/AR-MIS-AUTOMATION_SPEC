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
     To Date - Section 4.2's YTD full-pull cross-check, satisfied from
     an uploaded file instead of a live pull. Optional: if not supplied,
     the weekly figures are still processed, just without a drift check
     this run.

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
from datetime import date, datetime

from ar_mis.models import Voucher
from ar_mis.orchestration import BranchRunOutcome
from ar_mis.parsers import parse_ledger_closing_balances, parse_voucher_collection
from ar_mis.pipeline import process_branch_data
from ar_mis.reconciliation import DriftFinding, isolate_drift
from ar_mis.sign import flip_sign
from ar_mis.storage import Store

WEEKLY_VOUCHER_SLOTS = ["Sales", "Credit Note", "Debit Note", "Receipt", "Journal"]


class ManualUploadRefused(Exception):
    """This branch/week already has recorded data - refuse, never
    silently overwrite (the conflict rule)."""


@dataclass
class ManualUploadResult:
    outcome: BranchRunOutcome
    drift_findings: list[DriftFinding] = field(default_factory=list)


def process_manual_upload(
    store: Store,
    branch_id: str,
    branch_name: str,
    to_date: date,
    weekly_voucher_xml: dict[str, str],
    trial_balance_xml: str,
    ytd_voucher_xml: str = "",
) -> ManualUploadResult:
    """`weekly_voucher_xml` maps slot name (any/all of WEEKLY_VOUCHER_SLOTS)
    to raw XML text - a blank or missing entry is simply skipped, since a
    branch legitimately might have zero vouchers of some type in a given
    week. `to_date` doubles as the week_ending this data is recorded
    under, matching how the live path uses its reporting date.
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

    outcome = process_branch_data(store, branch_id, branch_name, to_date, all_vouchers, closing_extracted)

    # data is now always written (see process_branch_data's own docstring
    # for this session's never-discard reversal), so this only needs to
    # gate on whether a YTD file was actually supplied.
    drift_findings: list[DriftFinding] = []
    if ytd_voucher_xml.strip():
        ytd_vouchers = parse_voucher_collection(ytd_voucher_xml, branch_id)
        party_names = set(closing_extracted)
        logged_keys = {party: store.logged_voucher_keys(branch_id, party) for party in party_names}
        week_boundaries = store.all_week_endings()
        drift_findings = isolate_drift(branch_id, ytd_vouchers, party_names, logged_keys, week_boundaries)
        store.record_drift_findings(branch_id, drift_findings, datetime.now())

    return ManualUploadResult(outcome=outcome, drift_findings=drift_findings)
