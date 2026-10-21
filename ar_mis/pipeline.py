"""Wires extraction, roll-forward, reconciliation and storage together
into the per-branch runner that ar_mis.orchestration drives.

process_branch_data() is the source-agnostic core: roll-forward,
auto-discovery of new customers, zero-tolerance reconciliation (real, but
never a reason to withhold data - see its own docstring), and storage
writes. It takes already-fetched vouchers and
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
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
)
from ar_mis.orchestration import BranchRunOutcome, ExtractionOutcome
from ar_mis.reconciliation import reconcile_all_parties
from ar_mis.registers import (
    RegisterBuildExceptions,
    build_bill_reference_lookup,
    build_credit_note_register_row,
    build_receipt_journal_register_rows,
    build_sales_dn_register_row,
    find_pending_review_rows_now_resolvable,
)
from ar_mis.rollforward import compute_party_closings_as_of
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
    period_start: date | None = None,
) -> BranchRunOutcome:
    """Roll forward, auto-discover new customers, reconcile at zero
    tolerance, and write the voucher log and invoice-level registers -
    for EVERY party, regardless of whether that party's own
    reconciliation matched.

    This is a deliberate reversal of this module's original all-or-
    nothing gate: the client's explicit instruction is that data must
    never be discarded, even when it doesn't tie out. `BranchRunOutcome.
    failed_parties` still reports which parties didn't reconcile this
    run, for anything downstream that needs to flag it (the webapp's own
    combined Cross-Check banner) - but nothing about a mismatch is ever
    frozen into storage: Store.party_tb_balance only ever remembers
    Tally's own stated figure, never this app's own workings, so a
    mismatch is always re-detected fresh (ar_mis.rollforward.
    compute_party_closings_as_of) whenever anyone looks, never read back
    from what this run happened to compute.

    `extracted_at` is the real wall-clock moment this run happened (for
    the Registers/Reports screens' freshness indicator) - defaults to
    datetime.now() when not supplied, but callers (tests, or a run that
    needs to be attributed to a specific recorded time) may pass their
    own rather than have it resolved internally, matching how every
    other point-in-time value in this codebase is threaded through
    explicitly.

    `closing_extracted` must already be post-sign-flip (Section 2.3),
    matching `all_vouchers`' entries which are still pre-flip (flipped
    internally by aggregate_party_movements_from_registers, from the
    register rows this run builds). Callers own the flip on
    closing_extracted because where it comes from differs: a live
    Tally pull needs it applied, some manual-upload sources might not.

    `period_start` is this run's own real start date - stored alongside
    week_ending (now on extraction_log rather than weekly_snapshot,
    which this version stops writing) for Branch Master's coverage
    summary. None only for a caller that genuinely has no meaningful
    start date of its own.
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

    # Registers are built FIRST - compute_party_closings_as_of below
    # reads them straight back out, so this run's own vouchers must
    # already be persisted for its own reconciliation to see them.
    register_exceptions, sales_dn_rows_built, cn_rows_built, rj_rows_built = _build_and_persist_registers(
        store, branch_id, all_vouchers
    )

    # Client's own catch, live-testing: a CN/Receipt/Journal classified
    # Pending Review at ITS OWN extraction time never got re-evaluated
    # once its own invoice later showed up in a different run (catch-up,
    # non-chronological backfill, an invoice added after the fact) - it
    # stayed stuck even once the register's own live Linked Invoice
    # display already showed it resolving. Run before reconciliation
    # below, not after, so THIS run's own movements/reconciliation
    # already reflect whatever just got promoted, not a one-run-stale
    # picture.
    pending_review_auto_matched = _recheck_pending_review_rows(store, branch_id)

    # Live reconciliation, client's explicit call this session: Tally's
    # own stated closing vs one full recompute from EVERY register row
    # on record for that party through week_ending, done fresh here and
    # again every time anyone views TB Cross-Check later - never a
    # stored breakdown, never a chain. There is no separate "this run's
    # own movement" to isolate any more (that was weekly_snapshot's own
    # job, now gone): a party's whole position as of a date is one
    # number, computed the same way regardless of who's asking or when.
    movements = compute_party_closings_as_of(store, branch_id, party_names, week_ending)
    results = reconcile_all_parties(branch_id, movements, closing_extracted)
    failed = [r for r in results if not r.reconciled]

    store.replace_party_tb_balances_for_branch(branch_id, week_ending, closing_extracted)

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

    resolved_extracted_at = extracted_at or datetime.now()
    store.record_extraction_run(branch_id, week_ending, resolved_extracted_at, period_start)
    if register_exceptions.unattributable_party:
        store.append_register_build_exceptions(
            branch_id, week_ending, register_exceptions.unattributable_party, resolved_extracted_at
        )

    if failed:
        detail = (
            f"{len(results) - len(failed)} of {len(results)} part(y/ies) reconciled; "
            f"{len(failed)} did not (data recorded, not discarded): "
            + ", ".join(r.party_ledger_name for r in failed)
        )
    else:
        detail = f"{len(results)} part(y/ies) reconciled clean"

    return BranchRunOutcome(
        branch_id=branch_id,
        branch_name=branch_name,
        outcome=ExtractionOutcome.PASS,
        detail=detail,
        failed_parties=[r.party_ledger_name for r in failed],
        new_parties=new_parties,
        register_build_exceptions=register_exceptions.unattributable_party,
        pending_review_auto_matched=pending_review_auto_matched,
    )


def _recheck_pending_review_rows(store: Store, branch_id: str) -> list[tuple[str, str, str]]:
    """Storage-touching half of registers.find_pending_review_rows_now_
    resolvable - that function only decides what's now resolvable; this
    reads the branch's current register state, calls it, and writes the
    promotion. CN uses the same Store method a human clicking Resolve
    already uses (resolve_credit_note_classification), with a reason
    that says this was automatic, never a silent change indistinguishable
    from a human's own Resolve. RJ promotes row-by-row via Store.
    resolve_receipt_journal_row_classification instead - the same method
    a human's own Resolve/Revert now uses too (see that method's own
    docstring for why this register's classification is never touched at
    whole-voucher-group scope any more).
    """
    lookup = build_bill_reference_lookup(store.all_sales_dn_rows(branch_id))
    cn_rows = store.all_credit_note_rows(branch_id)
    rj_rows = store.all_receipt_journal_rows(branch_id)
    cn_resolvable, rj_resolvable = find_pending_review_rows_now_resolvable(cn_rows, rj_rows, lookup)

    promoted: list[tuple[str, str, str]] = []
    reason = "Auto-matched: the reference now points to an invoice tracked in the Sales & DN Register"
    for row in cn_resolvable:
        store.resolve_credit_note_classification(
            branch_id, row.voucher_number, row.party_id, RegisterClassification.CURRENT, reason
        )
        promoted.append(("Credit Note", row.voucher_number, row.party_id))
    for row in rj_resolvable:
        store.resolve_receipt_journal_row_classification(row.id, RegisterClassification.CURRENT)
        promoted.append(("Receipt/Journal", row.voucher_number, row.party_id))
    return promoted


def _build_and_persist_registers(
    store: Store, branch_id: str, all_vouchers: list[Voucher]
) -> tuple[
    RegisterBuildExceptions,
    list[SalesDNRegisterRow],
    list[CreditNoteRegisterRow],
    list[ReceiptJournalRegisterRow],
]:
    """Builds and persists the Sales & DN, Credit Note, and Receipt &
    Journal registers (docs/registers_and_reporting_design.md items 1-2)
    from this run's vouchers.

    Also returns the rows actually built THIS run (not the branch's full
    history) - register_build_exceptions needs exactly these to report
    what this run itself couldn't attribute; process_branch_data's own
    reconciliation reads the full history straight back out instead
    (ar_mis.rollforward.compute_party_closings_as_of), now that it has
    no separate "this run's own movement" to isolate.

    Sales & DN rows are built and persisted FIRST, then the bill-
    reference lookup is built from the branch's ENTIRE tracked history
    (store.all_sales_dn_rows(branch_id), not just this run's rows) -
    a Credit Note or Receipt/Journal voucher in this week's data commonly
    references an invoice from a prior week, and item 8's composite-key
    matching only works if the lookup can see it.

    Credit Note and Receipt/Journal vouchers are also checked against
    `tracked_party_names` (this branch's real Sundry Debtors, per
    customer_master) before being attributed to anyone - a Journal in
    particular can touch a creditor, a loan account, anything, and
    Tally's own top-level PARTYLEDGERNAME tag on that voucher is not
    reliable evidence of which leg is the actual customer (see
    registers.build_receipt_journal_register_rows's own docstring for the
    live-reproduced case this fixes).
    """
    exceptions = RegisterBuildExceptions()
    sales_dn_rows_built: list[SalesDNRegisterRow] = []

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
            exceptions.unattributable_party.append(
                (voucher.voucher_number, voucher.party_ledger_name or "", voucher.voucher_type.value, reason)
            )
            continue
        row = build_sales_dn_register_row(voucher, customer, exceptions)
        if row is not None:
            store.append_sales_dn_row(row)
            sales_dn_rows_built.append(row)

    lookup = build_bill_reference_lookup(store.all_sales_dn_rows(branch_id))
    tracked_party_names = {r["party_id"] for r in store.all_customer_master_records() if r["branch_id"] == branch_id}

    cn_rows_built: list[CreditNoteRegisterRow] = []
    rj_rows_built: list[ReceiptJournalRegisterRow] = []
    for voucher in all_vouchers:
        if voucher.voucher_type == VoucherType.CREDIT_NOTE:
            row = build_credit_note_register_row(voucher, tracked_party_names, lookup, exceptions)
            if row is not None:
                store.append_credit_note_row(row)
                cn_rows_built.append(row)
        elif voucher.voucher_type in (VoucherType.RECEIPT, VoucherType.JOURNAL):
            rows = build_receipt_journal_register_rows(voucher, tracked_party_names, lookup, exceptions)
            store.append_receipt_journal_rows(rows)
            rj_rows_built.extend(rows)

    return exceptions, sales_dn_rows_built, cn_rows_built, rj_rows_built


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
        aliases = store.voucher_type_aliases(branch.branch_id)
        client = TallyClient(branch=branch, voucher_type_aliases=aliases)
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

        return process_branch_data(
            store, branch.branch_id, branch.branch_name, week_ending, all_vouchers, closing_extracted,
            period_start=from_date,
        )

    return run_branch
