import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    FYRolloverSnapshot,
    InvoiceFollowUp,
    LedgerEntry,
    NoteType,
    PartyGrouping,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
    WeeklyMovementRow,
    WeeklySnapshotRow,
)
from ar_mis.reconciliation import DriftFinding
from ar_mis.storage import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_mark_reconciled_records_who_and_when(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme", "KOL", Decimal("0.00")))
    records = store.all_customer_master_records()
    assert records[0]["last_reconciled_on"] is None
    assert records[0]["last_reconciled_by"] is None

    store.mark_reconciled("P1", "KOL", date(2026, 1, 12), "Priya")
    records = store.all_customer_master_records()
    assert records[0]["last_reconciled_on"] == "2026-01-12"
    assert records[0]["last_reconciled_by"] == "Priya"


def test_mark_reconciled_does_not_touch_pre_mis_outstanding(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme", "KOL", Decimal("50000.00")))
    store.mark_reconciled("P1", "KOL", date(2026, 1, 12), "Priya")
    assert store.get_opening_balance("P1", "KOL") == Decimal("50000.00")


def test_mark_reconciled_raises_for_unknown_party(store):
    with pytest.raises(ValueError, match="No customer_master record"):
        store.mark_reconciled("GHOST", "KOL", date(2026, 1, 12), "Priya")


def test_store_creates_missing_parent_directory(tmp_path):
    # A fresh checkout never ships an empty `data/` dir (git can't track
    # one), so Store must create it rather than fail with sqlite3's
    # "unable to open database file" - hit for real on a first-time
    # Windows install where data/ didn't exist yet.
    db_path = tmp_path / "data" / "nested" / "ar_mis.db"
    assert not db_path.parent.exists()
    s = Store(str(db_path))
    assert db_path.exists()
    s.close()


def test_customer_master_upsert_does_not_touch_pre_mis_outstanding(store):
    store.upsert_customer_master(
        CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("100000.00"))
    )
    # Re-sync with a name change and a (deliberately wrong) attempted balance -
    # only name should move via upsert.
    store.upsert_customer_master(
        CustomerMasterRecord("P1", "Acme Corp Pvt Ltd", "KOL", Decimal("999999.00"))
    )
    assert store.get_opening_balance("P1", "KOL") == Decimal("100000.00")


def test_get_customer_master_round_trips_grouping_and_credit_period(store):
    assert store.get_customer_master("P1", "KOL") is None
    store.upsert_customer_master(
        CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("100000.00"),
                              grouping=PartyGrouping.SUNDRY_DEBTOR, credit_period_days=45)
    )
    record = store.get_customer_master("P1", "KOL")
    assert record.grouping == PartyGrouping.SUNDRY_DEBTOR
    assert record.credit_period_days == 45


def test_new_customer_master_defaults_credit_period_to_30_and_ungrouped(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    record = store.get_customer_master("P1", "KOL")
    assert record.credit_period_days == 30
    assert record.grouping is None


def test_all_customer_masters_returns_fully_typed_records(store):
    store.upsert_customer_master(
        CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("500.00"),
                              grouping=PartyGrouping.RELATED_PARTY, credit_period_days=45)
    )
    store.upsert_customer_master(CustomerMasterRecord("P2", "Beta Ltd", "KOL", Decimal("0.00")))
    records = {r.party_id: r for r in store.all_customer_masters()}
    assert records["P1"].pre_mis_outstanding == Decimal("500.00")
    assert records["P1"].grouping == PartyGrouping.RELATED_PARTY
    assert records["P1"].credit_period_days == 45
    assert records["P2"].grouping is None


def test_latest_weekly_snapshot_closing_total_is_none_when_empty(store):
    assert store.latest_weekly_snapshot_closing_total() is None


