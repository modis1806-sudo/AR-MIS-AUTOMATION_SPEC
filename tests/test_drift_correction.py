from datetime import date, datetime
from decimal import Decimal

import pytest

from ar_mis.drift_correction import (
    DriftFindingAlreadyIncorporated,
    DriftFindingNotAttributable,
    incorporate_drift_finding,
)
from ar_mis.models import CustomerMasterRecord, LedgerEntry, NoteType, SalesDNRegisterRow, Voucher, VoucherType
from ar_mis.reconciliation import DriftFinding
from ar_mis.storage import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def _backdated_sales_voucher(voucher_number="SB/0099-BACKDATED", party="Acme", branch_id="KOL", voucher_date=date(2026, 3, 1), value=Decimal("20000.00")):
    return Voucher(
        voucher_type=VoucherType.SALES, voucher_date=voucher_date, voucher_number=voucher_number,
        branch_id=branch_id, party_ledger_name=party,
        entries=[
            LedgerEntry(party_ledger_name=party, amount_as_extracted=-value, bill_name=voucher_number, bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=value),
        ],
    )


def _backdated_journal_voucher(voucher_number="JV/0099-BACKDATED", party="Acme", branch_id="KOL", voucher_date=date(2026, 3, 1), value=Decimal("5000.00")):
    return Voucher(
        voucher_type=VoucherType.JOURNAL, voucher_date=voucher_date, voucher_number=voucher_number,
        branch_id=branch_id, party_ledger_name=party,
        entries=[
            LedgerEntry(party_ledger_name=party, amount_as_extracted=value, bill_name="SOME-OTHER-BILL", bill_type="Agst Ref"),
        ],
    )


def _finding(voucher, attributed_week=date(2026, 3, 8), amount=Decimal("20000.00")):
    return DriftFinding(
        party_ledger_name=voucher.party_ledger_name, voucher_type=voucher.voucher_type.value,
        voucher_number=voucher.voucher_number, voucher_date=voucher.voucher_date,
        flipped_amount=amount, attributed_week=attributed_week, voucher=voucher,
    )


def test_incorporate_writes_the_register_row(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    result = incorporate_drift_finding(store, "KOL", finding_record, "AR Manager", datetime(2026, 9, 12, 11, 0))

    assert result.voucher_type == "Sales"
    assert result.voucher_number == "SB/0099-BACKDATED"
    assert result.party_ledger_name == "Acme"

    rows = store.all_sales_dn_rows("KOL")
    assert len(rows) == 1
    assert rows[0].voucher_number == "SB/0099-BACKDATED"
    # The invoice's own historical date is preserved for ageing purposes -
    # it must NOT read as if it were raised the day it was incorporated.
    assert rows[0].invoice_date == date(2026, 3, 1)

    updated = store.get_drift_finding(finding_record.id)
    assert updated.incorporated is True
    assert updated.incorporated_by == "AR Manager"


def test_incorporate_auto_creates_a_never_tracked_party(store):
    # A genuinely new customer whose first-ever voucher happened to be the
    # one that went missing - must not require the party to already exist
    # in customer_master before this can run.
    voucher = _backdated_sales_voucher(party="Brand New Customer")
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    incorporate_drift_finding(store, "KOL", finding_record, "AR Manager", datetime(2026, 9, 12, 11, 0))

    assert store.get_customer_master("Brand New Customer", "KOL") is not None
    assert len(store.all_sales_dn_rows("KOL")) == 1


def test_incorporate_handles_a_journal_voucher(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_journal_voucher()
    store.record_drift_findings("KOL", [_finding(voucher, amount=Decimal("5000.00"))], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    result = incorporate_drift_finding(store, "KOL", finding_record, "AR Manager", datetime(2026, 9, 12, 11, 0))

    assert result.voucher_type == "Journal"
    rows = store.all_receipt_journal_rows("KOL")
    assert len(rows) == 1
    assert rows[0].voucher_number == "JV/0099-BACKDATED"


def test_incorporate_refuses_a_second_time(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    incorporate_drift_finding(store, "KOL", finding_record, "AR Manager", datetime(2026, 9, 12, 11, 0))
    already_incorporated = store.get_drift_finding(finding_record.id)

    with pytest.raises(DriftFindingAlreadyIncorporated):
        incorporate_drift_finding(store, "KOL", already_incorporated, "Someone Else", datetime(2026, 9, 19, 11, 0))


def test_incorporate_refuses_a_journal_touching_no_tracked_debtor(store):
    # A Journal whose only leg references a bill no tracked debtor's
    # register has any record of - the same reason build_receipt_journal_
    # register_rows would otherwise produce zero rows during a normal run.
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL, voucher_date=date(2026, 3, 1), voucher_number="JV/ORPHAN",
        branch_id="KOL", party_ledger_name="Nobody Tracked",
        entries=[
            LedgerEntry(party_ledger_name="Freight Account", amount_as_extracted=Decimal("1000.00")),
        ],
    )
    store.record_drift_findings("KOL", [_finding(voucher, amount=Decimal("1000.00"))], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    with pytest.raises(DriftFindingNotAttributable):
        incorporate_drift_finding(store, "KOL", finding_record, "AR Manager", datetime(2026, 9, 12, 11, 0))


def test_delete_drift_correction_week_scoped_to_one_party_and_week(store):
    # delete_drift_correction_week stays a general-purpose, narrowly-scoped
    # cleanup tool independent of how a row got there - set up directly via
    # Store rather than through incorporate_drift_finding, which no longer
    # touches weekly_snapshot at all.
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("Other Party", "Other Party", "KOL", Decimal("0.00")))

    from ar_mis.models import WeeklySnapshotRow

    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="Acme", branch_id="KOL", week_ending=date(2026, 3, 8),
            opening=Decimal("0.00"), sales=Decimal("20000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("20000.00"), closing_extracted=Decimal("99999.00"),
            reconciled=False, difference=Decimal("79999.00"),
        )
    )
    store.append_voucher_log_entry("KOL", date(2026, 3, 8), "Acme", "Sales", "SB/BAD", date(2026, 3, 1), Decimal("20000.00"))
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 3, 1), note_type=NoteType.INVOICE,
            voucher_number="SB/BAD", bill_allocation_reference="SB/BAD", party_id="Acme",
            taxable_value=Decimal("20000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"), igst=Decimal("0.00"),
            invoice_value=Decimal("20000.00"), due_date=date(2026, 4, 1),
        )
    )
    # Other party sharing the exact same week_ending - must survive untouched.
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="Other Party", branch_id="KOL", week_ending=date(2026, 3, 8),
            opening=Decimal("0.00"), sales=Decimal("7000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("7000.00"), closing_extracted=Decimal("7000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )

    store.delete_drift_correction_week("KOL", "Acme", date(2026, 3, 8))

    assert store.all_sales_dn_rows("KOL") == []
    other_party_week = store.weekly_snapshots_for_week(date(2026, 3, 8))
    assert len(other_party_week) == 1 and other_party_week[0]["party_id"] == "Other Party"
