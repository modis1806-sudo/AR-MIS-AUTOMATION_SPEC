"""Wires extraction, roll-forward, reconciliation and storage together
into the per-branch runner that ar_mis.orchestration drives.

process_branch_data() is the source-agnostic core: roll-forward,
auto-discovery of new customers, zero-tolerance reconciliation, and the
conditional storage writes. It takes already-fetched vouchers and
closing balances - it doesn't know or care whether they came from a
live Tally connection (build_branch_runner, below) or a manually
uploaded XML file (ar_mis.manual_upload). That's deliberate: the
parsing layer already didn't care where XML came from, and this is the
same principle one level up - the business logic shouldn't care either.

This module (the live-extraction wiring specifically) has NOT been
exercised against a live Tally instance (see README "Known limitation")
- every function it calls is independently unit-tested, but this
composition itself needs validation on a real branch before the first
live run.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.models import CustomerMasterRecord, Voucher, VoucherType, WeeklySnapshotRow
from ar_mis.orchestration import BranchRunOutcome, ExtractionOutcome
from ar_mis.reconciliation import reconcile_all_parties
from ar_mis.registers import (
    RegisterBuildExceptions,
    build_bill_reference_lookup,
    build_credit_note_register_row,
    build_receipt_journal_register_rows,
    build_sales_dn_register_row,
)
from ar_mis.rollforward import aggregate_party_movements, resolve_opening_balances
from ar_mis.sign import flip_sign
from ar_mis.storage import Store
from ar_mis.tally_client import TallyClient


def process_branch_data(
    store: Store,
    branch_id: str,
    branch_name: str,
    week_ending: date,
    all_vouchers: list[Voucher],
    closing_extracted: dict[str, Decimal],
    extracted_at: datetime | None = None,
) -> BranchRunOutcome:
    """Roll forward, auto-discover new customers, reconcile at zero
    tolerance, and write Layer 2 + the voucher log - but ONLY if every
    party reconciled; a branch that fails reconciliation must not have
    partial/wrong data written into the append-only history.

    `extracted_at` is the real wall-clock moment this run happened (for
    the Registers/Reports screens' freshness indicator) - defaults to
    datetime.now() when not supplied, but callers (tests, or a run that
    needs to be attributed to a specific recorded time) may pass their
    own rather than have it resolved internally, matching how every
    other point-in-time value in this codebase is threaded through
    explicitly.

    `closing_extracted` must already be post-sign-flip (Section 2.3),
    matching `all_vouchers`' entries which are still pre-flip (flipped
    internally by aggregate_party_movements). Callers own the flip on
    closing_extracted because where it comes from differs: a live
    Tally pull needs it applied, some manual-upload sources might not.
    """
    party_names = set(closing_extracted)

    # A party in the Sundry Debtors YTD pull with no customer_master
    # record is a genuinely new customer (the client provides a
    # comprehensive opening-balance seed covering every customer as
    # of go-live, so anything missing from it didn't exist then).
    # Auto-create at Pre-MIS Outstanding = 0, the only correct value
    # for a party with no legacy balance - but still surface it in
    # the report's Exceptions section, since a new customer is worth
    # a human noticing even when the number itself isn't in doubt.
    new_parties = [party for party in party_names if not store.customer_master_exists(party, branch_id)]
    for party in new_parties:
        store.upsert_customer_master(CustomerMasterRecord(party, party, branch_id, Decimal("0.00")))

    openings = resolve_opening_balances(store, party_names, branch_id)
    movements = aggregate_party_movements(all_vouchers, party_names, openings)
    results = reconcile_all_parties(branch_id, movements, closing_extracted)

    failed = [r for r in results if not r.reconciled]
    if failed:
        failed_names = ", ".join(r.party_ledger_name for r in failed)
        return BranchRunOutcome(
            branch_id=branch_id,
            branch_name=branch_name,
            outcome=ExtractionOutcome.RECON_FAIL,
            detail=f"{len(failed)} part(y/ies) did not reconcile: {failed_names}",
            failed_parties=[r.party_ledger_name for r in failed],
            new_parties=new_parties,
        )

    for result in results:
        movement = movements[result.party_ledger_name]
        store.append_weekly_snapshot(
            WeeklySnapshotRow(
                party_id=result.party_ledger_name,
                branch_id=branch_id,
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
                branch_id=branch_id,
                week_ending=week_ending,
                party_ledger_name=entry.party_ledger_name,
                voucher_type=voucher.voucher_type.value,
                voucher_number=voucher.voucher_number,
                voucher_date=voucher.voucher_date,
                flipped_amount=flip_sign(entry.amount_as_extracted),
            )

    register_exceptions = _build_and_persist_registers(store, branch_id, all_vouchers)
    store.record_extraction_run(branch_id, week_ending, extracted_at or datetime.now())

    return BranchRunOutcome(
        branch_id=branch_id,
        branch_name=branch_name,
        outcome=ExtractionOutcome.PASS,
        detail=f"{len(results)} part(y/ies) reconciled clean",
        new_parties=new_parties,
        register_build_exceptions=register_exceptions.unattributable_party,
    )


def _build_and_persist_registers(
    store: Store, branch_id: str, all_vouchers: list[Voucher]
) -> RegisterBuildExceptions:
    """Builds and persists the Sales & DN, Credit Note, and Receipt &
    Journal registers (docs/registers_and_reporting_design.md items 1-2)
    from this run's vouchers - only ever called after reconciliation has
    already cleared (see process_branch_data), matching the same
    "never write partial/wrong data" principle already enforced for
    weekly_snapshot and voucher_log.

    Sales & DN rows are built and persisted FIRST, then the bill-
    reference lookup is built from the branch's ENTIRE tracked history
    (store.all_sales_dn_rows(branch_id), not just this run's rows) -
    a Credit Note or Receipt/Journal voucher in this week's data commonly
    references an invoice from a prior week, and item 8's composite-key
    matching only works if the lookup can see it.
    """
    exceptions = RegisterBuildExceptions()

    for voucher in all_vouchers:
        if voucher.voucher_type not in (VoucherType.SALES, VoucherType.DEBIT_NOTE):
            continue
        customer = store.get_customer_master(voucher.party_ledger_name, branch_id) if voucher.party_ledger_name else None
        if customer is None:
            reason = (
                "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer"
                if not voucher.party_ledger_name
                else f"No customer_master record for '{voucher.party_ledger_name}'"
            )
            exceptions.unattributable_party.append((voucher.voucher_number, reason))
            continue
        row = build_sales_dn_register_row(voucher, customer, exceptions)
        if row is not None:
            store.append_sales_dn_row(row)

    lookup = build_bill_reference_lookup(store.all_sales_dn_rows(branch_id))

    for voucher in all_vouchers:
        if voucher.voucher_type == VoucherType.CREDIT_NOTE:
            row = build_credit_note_register_row(voucher, lookup, exceptions)
            if row is not None:
                store.append_credit_note_row(row)
        elif voucher.voucher_type in (VoucherType.RECEIPT, VoucherType.JOURNAL):
            store.append_receipt_journal_rows(build_receipt_journal_register_rows(voucher, lookup, exceptions))

    return exceptions


def build_branch_runner(store: Store, week_ending: date, from_date: date, to_date: date):
    """Returns a callable suitable for ar_mis.orchestration.run_weekly_cycle.

    For each branch:
      1. Confirm the loaded Tally company matches the branch (2.2 hard gate;
         a mismatch/connection issue raises out of TallyClient and is
         caught by the orchestrator as an extraction failure).
      2. Pull the current week's vouchers for all five types (2.1) and the
         YTD Sundry Debtors closing balances (source of the
         "closing_extracted" side of the 4.1 comparison, and party list).
      3. Hand off to process_branch_data() for the source-agnostic part.
    """

    def run_branch(branch: BranchConfig) -> BranchRunOutcome:
        client = TallyClient(branch=branch)
        client.confirm_current_company()  # raises CompanyMismatchError/TallyConnectionError

        vouchers_by_type = client.fetch_all_voucher_types(from_date, to_date)
        all_vouchers = [v for vs in vouchers_by_type.values() for v in vs]

        fy_start = financial_year_start(to_date)
        # fetch_ytd_sundry_debtors returns raw Tally-signed balances
        # (Section 2.3: Dr negative at source) - flip here so the
        # comparison in reconcile_all_parties is post-flip on both sides,
        # matching movements[...].closing_computed.
        closing_extracted = {
            name: flip_sign(balance) for name, balance in client.fetch_ytd_sundry_debtors(fy_start, to_date).items()
        }

        return process_branch_data(store, branch.branch_id, branch.branch_name, week_ending, all_vouchers, closing_extracted)

    return run_branch