def test_latest_weekly_snapshot_closing_total_sums_the_most_recent_week(store):
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("1000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 12),
            opening=Decimal("1000.00"), sales=Decimal("500.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1500.00"), closing_extracted=Decimal("1500.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    # Only the most recent week's total, not both weeks summed together.
    assert store.latest_weekly_snapshot_closing_total() == Decimal("1500.00")


def test_latest_weekly_snapshot_closing_by_party_is_empty_when_no_snapshots(store):
    assert store.latest_weekly_snapshot_closing_by_party(date(2026, 1, 12)) == {}


def test_latest_weekly_snapshot_closing_by_party_picks_latest_week_per_party(store):
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("1000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 12),
            opening=Decimal("1000.00"), sales=Decimal("500.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1500.00"), closing_extracted=Decimal("1490.00"),
            reconciled=True, difference=Decimal("10.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P2", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("2000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("2000.00"), closing_extracted=Decimal("2000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    result = store.latest_weekly_snapshot_closing_by_party(date(2026, 1, 12))
    assert result == {("P1", "KOL"): Decimal("1490.00"), ("P2", "KOL"): Decimal("2000.00")}


def test_get_latest_closing_is_none_before_any_week(store):
    assert store.get_latest_closing("P1", "KOL") is None


def test_get_latest_closing_uses_closing_extracted_not_closing_computed(store):
    # Client's explicit decision this session: next week's opening rolls
    # forward from Tally's own stated truth (closing_extracted), not this
    # app's own workings (closing_computed) - so an unresolved
    # reconciliation difference doesn't silently compound week after
    # week. This party's week didn't reconcile (computed 1500 vs
    # extracted 1490), so the next opening must be 1490, not 1500.
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1500.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1500.00"), closing_extracted=Decimal("1490.00"),
            reconciled=False, difference=Decimal("10.00"),
        )
    )
    assert store.get_latest_closing("P1", "KOL") == Decimal("1490.00")


def test_get_latest_closing_uses_the_most_recent_week(store):
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("1000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 12),
            opening=Decimal("1000.00"), sales=Decimal("500.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1500.00"), closing_extracted=Decimal("1500.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    assert store.get_latest_closing("P1", "KOL") == Decimal("1500.00")


def test_all_weekly_snapshot_rows_is_empty_before_any_week(store):
    assert store.all_weekly_snapshot_rows() == []


def test_all_weekly_snapshot_rows_returns_full_history_typed(store):
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P2", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("2000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("2000.00"), closing_extracted=Decimal("2000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("990.00"),
            reconciled=False, difference=Decimal("10.00"),
        )
    )
    rows = store.all_weekly_snapshot_rows()
    assert len(rows) == 2
    # Ordered by party_id first, so P1 (the mismatched one) comes first.
    assert rows[0].party_id == "P1"
    assert rows[0].reconciled is False
    assert rows[0].difference == Decimal("10.00")
    assert rows[0].closing_extracted == Decimal("990.00")
    assert rows[1].party_id == "P2"
    assert rows[1].reconciled is True


def test_latest_weekly_snapshot_closing_by_party_respects_as_of_cutoff(store):
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("1000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="P1", branch_id="KOL", week_ending=date(2026, 1, 12),
            opening=Decimal("1000.00"), sales=Decimal("500.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1500.00"), closing_extracted=Decimal("1500.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    # Only the week on-or-before as_of counts, not the later one.
    result = store.latest_weekly_snapshot_closing_by_party(date(2026, 1, 5))
    assert result == {("P1", "KOL"): Decimal("1000.00")}


def test_upsert_customer_master_does_not_touch_credit_period_or_grouping_for_existing_party(store):
    store.upsert_customer_master(
        CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00"),
                              grouping=PartyGrouping.RELATED_PARTY, credit_period_days=60)
    )
    # Re-sync (e.g. a name change) must not reset these back to defaults -
    # same protection pre_mis_outstanding already has.
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp Renamed", "KOL", Decimal("0.00")))
    record = store.get_customer_master("P1", "KOL")
    assert record.credit_period_days == 60
    assert record.grouping == PartyGrouping.RELATED_PARTY


def test_set_credit_period_days(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    store.set_credit_period_days("P1", "KOL", 45)
    assert store.get_customer_master("P1", "KOL").credit_period_days == 45


def test_set_credit_period_days_raises_for_unknown_party(store):
    with pytest.raises(ValueError, match="No customer_master record"):
        store.set_credit_period_days("GHOST", "KOL", 45)


def test_new_customer_master_defaults_to_one_crore_credit_limit(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    assert store.get_customer_master("P1", "KOL").credit_limit == Decimal("10000000.00")


def test_set_credit_limit(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    store.set_credit_limit("P1", "KOL", Decimal("2500000.00"))
    assert store.get_customer_master("P1", "KOL").credit_limit == Decimal("2500000.00")


def test_set_credit_limit_raises_for_unknown_party(store):
    with pytest.raises(ValueError, match="No customer_master record"):
        store.set_credit_limit("GHOST", "KOL", Decimal("500000.00"))


def test_upsert_customer_master_preserves_credit_limit_on_repeat_calls(store):
    # Master-data sync (a name refresh) must never silently reset an
    # already-edited credit limit back to the record's own default.
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    store.set_credit_limit("P1", "KOL", Decimal("2500000.00"))
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp Renamed", "KOL", Decimal("0.00")))
    assert store.get_customer_master("P1", "KOL").credit_limit == Decimal("2500000.00")


def test_set_party_grouping(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme Corp", "KOL", Decimal("0.00")))
    store.set_party_grouping("P1", "KOL", PartyGrouping.RELATED_PARTY)
    assert store.get_customer_master("P1", "KOL").grouping == PartyGrouping.RELATED_PARTY


def test_pre_mis_adjustment_is_the_only_way_to_move_baseline(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme", "KOL", Decimal("100000.00")))
    store.record_pre_mis_adjustment(
        PreMisAdjustment("P1", "KOL", Decimal("-25000.00"), "Legacy balance write-off", "cfo-office", date(2026, 1, 5))
    )
    assert store.get_opening_balance("P1", "KOL") == Decimal("75000.00")


def test_weekly_snapshot_is_append_only(store):
    row = WeeklySnapshotRow(
        party_id="P1",
        branch_id="KOL",
        week_ending=date(2026, 1, 5),
        opening=Decimal("100000.00"),
        sales=Decimal("5000.00"),
        credit_notes=Decimal("0.00"),
        debit_notes=Decimal("0.00"),
        receipts=Decimal("-2000.00"),
        journals=Decimal("0.00"),
        closing_computed=Decimal("103000.00"),
        closing_extracted=Decimal("103000.00"),
        reconciled=True,
        difference=Decimal("0.00"),
    )
    store.append_weekly_snapshot(row)
    rows = store.weekly_snapshots_for_week(date(2026, 1, 5))
    assert len(rows) == 1
    assert rows[0]["reconciled"] == 1

    # Duplicate week/party/branch must fail, not silently overwrite -
    # append-only means a correction is a new week, not a mutated row.
    with pytest.raises(Exception):
        store.append_weekly_snapshot(row)


def _weekly_movement_row(week_ending, total_ar=Decimal("100000.00")):
    return WeeklyMovementRow(
        week_ending=week_ending,
        recorded_at=datetime(2026, 9, 12, 10, 0),
        total_ar=total_ar,
        open_ar_by_fy={"2026-27": total_ar},
        pre_mis_outstanding=Decimal("5000.00"),
        overdue_ar=Decimal("20000.00"),
        overdue_by_bucket={"1-30": Decimal("20000.00")},
        dso=Decimal("42.50"),
        collection_efficiency=Decimal("77.25"),
        unapplied_cash=Decimal("1500.00"),
    )


def test_record_weekly_movement_then_read_back(store):
    store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 12)))
    rows = store.all_weekly_movement_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.week_ending == date(2026, 9, 12)
    assert row.recorded_at == datetime(2026, 9, 12, 10, 0)
    assert row.total_ar == Decimal("100000.00")
    assert row.open_ar_by_fy == {"2026-27": Decimal("100000.00")}
    assert row.pre_mis_outstanding == Decimal("5000.00")
    assert row.overdue_ar == Decimal("20000.00")
    assert row.overdue_by_bucket == {"1-30": Decimal("20000.00")}
    assert row.dso == Decimal("42.50")
    assert row.collection_efficiency == Decimal("77.25")
    assert row.unapplied_cash == Decimal("1500.00")


def test_weekly_movement_handles_none_dso_and_collection_efficiency(store):
    row = _weekly_movement_row(date(2026, 9, 12))
    row = WeeklyMovementRow(**{**row.__dict__, "dso": None, "collection_efficiency": None})
    store.record_weekly_movement(row)
    stored = store.all_weekly_movement_rows()[0]
    assert stored.dso is None
    assert stored.collection_efficiency is None


def test_weekly_movement_is_append_only(store):
    store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 12)))
    # Recording the same week twice must fail, not silently overwrite -
    # this is the entire mechanism that makes the history genuinely
    # historical rather than a live recompute in disguise.
    with pytest.raises(Exception):
        store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 12)))


def test_has_weekly_movement_for_week(store):
    assert store.has_weekly_movement_for_week(date(2026, 9, 12)) is False
    store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 12)))
    assert store.has_weekly_movement_for_week(date(2026, 9, 12)) is True
    assert store.has_weekly_movement_for_week(date(2026, 9, 19)) is False


