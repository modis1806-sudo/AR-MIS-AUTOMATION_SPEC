"""Wires extraction, roll-forward, reconciliation and storage together
into the per-branch runner that ar_mis.orchestration drives.

This module is the actual production glue and has NOT been exercised
against a live Tally instance (see README "Known limitation") - every
function it calls (TallyClient, rollforward, reconciliation, storage) is
independently unit-tested, but this composition itself needs validation
on a real branch before the first live run.
"""
from __future__ import annotations

from datetime import date

from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.models import WeeklySnapshotRow
from ar_mis.orchestration import BranchRunOutcome, ExtractionOutcome
from ar_mis.reconciliation import reconcile_all_parties
from ar_mis.rollforward import aggregate_party_movements, resolve_opening_balances
from ar_mis.sign import flip_sign
from ar_mis.storage import Store
from ar_mis.tally_client import TallyClient


def build_branch_runner(store: Store, week_ending: date, from_date: date, to_date: date):
    """Returns a callable suitable for ar_mis.orchestration.run_weekly_cycle.

    For each branch:
      1. Confirm the loaded Tally company matches the branch (2.2 hard gate;
         a mismatch/connection issue raises out of TallyClient and is
         caught by the orchestrator as an extraction failure).
      2. Pull the current week's vouchers for all five types (2.1).
      3. Pull the YTD Sundry Debtors closing balances (source of the
         "closing_extracted" side of the 4.1 comparison, and party list).
      4. Roll forward each party (2.4) from the correct opening balance
         (2.5, via resolve_opening_balances).
      5. Reconcile every party at zero tolerance (4.1).
      6. Append Layer 2 snapshot rows and the voucher log (for future 4.2
         drift isolation) - but ONLY if every party reconciled; a branch
         that fails reconciliation must not have partial/wrong data
         written into the append-only history.
    """

    def run_branch(branch: BranchConfig) -> BranchRunOutcome:
        client = TallyClient(branch=branch)
        client.confirm_current_company()  # raises CompanyMismatchError/TallyConnectionError

        vouchers_by_type = client.fetch_all_voucher_types(from_date, to_date)
        all_vouchers = [v for vs in vouchers_by_type.values() for v in vs]

        fy_start = financial_year_start(to_date)
        closing_extracted = client.fetch_ytd_sundry_debtors(fy_start, to_date)
        party_names = set(closing_extracted)

        openings = resolve_opening_balances(store, party_names, branch.branch_id)
        movements = aggregate_party_movements(all_vouchers, party_names, openings)
        results = reconcile_all_parties(branch.branch_id, movements, closing_extracted)

        failed = [r for r in results if not r.reconciled]
        if failed:
            failed_names = ", ".join(r.party_ledger_name for r in failed)
            return BranchRunOutcome(
                branch_id=branch.branch_id,
                branch_name=branch.branch_name,
                outcome=ExtractionOutcome.RECON_FAIL,
                detail=f"{len(failed)} part(y/ies) did not reconcile: {failed_names}",
                failed_parties=[r.party_ledger_name for r in failed],
            )

        for result in results:
            movement = movements[result.party_ledger_name]
            store.append_weekly_snapshot(
                WeeklySnapshotRow(
                    party_id=result.party_ledger_name,
                    branch_id=branch.branch_id,
                    week_ending=week_ending,
                    opening=movement.opening,
                    sales=movement.sales,
                    credit_notes=movement.credit_notes,
                    debit_notes=movement.debit_notes,
                    receipts=movement.receipts,
                    journals=movement.journals,
                    closing_computed=result.closing_computed,
                    closing_extracted=result.closing_extracted,
                    reconciled=result.reconciled,
                    difference=result.difference,
                )
            )

        for voucher in all_vouchers:
            for entry in voucher.entries:
                if entry.party_ledger_name not in party_names:
                    continue
                store.append_voucher_log_entry(
                    branch_id=branch.branch_id,
                    week_ending=week_ending,
                    party_ledger_name=entry.party_ledger_name,
                    voucher_type=voucher.voucher_type.value,
                    voucher_number=voucher.voucher_number,
                    voucher_date=voucher.voucher_date,
                    flipped_amount=flip_sign(entry.amount_as_extracted),
                )

        return BranchRunOutcome(
            branch_id=branch.branch_id,
            branch_name=branch.branch_name,
            outcome=ExtractionOutcome.PASS,
            detail=f"{len(results)} part(y/ies) reconciled clean",
        )

    return run_branch
