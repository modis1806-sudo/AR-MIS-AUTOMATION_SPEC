import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    FYRolloverSnapshot,
    InvoiceFollowUp,
    NoteType,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    WeeklySnapshotRow,
)
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
        )
    )
    got = store.get_invoice_follow_up("B1", "INV001", "ACME")
    assert got.next_action == "Call customer"
    assert got.ptp_amount == Decimal("1180.00")

    # A second upsert against the same identity overwrites in place -
    # this is the one register row that is genuinely mutable, not
    # append-only.
    store.upsert_invoice_follow_up(
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         next_action="Escalated to legal", updated_by="bob")
    )
    got2 = store.get_invoice_follow_up("B1", "INV001", "ACME")
    assert got2.next_action == "Escalated to legal"
    assert got2.updated_by == "bob"
    assert got2.ptp_date is None  # cleared, not carried over from the prior upsert


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