def test_all_weekly_movement_rows_ordered_oldest_first(store):
    store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 19), total_ar=Decimal("120000.00")))
    store.record_weekly_movement(_weekly_movement_row(date(2026, 9, 12), total_ar=Decimal("100000.00")))
    rows = store.all_weekly_movement_rows()
    assert [r.week_ending for r in rows] == [date(2026, 9, 12), date(2026, 9, 19)]


def _drift_finding(voucher_number="SB/999", party="Acme", amount=Decimal("13000.00"), voucher_date=date(2026, 1, 4), branch_id="KOL"):
    return DriftFinding(
        party_ledger_name=party, voucher_type="Sales", voucher_number=voucher_number,
        voucher_date=voucher_date, flipped_amount=amount, attributed_week=date(2026, 1, 5),
        voucher=Voucher(
            voucher_type=VoucherType.SALES, voucher_date=voucher_date, voucher_number=voucher_number,
            branch_id=branch_id, party_ledger_name=party,
        ),
    )


def test_record_drift_findings_then_read_back(store):
    finding = _drift_finding()
    new_count = store.record_drift_findings("KOL", [finding], datetime(2026, 1, 6, 9, 0))
    assert new_count == 1

    records = store.all_drift_findings()
    assert len(records) == 1
    r = records[0]
    assert r.branch_id == "KOL"
    assert r.finding.voucher_number == "SB/999"
    assert r.finding.party_ledger_name == "Acme"
    assert r.finding.flipped_amount == Decimal("13000.00")
    assert r.finding.attributed_week == date(2026, 1, 5)
    assert r.discovered_at == datetime(2026, 1, 6, 9, 0)
    assert r.acknowledged is False
    assert r.acknowledged_by is None
    assert r.acknowledged_at is None


def test_record_drift_findings_ignores_an_already_recorded_finding(store):
    finding = _drift_finding()
    first_count = store.record_drift_findings("KOL", [finding], datetime(2026, 1, 6, 9, 0))
    # Same still-unresolved finding re-detected on a later extraction -
    # must not spawn a second row.
    second_count = store.record_drift_findings("KOL", [finding], datetime(2026, 1, 13, 9, 0))
    assert first_count == 1
    assert second_count == 0
    assert len(store.all_drift_findings()) == 1
    # The original discovery time is preserved, not overwritten.
    assert store.all_drift_findings()[0].discovered_at == datetime(2026, 1, 6, 9, 0)


def test_record_drift_findings_distinguishes_different_findings(store):
    f1 = _drift_finding(voucher_number="SB/001")
    f2 = _drift_finding(voucher_number="SB/002")
    count = store.record_drift_findings("KOL", [f1, f2], datetime(2026, 1, 6, 9, 0))
    assert count == 2
    assert len(store.all_drift_findings()) == 2


def test_record_drift_findings_same_voucher_different_branch_is_distinct(store):
    finding = _drift_finding()
    store.record_drift_findings("KOL", [finding], datetime(2026, 1, 6, 9, 0))
    count = store.record_drift_findings("MUM", [finding], datetime(2026, 1, 6, 9, 0))
    assert count == 1
    assert len(store.all_drift_findings()) == 2


