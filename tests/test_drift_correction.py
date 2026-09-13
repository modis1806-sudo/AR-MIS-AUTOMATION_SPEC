from datetime import date, datetime
from decimal import Decimal

import pytest

from ar_mis.drift_correction import (
    DriftFindingAlreadyIncorporated,
    DriftFindingCorrectionWeekConflict,
    incorporate_drift_finding,
)
from ar_mis.models import CustomerMasterRecord, LedgerEntry, Voucher, VoucherType
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


def _finding(voucher, attributed_week=date(2026, 3, 8)):
    return DriftFinding(
        party_ledger_name=voucher.party_ledger_name, voucher_type=voucher.voucher_type.value,
        voucher_number=voucher.voucher_number, voucher_date=voucher.voucher_date,
        flipped_amount=Decimal("20000.00"), attributed_week=attributed_week, voucher=voucher,
    )


def test_incorporate_writes_register_row_and_reconciles_clean(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    result = incorporate_drift_finding(
        store, "KOL", "Kolkata", finding_record,
        week_ending=date(2026, 9, 12), party_closing_extracted=Decimal("20000.00"),
        incorporated_by="AR Manager", incorporated_at=datetime(2026, 9, 12, 11, 0),
    )

    assert result.outcome.failed_parties == []
    assert result.week_ending == date(2026, 9, 12)

    rows = store.all_sales_dn_rows("KOL")
    assert len(rows) == 1
    assert rows[0].voucher_number == "SB/0099-BACKDATED"
    # The invoice's own historical date is preserved for ageing purposes -
    # it must NOT read as if it were raised on the correction week.
    assert rows[0].invoice_date == date(2026, 3, 1)

    updated = store.get_drift_finding(finding_record.id)
    assert updated.incorporated is True
    assert updated.incorporated_by == "AR Manager"
    assert updated.incorporated_week_ending == date(2026, 9, 12)


def test_incorporate_still_marks_incorporated_when_it_does_not_fully_reconcile(store):
    # Client's own framing: incorporating only claims the voucher is now
    # in the system, never that the party's books are now clean.
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    result = incorporate_drift_finding(
        store, "KOL", "Kolkata", finding_record,
        week_ending=date(2026, 9, 12), party_closing_extracted=Decimal("999999.00"),  # wrong figure
        incorporated_by="AR Manager", incorporated_at=datetime(2026, 9, 12, 11, 0),
    )

    assert result.outcome.failed_parties == ["Acme"]
    updated = store.get_drift_finding(finding_record.id)
    assert updated.incorporated is True  # still incorporated - the voucher IS in the system now
    # And the mismatch is genuinely visible, not swept away.
    snapshot_rows = store.weekly_snapshots_for_week(date(2026, 9, 12))
    assert snapshot_rows[0]["reconciled"] == 0


def test_incorporate_refuses_a_second_time(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    incorporate_drift_finding(
        store, "KOL", "Kolkata", finding_record,
        week_ending=date(2026, 9, 12), party_closing_extracted=Decimal("20000.00"),
        incorporated_by="AR Manager", incorporated_at=datetime(2026, 9, 12, 11, 0),
    )
    already_incorporated = store.get_drift_finding(finding_record.id)

    with pytest.raises(DriftFindingAlreadyIncorporated):
        incorporate_drift_finding(
            store, "KOL", "Kolkata", already_incorporated,
            week_ending=date(2026, 9, 19), party_closing_extracted=Decimal("20000.00"),
            incorporated_by="Someone Else", incorporated_at=datetime(2026, 9, 19, 11, 0),
        )


def test_incorporate_refuses_a_week_the_party_already_has(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    # Acme already has a real recorded week on 2026-09-12.
    other_voucher = _backdated_sales_voucher(voucher_number="SB/0200", voucher_date=date(2026, 9, 6), value=Decimal("5000.00"))
    from ar_mis.pipeline import process_branch_data
    process_branch_data(store, "KOL", "Kolkata", date(2026, 9, 12), [other_voucher], {"Acme": Decimal("5000.00")})

    voucher = _backdated_sales_voucher()
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    with pytest.raises(DriftFindingCorrectionWeekConflict):
        incorporate_drift_finding(
            store, "KOL", "Kolkata", finding_record,
            week_ending=date(2026, 9, 12), party_closing_extracted=Decimal("25000.00"),
            incorporated_by="AR Manager", incorporated_at=datetime(2026, 9, 12, 11, 0),
        )


def test_incorporate_rolls_forward_from_the_partys_last_known_closing(store):
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("0.00")))
    # Acme's last known-good week: closing 100000.00 on 2026-09-05.
    known_voucher = _backdated_sales_voucher(voucher_number="SB/0001", voucher_date=date(2026, 9, 1), value=Decimal("100000.00"))
    from ar_mis.pipeline import process_branch_data
    process_branch_data(store, "KOL", "Kolkata", date(2026, 9, 5), [known_voucher], {"Acme": Decimal("100000.00")})

    voucher = _backdated_sales_voucher()  # 20000.00, dated 2026-03-01
    store.record_drift_findings("KOL", [_finding(voucher)], datetime(2026, 9, 1, 10, 0))
    finding_record = store.all_drift_findings()[0]

    # 100000 (last known closing) + 20000 (this correction) = 120000.
    result = incorporate_drift_finding(
        store, "KOL", "Kolkata", finding_record,
        week_ending=date(2026, 9, 12), party_closing_extracted=Decimal("120000.00"),
        incorporated_by="AR Manager", incorporated_at=datetime(2026, 9, 12, 11, 0),
    )
    assert result.outcome.failed_parties == []
