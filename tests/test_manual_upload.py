"""Tests for the manual XML upload fallback (ar_mis.manual_upload) -
built for when the live Tally gateway isn't reachable. Uses the same
fixture XML the live-extraction parser tests use, since parsing never
cared whether XML arrived over HTTP or as a file - only how the values
get into process_branch_data differs.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.manual_upload import ManualUploadRefused, process_manual_upload
from ar_mis.models import CustomerMasterRecord, WeeklySnapshotRow
from ar_mis.orchestration import ExtractionOutcome
from ar_mis.storage import Store

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def _seed_matching_openings(store):
    # Opening balances chosen so opening + sales(flipped) = TB(flipped)
    # exactly, for the two parties in voucher_collection_sales.xml /
    # ledger_closing_balances.xml.
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport Pvt Ltd", "A & B Transport Pvt Ltd", "KOL", Decimal("300000.00"))
    )
    store.upsert_customer_master(
        CustomerMasterRecord("Reliable Cargo Movers", "Reliable Cargo Movers", "KOL", Decimal("62500.00"))
    )


def test_process_manual_upload_clean_run_writes_snapshot(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
    rows = store.weekly_snapshots_for_week(date(2026, 4, 7))
    assert len(rows) == 2


def test_process_manual_upload_recon_fail_still_writes_data(store):
    # Wrong opening balances (0.00) - will not match the TB closing. Per
    # the client's explicit instruction, this no longer withholds data:
    # the outcome is still PASS, with the mismatched parties reported via
    # failed_parties, and their weekly_snapshot rows are still written.
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport Pvt Ltd", "A & B Transport Pvt Ltd", "KOL", Decimal("0.00"))
    )
    store.upsert_customer_master(
        CustomerMasterRecord("Reliable Cargo Movers", "Reliable Cargo Movers", "KOL", Decimal("0.00"))
    )
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
    assert result.outcome.failed_parties != []
    assert len(store.weekly_snapshots_for_week(date(2026, 4, 7))) == 2


def test_process_manual_upload_refuses_when_already_recorded(store):
    _seed_matching_openings(store)
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="A & B Transport Pvt Ltd", branch_id="KOL", week_ending=date(2026, 4, 7),
            opening=Decimal("300000.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("300000.00"), closing_extracted=Decimal("300000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    with pytest.raises(ManualUploadRefused):
        process_manual_upload(
            store, "KOL", "Kolkata", date(2026, 4, 7),
            weekly_voucher_xml={"Sales": sales_xml},
            trial_balance_xml=tb_xml,
        )
    # Nothing new was written - still exactly the one pre-existing row.
    assert len(store.weekly_snapshots_for_week(date(2026, 4, 7))) == 1


def test_process_manual_upload_auto_discovers_new_customer(store):
    # No customer_master seeding at all - both parties are "new".
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    # closing_extracted for both parties is nonzero while opening defaults
    # to 0 for a brand-new party, so this will NOT reconcile clean - that's
    # expected and correct (a truly new party with a nonzero YTD closing
    # and no matching movement is a real mismatch, not a bug). What matters
    # here is that it doesn't crash, and that both parties get auto-created.
    process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert store.customer_master_exists("A & B Transport Pvt Ltd", "KOL")
    assert store.customer_master_exists("Reliable Cargo Movers", "KOL")
    assert store.get_opening_balance("A & B Transport Pvt Ltd", "KOL") == Decimal("0.00")


def test_process_manual_upload_runs_drift_check_when_ytd_file_given(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()
    ytd_xml = (FIXTURES / "voucher_collection_sales_with_backdated_entry.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
        ytd_voucher_xml=ytd_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
    assert len(result.drift_findings) == 1
    assert result.drift_findings[0].voucher_number == "SB/0099-BACKDATED"

    # Found and fixed this session: the finding must be persisted, not
    # just shown once on this call's own return value.
    persisted = store.all_drift_findings()
    assert len(persisted) == 1
    assert persisted[0].finding.voucher_number == "SB/0099-BACKDATED"
    assert persisted[0].branch_id == "KOL"
    assert persisted[0].acknowledged is False


def test_process_manual_upload_skips_drift_check_when_ytd_file_omitted(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert result.drift_findings == []


def test_process_manual_upload_ignores_blank_voucher_slots(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml, "Credit Note": "", "Receipt": "   "},
        trial_balance_xml=tb_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