def test_all_drift_findings_orders_most_recent_first(store):
    store.record_drift_findings("KOL", [_drift_finding(voucher_number="SB/001")], datetime(2026, 1, 6, 9, 0))
    store.record_drift_findings("KOL", [_drift_finding(voucher_number="SB/002")], datetime(2026, 1, 13, 9, 0))
    records = store.all_drift_findings()
    assert [r.finding.voucher_number for r in records] == ["SB/002", "SB/001"]


def test_acknowledge_drift_finding(store):
    store.record_drift_findings("KOL", [_drift_finding()], datetime(2026, 1, 6, 9, 0))
    finding_id = store.all_drift_findings()[0].id

    store.acknowledge_drift_finding(finding_id, "AR Manager - Kolkata", datetime(2026, 1, 7, 10, 0))

    record = store.all_drift_findings()[0]
    assert record.acknowledged is True
    assert record.acknowledged_by == "AR Manager - Kolkata"
    assert record.acknowledged_at == datetime(2026, 1, 7, 10, 0)


def test_drift_finding_voucher_round_trips_full_entries():
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 1, 4), voucher_number="SB/999",
        branch_id="KOL", party_ledger_name="Acme", raw_voucher_type_name="Sales - Export",
        entries=[
            LedgerEntry(party_ledger_name="Acme", amount_as_extracted=Decimal("-13000.00"), bill_name="SB/999", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("11000.00")),
            LedgerEntry(party_ledger_name="CGST", amount_as_extracted=Decimal("1000.00")),
            LedgerEntry(party_ledger_name="SGST", amount_as_extracted=Decimal("1000.00")),
        ],
    )
    from ar_mis.storage import Store
    raw = Store._serialize_voucher(voucher)
    restored = Store._deserialize_voucher(raw)
    assert restored == voucher


def test_drift_finding_stores_and_returns_the_full_voucher(store):
    finding = _drift_finding()
    store.record_drift_findings("KOL", [finding], datetime(2026, 1, 6, 9, 0))
    record = store.all_drift_findings()[0]
    assert record.finding.voucher == finding.voucher


def test_get_drift_finding_by_id(store):
    store.record_drift_findings("KOL", [_drift_finding()], datetime(2026, 1, 6, 9, 0))
    finding_id = store.all_drift_findings()[0].id
    record = store.get_drift_finding(finding_id)
    assert record is not None
    assert record.id == finding_id
    assert record.finding.voucher_number == "SB/999"


def test_get_drift_finding_returns_none_for_unknown_id(store):
    assert store.get_drift_finding(999) is None


def test_mark_drift_finding_incorporated(store):
    store.record_drift_findings("KOL", [_drift_finding()], datetime(2026, 1, 6, 9, 0))
    finding_id = store.all_drift_findings()[0].id

    store.mark_drift_finding_incorporated(finding_id, "AR Manager - Kolkata", datetime(2026, 9, 13, 9, 0), date(2026, 9, 12))

    record = store.get_drift_finding(finding_id)
    assert record.incorporated is True
    assert record.incorporated_by == "AR Manager - Kolkata"
    assert record.incorporated_at == datetime(2026, 9, 13, 9, 0)
    assert record.incorporated_week_ending == date(2026, 9, 12)
    # Incorporating is independent of acknowledging.
    assert record.acknowledged is False


