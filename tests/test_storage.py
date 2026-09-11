from datetime import date
from decimal import Decimal

import pytest

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CustomerMasterRecord,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    WeeklySnapshotRow,
)
from ar_mis.storage import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


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
