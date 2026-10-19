"""The correction mechanism for a backdated entry - client's explicit
ask this session (design doc item 18), simplified after the original
design proved disproportionate to what it was actually solving.

"Incorporating" a finding means: build and persist its original voucher
(persisted whole - see DriftFinding's own docstring) into the one
register its voucher type belongs to, exactly the same builder a normal
weekly extraction uses, dated with its own real historical voucher_date
so ageing/DSO still treat it correctly. Nothing else.

This is deliberately NOT routed through process_branch_data/weekly_
snapshot the way the original version was. Tracing through what actually
reads each of those two tables settles it: Check 1 (TB vs Registers),
the Reconciliation Check gap on AR Snapshot, and every registers-sourced
performance figure (ageing, DSO, Open AR by FY) all read the registers
directly - a missing voucher is fully fixed for every one of them the
moment it lands in the right register, nothing else required. Total AR,
AR Snapshot's own headline figure, is sourced from Tally's own closing_
extracted (ar_mis.dashboard's own docstring) and was never wrong either
way, register or no register. The only thing the old version's weekly_
snapshot write actually touched was that one historical week's own
Opening+Movements=Closing tie-out on the TB Reconciliation Cross-Check
sheet - and retroactively forcing an already-closed week to tie out,
via a fresh Tally closing balance and a `week_ending` the Maker had to
pick carefully around every other already-recorded week for that party,
turned out to be real, confirmed-live complexity (a wrong Opening
Balance when the chosen week landed earlier than weeks already on
record, a date-picking dance to dodge collisions) in service of
something nobody downstream needed fixed: that old week's own record
staying an honest "this didn't tie out at the time, a voucher was found
later" is correct history, not a defect to paper over. Every week after
it keeps rolling forward from its own real, unaffected closing_
extracted regardless.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, VoucherType
from ar_mis.reconciliation import DriftFindingRecord
from ar_mis.registers import (
    RegisterBuildExceptions,
    build_bill_reference_lookup,
    build_credit_note_register_row,
    build_receipt_journal_register_rows,
    build_sales_dn_register_row,
)
from ar_mis.storage import Store


class DriftFindingAlreadyIncorporated(Exception):
    """Refuses a second incorporation attempt for the same finding - the
    voucher is already in the registers."""


class DriftFindingNotAttributable(Exception):
    """The voucher could not be placed into any register - the same
    "never silent" reasons RegisterBuildExceptions would otherwise log
    during a normal extraction (e.g. a Receipt/Journal that, on closer
    inspection, touches no tracked Sundry Debtor leg at all)."""


@dataclass
class DriftFindingCorrection:
    voucher_type: str
    voucher_number: str
    party_ledger_name: str


def incorporate_drift_finding(
    store: Store,
    branch_id: str,
    finding_record: DriftFindingRecord,
    incorporated_by: str,
    incorporated_at: datetime,
) -> DriftFindingCorrection:
    """Builds and persists `finding_record`'s original voucher into the
    one register its voucher type belongs to, then marks the finding
    incorporated. A party this app hasn't tracked before (e.g. a
    genuinely new customer whose first-ever voucher happened to be the
    one that went missing) is auto-created at Pre-MIS Outstanding = 0.00
    first, the same way a normal extraction's own new-party handling
    does - the voucher cannot be attributed to a customer_master-less
    party otherwise.

    Undoing a wrong incorporation needs no dedicated recovery path the
    way the old weekly_snapshot-touching version did: it is a plain
    register row like any other, so ar_mis.register_corrections' own
    Exclude action (Check 1's own Delete control) removes it cleanly.
    """
    if finding_record.incorporated:
        raise DriftFindingAlreadyIncorporated(
            f"Finding {finding_record.id} was already incorporated on {finding_record.incorporated_at}."
        )

    voucher = finding_record.finding.voucher
    party_id = finding_record.finding.party_ledger_name
    if not store.customer_master_exists(party_id, branch_id):
        store.upsert_customer_master(CustomerMasterRecord(party_id, party_id, branch_id, Decimal("0.00")))

    exceptions = RegisterBuildExceptions()
    built = False
    if voucher.voucher_type in (VoucherType.SALES, VoucherType.DEBIT_NOTE):
        customer = store.get_customer_master(party_id, branch_id)
        row = build_sales_dn_register_row(voucher, customer, exceptions)
        if row is not None:
            store.append_sales_dn_row(row)
            built = True
    else:
        tracked_party_names = {
            r["party_id"] for r in store.all_customer_master_records() if r["branch_id"] == branch_id
        }
        lookup = build_bill_reference_lookup(store.all_sales_dn_rows(branch_id))
        if voucher.voucher_type == VoucherType.CREDIT_NOTE:
            row = build_credit_note_register_row(voucher, tracked_party_names, lookup, exceptions)
            if row is not None:
                store.append_credit_note_row(row)
                built = True
        elif voucher.voucher_type in (VoucherType.RECEIPT, VoucherType.JOURNAL):
            rows = build_receipt_journal_register_rows(voucher, tracked_party_names, lookup, exceptions)
            if rows:
                store.append_receipt_journal_rows(rows)
                built = True

    if not built:
        reason = exceptions.unattributable_party[0][3] if exceptions.unattributable_party else "could not be placed into a register"
        raise DriftFindingNotAttributable(f"{voucher.voucher_number} ({party_id}): {reason}")

    store.mark_drift_finding_incorporated(finding_record.id, incorporated_by, incorporated_at)
    return DriftFindingCorrection(
        voucher_type=voucher.voucher_type.value, voucher_number=voucher.voucher_number, party_ledger_name=party_id,
    )