def test_has_weekly_snapshot_for_party_week(store):
    assert store.has_weekly_snapshot_for_party_week("Acme", "KOL", date(2026, 9, 12)) is False
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="Acme", branch_id="KOL", week_ending=date(2026, 9, 12),
            opening=Decimal("0.00"), sales=Decimal("1000.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("1000.00"), closing_extracted=Decimal("1000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    assert store.has_weekly_snapshot_for_party_week("Acme", "KOL", date(2026, 9, 12)) is True
    # A different party in the same branch/week must not be affected.
    assert store.has_weekly_snapshot_for_party_week("Beta", "KOL", date(2026, 9, 12)) is False


def test_ptp_status_log_tracks_latest_status_per_week(store):
    store.upsert_customer_master(CustomerMasterRecord("P1", "Acme", "KOL", Decimal("0.00")))
    entry = PTPEntry(
        ptp_id="ptp-1",
        party_id="P1",
        branch_id="KOL",
        created_week=date(2026, 1, 5),
        promised_amount=Decimal("50000.00"),
        promised_date=date(2026, 1, 12),
        owner="AR Manager - Kolkata",
    )
    store.create_ptp_entry(entry)
    store.log_ptp_status(PTPStatusLogRow("ptp-1", date(2026, 1, 5), PTPStatus.ACTIVE, "AR Manager - Kolkata"))

    assert store.current_ptp_status("ptp-1", date(2026, 1, 5)) == PTPStatus.ACTIVE
    assert store.current_ptp_status("ptp-1", date(2026, 1, 12)) == PTPStatus.ACTIVE

    store.log_ptp_status(PTPStatusLogRow("ptp-1", date(2026, 1, 19), PTPStatus.BROKEN, "AR Manager - Kolkata", "Payment not received"))
    # History preserved: earlier week still reads as Active as-of that date.
    assert store.current_ptp_status("ptp-1", date(2026, 1, 12)) == PTPStatus.ACTIVE
    assert store.current_ptp_status("ptp-1", date(2026, 1, 19)) == PTPStatus.BROKEN


def test_branch_master_crud(store):
    assert store.list_branches() == []
    kol = BranchConfig("KOL", "Kolkata", "Kolkata HQ", "192.168.1.10", 9000)
    store.upsert_branch(kol)
    assert store.list_branches() == [kol]
    assert store.get_branch("KOL") == kol
    assert store.get_branch("NOPE") is None

    updated = BranchConfig("KOL", "Kolkata HQ Renamed", "Kolkata HQ", "192.168.1.20", 9001)
    store.upsert_branch(updated)
    branches = store.list_branches()
    assert len(branches) == 1
    assert branches[0] == updated

    store.delete_branch("KOL")
    assert store.list_branches() == []


def test_voucher_log_dedupes_and_supports_key_lookup(store):
    store.append_voucher_log_entry(
        "KOL", date(2026, 1, 5), "Acme", "Sales", "SB/1", date(2026, 1, 3), Decimal("5000.00")
    )
    # Re-logging the identical voucher (e.g. a re-run of the same week)
    # must not duplicate - INSERT OR IGNORE on the natural key.
    store.append_voucher_log_entry(
        "KOL", date(2026, 1, 5), "Acme", "Sales", "SB/1", date(2026, 1, 3), Decimal("5000.00")
    )
    keys = store.logged_voucher_keys("KOL", "Acme")
    assert keys == {("Sales", "SB/1", date(2026, 1, 3).isoformat(), "5000.00")}


# ---- Schema versioning / automatic migration + backup (design doc item 13) --


def test_new_database_is_stamped_at_current_version_with_no_backup(tmp_path):
    db_path = tmp_path / "fresh.db"
    s = Store(str(db_path))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()
    backups = list(tmp_path.glob("*.backup-*"))
    assert backups == []


def test_reopening_at_current_version_does_not_create_another_backup(tmp_path):
    db_path = tmp_path / "test.db"
    Store(str(db_path)).close()
    Store(str(db_path)).close()
    backups = list(tmp_path.glob("*.backup-*"))
    assert backups == []


def test_legacy_database_at_version_zero_is_migrated_and_backed_up(tmp_path):
    # Simulates a real pre-this-mechanism database: tables already exist
    # in today's shape but PRAGMA user_version was never set (SQLite's own
    # default is 0). Migrating it must be a no-op on the schema itself
    # (idempotent CREATE TABLE IF NOT EXISTS) but must still take a backup
    # and stamp the version - "regardless" per the design doc, even though
    # nothing here actually needed to change.
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE branch_master (branch_id TEXT PRIMARY KEY, branch_name TEXT NOT NULL,"
        " tally_company_name TEXT NOT NULL, tally_host TEXT NOT NULL DEFAULT 'localhost',"
        " tally_port INTEGER NOT NULL DEFAULT 9000)"
    )
    conn.execute(
        "INSERT INTO branch_master (branch_id, branch_name, tally_company_name) VALUES ('B1', 'Branch One', 'ACME')"
    )
    conn.commit()
    conn.close()

    store = Store(str(db_path))
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # Pre-existing data survives the migration untouched.
    assert store.get_branch("B1").branch_name == "Branch One"
    store.close()

    backups = list(tmp_path.glob("legacy.db.backup-v0-*"))
    assert len(backups) == 1
    backup_conn = sqlite3.connect(str(backups[0]))
    row = backup_conn.execute("SELECT branch_name FROM branch_master WHERE branch_id='B1'").fetchone()
    assert row == ("Branch One",)
    backup_conn.close()


# ---- Sales & Debit Note Register -----------------------------------------


def _sales_dn_row(voucher_number="INV001", party_id="ACME"):
    return SalesDNRegisterRow(
        branch_id="B1",
        invoice_date=date(2026, 4, 10),
        note_type=NoteType.INVOICE,
        voucher_number=voucher_number,
        bill_allocation_reference=voucher_number,
        party_id=party_id,
        taxable_value=Decimal("1000.00"),
        cgst=Decimal("90.00"),
        sgst=Decimal("90.00"),
        igst=Decimal("0.00"),
        invoice_value=Decimal("1180.00"),
        due_date=date(2026, 5, 10),
    )


def test_sales_dn_row_round_trips(store):
    store.append_sales_dn_row(_sales_dn_row())
    rows = store.all_sales_dn_rows()
    assert len(rows) == 1
    assert rows[0] == _sales_dn_row()


def test_sales_dn_row_dedupes_on_reextraction(store):
    # Re-extracting an overlapping date range must not duplicate a voucher
    # already recorded - insert the identical row twice.
    store.append_sales_dn_row(_sales_dn_row())
    store.append_sales_dn_row(_sales_dn_row())
    assert len(store.all_sales_dn_rows()) == 1


def test_sales_dn_rows_filtered_by_branch(store):
    store.append_sales_dn_row(_sales_dn_row("INV001"))
    row2 = SalesDNRegisterRow(**{**_sales_dn_row("INV002").__dict__, "branch_id": "B2"})
    store.append_sales_dn_row(row2)
    assert len(store.all_sales_dn_rows("B1")) == 1
    assert len(store.all_sales_dn_rows("B2")) == 1
    assert len(store.all_sales_dn_rows()) == 2


# ---- Credit Note Register + item-10 resolution ---------------------------


def test_credit_note_row_round_trips_and_resolves_classification(store):
    row = CreditNoteRegisterRow(
        branch_id="B1", cn_date=date(2026, 4, 15), voucher_number="CN001", party_id="ACME",
        cn_amount=Decimal("200.00"), bill_allocation_reference="INV-UNKNOWN",
        classification=RegisterClassification.PENDING_REVIEW,
    )
    store.append_credit_note_row(row)
    assert store.all_credit_note_rows()[0].classification == RegisterClassification.PENDING_REVIEW

    store.resolve_credit_note_classification(
        "B1", "CN001", "ACME", RegisterClassification.PRE_MIS_ADJUSTMENT, "Confirmed pre-MIS reference"
    )
    resolved = store.all_credit_note_rows()[0]
    assert resolved.classification == RegisterClassification.PRE_MIS_ADJUSTMENT
    assert resolved.reason == "Confirmed pre-MIS reference"


def test_resolve_credit_note_classification_raises_for_unknown_row(store):
    with pytest.raises(ValueError, match="No credit_note_register row"):
        store.resolve_credit_note_classification(
            "B1", "GHOST", "ACME", RegisterClassification.PRE_MIS_ADJUSTMENT, "n/a"
        )


# ---- Receipt & Journal Register + voucher-level classification ----------


def test_receipt_journal_rows_round_trip(store):
    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 20), voucher_type="Receipt",
            voucher_number="RCPT001", party_id="ACME", amount=Decimal("500.00"), target_doc_no="INV001",
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 20), voucher_type="Receipt",
            voucher_number="RCPT001", party_id="ACME", amount=Decimal("300.00"), target_doc_no=None,
        ),
    ]
    store.append_receipt_journal_rows(rows)
    assert len(store.all_receipt_journal_rows()) == 2


def test_resolve_receipt_journal_classification_promotes_every_line_of_the_voucher(store):
    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 20), voucher_type="Receipt",
            voucher_number="RCPT001", party_id="ACME", amount=Decimal("500.00"), target_doc_no="INV001",
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 20), voucher_type="Receipt",
            voucher_number="RCPT001", party_id="ACME", amount=Decimal("300.00"), target_doc_no=None,
        ),
    ]
    store.append_receipt_journal_rows(rows)
    store.resolve_receipt_journal_classification("B1", "RCPT001", "ACME", RegisterClassification.PRE_MIS_ADJUSTMENT)
    classifications = {r.target_doc_no: r.classification for r in store.all_receipt_journal_rows()}
    assert classifications == {
        "INV001": RegisterClassification.PRE_MIS_ADJUSTMENT,
        None: RegisterClassification.PRE_MIS_ADJUSTMENT,
    }


def test_resolve_receipt_journal_classification_raises_for_unknown_voucher(store):
    with pytest.raises(ValueError, match="No receipt_journal_register rows"):
        store.resolve_receipt_journal_classification("B1", "GHOST", "ACME", RegisterClassification.CURRENT)


# ---- Invoice Follow-Up (item 7 - the one mutable register row) ----------


def test_invoice_follow_up_upserts_in_place(store):
    assert store.get_invoice_follow_up("B1", "INV001", "ACME") is None

    store.upsert_invoice_follow_up(
        InvoiceFollowUp(
            branch_id="B1", voucher_number="INV001", party_id="ACME",
            ptp_date=date(2026, 5, 1), ptp_amount=Decimal("1180.00"),
            next_action="Call customer", updated_by="alice",
        ),
        today=date(2026, 4, 20),
    )
    got = store.get_invoice_follow_up("B1", "INV001", "ACME")
    assert got.next_action == "Call customer"
    assert got.ptp_amount == Decimal("1180.00")

    # A second upsert against the same identity overwrites in place -
    # this is the one register row that is genuinely mutable, not
    # append-only.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         next_action="Escalated to legal", updated_by="bob"),
        today=date(2026, 4, 25),
    )
    got2 = store.get_invoice_follow_up("B1", "INV001", "ACME")
    assert got2.next_action == "Escalated to legal"
    assert got2.updated_by == "bob"
    assert got2.ptp_date is None  # cleared, not carried over from the prior upsert


def test_invoice_follow_up_logged_at_stamps_only_when_the_promise_changes(store):
    # First time a promise is logged - logged_at is stamped with today.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 5, 1), ptp_amount=Decimal("60000.00")),
        today=date(2026, 4, 20),
    )
    assert store.get_invoice_follow_up("B1", "INV001", "ACME").logged_at == date(2026, 4, 20)

    # Editing something unrelated to the promise (next_action) must NOT
    # move logged_at - the PTP Kept Rate calculation depends on this
    # marking exactly when the CURRENT promise started.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 5, 1), ptp_amount=Decimal("60000.00"),
                         next_action="Follow up by phone"),
        today=date(2026, 4, 22),
    )
    assert store.get_invoice_follow_up("B1", "INV001", "ACME").logged_at == date(2026, 4, 20)

    # Changing the promise itself (a new PTP amount) re-stamps logged_at
    # to when the NEW promise was made.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 5, 10), ptp_amount=Decimal("40000.00")),
        today=date(2026, 4, 26),
    )
    assert store.get_invoice_follow_up("B1", "INV001", "ACME").logged_at == date(2026, 4, 26)

    # Clearing the promise entirely clears logged_at too.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME"),
        today=date(2026, 5, 15),
    )
    assert store.get_invoice_follow_up("B1", "INV001", "ACME").logged_at is None


def test_all_invoice_follow_ups_filters_by_branch(store):
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 5, 1), ptp_amount=Decimal("1000.00")),
        today=date(2026, 4, 1),
    )
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B2", voucher_number="INV002", party_id="OTHER",
                         ptp_date=date(2026, 5, 1), ptp_amount=Decimal("2000.00")),
        today=date(2026, 4, 1),
    )
    assert len(store.all_invoice_follow_ups()) == 2
    assert len(store.all_invoice_follow_ups("B1")) == 1
    assert store.all_invoice_follow_ups("B1")[0].voucher_number == "INV001"


# ---- FY rollover snapshot (item 4) ---------------------------------------


def test_fy_rollover_snapshot_round_trips_and_is_append_only(store):
    snapshot = FYRolloverSnapshot(
        branch_id="B1", voucher_number="INV001", party_id="ACME",
        from_financial_year="2025-26", to_financial_year="2026-27",
        open_amount_at_rollover=Decimal("680.00"), rolled_over_by="alice",
        rolled_over_at=date(2026, 4, 1),
    )
    store.append_fy_rollover_snapshot(snapshot)
    rows = store.fy_rollover_snapshots_for_invoice("B1", "INV001", "ACME")
    assert rows == [snapshot]

    # Rolling the same invoice into the same FY transition twice must not
    # create a duplicate row (UNIQUE guard).
    store.append_fy_rollover_snapshot(snapshot)
    assert len(store.fy_rollover_snapshots_for_invoice("B1", "INV001", "ACME")) == 1


# ---- Extraction log (freshness indicator) --------------------------------


def test_last_extraction_at_is_none_when_nothing_extracted_yet(store):
    assert store.last_extraction_at() is None
    assert store.last_extraction_at("B1") is None


def test_last_extraction_at_returns_the_most_recent_run(store):
    store.record_extraction_run("B1", date(2026, 4, 7), datetime(2026, 4, 8, 9, 0, 0))
    store.record_extraction_run("B1", date(2026, 4, 14), datetime(2026, 4, 15, 10, 30, 0))
    assert store.last_extraction_at("B1") == datetime(2026, 4, 15, 10, 30, 0)


def test_last_extraction_at_across_all_branches_is_the_most_recent_of_any(store):
    store.record_extraction_run("B1", date(2026, 4, 7), datetime(2026, 4, 8, 9, 0, 0))
    store.record_extraction_run("B2", date(2026, 4, 7), datetime(2026, 4, 20, 9, 0, 0))
    assert store.last_extraction_at() == datetime(2026, 4, 20, 9, 0, 0)
    # Per-branch still returns only that branch's own most recent run.
    assert store.last_extraction_at("B1") == datetime(2026, 4, 8, 9, 0, 0)


# ---- Branch Master data-coverage summary + delete-latest-week -----------
# Client's explicit ask: with no way today to undo a mistaken extraction,
# an error caught right after Save is stuck in the system forever. Two
# real calendar weeks (Mon 2026-03-30 - Sun 2026-04-05, then Mon 2026-04-06
# - Sun 2026-04-12) built via the real pipeline, matching exactly how the
# merged Test & Save Extraction flow now saves one week at a time.


def _run_week(store, week_ending, voucher_number, invoice_date, party="ACME", period_start=None):
    from ar_mis.pipeline import process_branch_data

    if period_start is None:
        period_start = week_ending - timedelta(days=6)
    store.upsert_customer_master(CustomerMasterRecord(party, party, "KOL", Decimal("0.00")))
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=invoice_date, voucher_number=voucher_number,
        branch_id="KOL", party_ledger_name=party,
        entries=[
            LedgerEntry(party_ledger_name=party, amount_as_extracted=Decimal("-1000.00"),
                        bill_name=voucher_number, bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("1000.00")),
        ],
    )
    return process_branch_data(
        store, "KOL", "Kolkata", week_ending, [voucher], {party: Decimal("-1000.00")}, period_start=period_start
    )


def test_week_endings_for_branch_is_empty_before_any_extraction(store):
    assert store.week_endings_for_branch("KOL") == []
    assert store.branch_data_summary("KOL") is None


def test_branch_data_summary_reports_earliest_start_latest_end_and_count(store):
    _run_week(store, date(2026, 4, 5), "SB/1", date(2026, 4, 2))
    _run_week(store, date(2026, 4, 12), "SB/2", date(2026, 4, 9))

    assert store.week_endings_for_branch("KOL") == [date(2026, 4, 5), date(2026, 4, 12)]
    summary = store.branch_data_summary("KOL")
    assert summary == {
        "earliest_week_start": date(2026, 3, 30),
        "latest_week_end": date(2026, 4, 12),
        "week_count": 2,
    }


def test_delete_branch_week_refuses_a_week_that_is_not_the_latest(store):
    _run_week(store, date(2026, 4, 5), "SB/1", date(2026, 4, 2))
    _run_week(store, date(2026, 4, 12), "SB/2", date(2026, 4, 9))

    with pytest.raises(ValueError, match="most recently recorded"):
        store.delete_branch_week("KOL", date(2026, 4, 5))

    # Nothing was touched by the refused attempt.
    assert len(store.all_sales_dn_rows("KOL")) == 2
    assert store.week_endings_for_branch("KOL") == [date(2026, 4, 5), date(2026, 4, 12)]


def test_delete_branch_week_refuses_when_branch_has_no_data(store):
    with pytest.raises(ValueError, match="most recently recorded"):
        store.delete_branch_week("KOL", date(2026, 4, 5))


def test_delete_branch_week_removes_only_the_latest_weeks_data(store):
    _run_week(store, date(2026, 4, 5), "SB/1", date(2026, 4, 2))
    _run_week(store, date(2026, 4, 12), "SB/2", date(2026, 4, 9))

    store.delete_branch_week("KOL", date(2026, 4, 12))

    assert store.week_endings_for_branch("KOL") == [date(2026, 4, 5)]
    remaining = store.all_sales_dn_rows("KOL")
    assert [r.voucher_number for r in remaining] == ["SB/1"]
    assert store.logged_voucher_keys("KOL", "ACME") == {
        ("Sales", "SB/1", "2026-04-02", "1000.00")
    }
    # extraction_log row for the deleted week is gone - only week 1's remains.
    cur = store.conn.execute("SELECT week_ending FROM extraction_log WHERE branch_id='KOL'")
    assert [r[0] for r in cur.fetchall()] == ["2026-04-05"]


def test_delete_branch_week_cascades_to_invoice_follow_up(store):
    _run_week(store, date(2026, 4, 5), "SB/1", date(2026, 4, 2))

    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="KOL", voucher_number="SB/1", party_id="ACME",
                         next_action="Call customer"),
        today=date(2026, 4, 6),
    )
    assert store.get_invoice_follow_up("KOL", "SB/1", "ACME") is not None

    store.delete_branch_week("KOL", date(2026, 4, 5))

    assert store.get_invoice_follow_up("KOL", "SB/1", "ACME") is None


def test_delete_branch_week_removes_only_that_weeks_drift_findings(store):
    from ar_mis.reconciliation import DriftFinding

    _run_week(store, date(2026, 4, 5), "SB/1", date(2026, 4, 2))
    _run_week(store, date(2026, 4, 12), "SB/2", date(2026, 4, 9))

    backdated_voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 3), voucher_number="SB/BACKDATED",
        branch_id="KOL", party_ledger_name="ACME",
        entries=[LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-500.00"))],
    )
    week1_finding = DriftFinding(
        party_ledger_name="ACME", voucher_type="Sales", voucher_number="SB/BACKDATED",
        voucher_date=date(2026, 4, 3), flipped_amount=Decimal("500.00"),
        attributed_week=date(2026, 4, 5), voucher=backdated_voucher,
    )
    week2_finding = DriftFinding(
        party_ledger_name="ACME", voucher_type="Sales", voucher_number="SB/BACKDATED2",
        voucher_date=date(2026, 4, 10), flipped_amount=Decimal("500.00"),
        attributed_week=date(2026, 4, 12), voucher=backdated_voucher,
    )
    store.record_drift_findings("KOL", [week1_finding, week2_finding], datetime(2026, 4, 13, 9, 0, 0))

    store.delete_branch_week("KOL", date(2026, 4, 12))

    remaining = store.all_drift_findings()
    assert [r.finding.voucher_number for r in remaining] == ["SB/BACKDATED"]


# ---- Register Build Exceptions (review log) --------------------------


def test_append_and_read_back_register_build_exceptions(store):
    store.append_register_build_exceptions(
        "KOL", date(2026, 4, 5),
        [("SB/0142", "Ghost Party", "Sales", "No customer_master record for 'Ghost Party'")],
        datetime(2026, 4, 7, 9, 0, 0),
    )
    records = store.all_register_build_exceptions()
    assert len(records) == 1
    r = records[0]
    assert r.branch_id == "KOL"
    assert r.week_ending == date(2026, 4, 5)
    assert r.voucher_number == "SB/0142"
    assert r.party_ledger_name == "Ghost Party"
    assert r.voucher_type == "Sales"
    assert r.reason == "No customer_master record for 'Ghost Party'"
    assert r.logged_at == datetime(2026, 4, 7, 9, 0, 0)
    assert r.status == "open"
    assert r.reviewed_by is None


def test_append_register_build_exceptions_handles_empty_party_name(store):
    store.append_register_build_exceptions(
        "KOL", date(2026, 4, 5),
        [("SB/0999", "", "Sales", "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")],
        datetime(2026, 4, 7, 9, 0, 0),
    )
    records = store.all_register_build_exceptions()
    assert records[0].party_ledger_name == ""


def test_all_register_build_exceptions_open_ones_come_first(store):
    store.append_register_build_exceptions("KOL", date(2026, 4, 5), [("SB/1", "P1", "Sales", "reason 1")], datetime(2026, 4, 7, 9, 0))
    store.append_register_build_exceptions("KOL", date(2026, 4, 12), [("SB/2", "P2", "Sales", "reason 2")], datetime(2026, 4, 14, 9, 0))
    records = store.all_register_build_exceptions()
    reviewed_id = next(r.id for r in records if r.voucher_number == "SB/1")
    store.review_register_build_exception(reviewed_id, "reviewed_no_action", "Priya", datetime(2026, 4, 15, 9, 0), "genuine SC")

    records = store.all_register_build_exceptions()
    assert records[0].voucher_number == "SB/2"  # still-open one leads
    assert records[0].status == "open"
    assert records[1].voucher_number == "SB/1"
    assert records[1].status == "reviewed_no_action"


def test_review_register_build_exception_records_disposition(store):
    store.append_register_build_exceptions("KOL", date(2026, 4, 5), [("SB/1", "Narendra Trading Company", "Sales", "reason")], datetime(2026, 4, 7, 9, 0))
    exception_id = store.all_register_build_exceptions()[0].id

    store.review_register_build_exception(
        exception_id, "resolved_via_catchup", "Priya", datetime(2026, 4, 20, 11, 0), "Onboarded via Catch Up a Party"
    )

    record = store.all_register_build_exceptions()[0]
    assert record.status == "resolved_via_catchup"
    assert record.reviewed_by == "Priya"
    assert record.reviewed_at == datetime(2026, 4, 20, 11, 0)
    assert record.reviewed_note == "Onboarded via Catch Up a Party"
