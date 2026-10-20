"""Tests for the manual XML upload fallback (ar_mis.manual_upload) -
built for when the live Tally gateway isn't reachable. Uses the same
fixture XML the live-extraction parser tests use, since parsing never
cared whether XML arrived over HTTP or as a file - only how the values
get into process_branch_data differs.
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.manual_upload import ManualUploadRefused, process_manual_upload
from ar_mis.models import CustomerMasterRecord
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


def test_process_manual_upload_clean_run_writes_tb_balances(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
    assert len(store.all_party_tb_balances()) == 2


def test_process_manual_upload_recon_fail_still_writes_data(store):
    # Wrong opening balances (0.00) - will not match the TB closing. Per
    # the client's explicit instruction, this no longer withholds data:
    # the outcome is still PASS, with the mismatched parties reported via
    # failed_parties, and their Tally-stated balances are still written.
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
    assert len(store.all_party_tb_balances()) == 2


def test_process_manual_upload_refuses_when_already_recorded(store):
    _seed_matching_openings(store)
    store.record_extraction_run("KOL", date(2026, 4, 7), datetime(2026, 4, 7, 9, 0))
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    with pytest.raises(ManualUploadRefused):
        process_manual_upload(
            store, "KOL", "Kolkata", date(2026, 4, 7),
            weekly_voucher_xml={"Sales": sales_xml},
            trial_balance_xml=tb_xml,
        )
    # Nothing new was written - no party_tb_balance rows at all.
    assert store.all_party_tb_balances() == {}


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


def test_process_manual_upload_runs_ytd_cross_check_when_ytd_file_given(store):
    # Phase 6e: Manual Upload's YTD file is Tally's own "Sundry Debtors ->
    # Ctrl+H -> Voucher view" Display Report export (fixtures/manual_ytd_
    # voucher_display_report.xml, built from the real confirmed shape) -
    # not the live gateway's Voucher Collection shape the old isolate_
    # drift-based test used. SB/0142 matches what's in the Sales register
    # already; SB/0199-BACKDATED does not exist there at all.
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()
    ytd_xml = (FIXTURES / "manual_ytd_voucher_display_report.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
        ytd_voucher_xml=ytd_xml,
    )
    assert result.outcome.outcome == ExtractionOutcome.PASS
    # SB/0142 (in both), SB/0199-BACKDATED (YTD file only), and SB/0143
    # (register only, per voucher_collection_sales.xml's own second
    # voucher - Reliable Cargo Movers - which the YTD file doesn't cover).
    assert len(result.ytd_vs_register_rows) == 3
    by_voucher = {r.voucher_number: r for r in result.ytd_vs_register_rows}
    assert by_voucher["SB/0142"].status == "Match"
    assert by_voucher["SB/0199-BACKDATED"].status == "Missing from Registers"
    assert by_voucher["SB/0143"].status == "Extra in Registers"

    # Never persisted anywhere - there is no party dimension to key a
    # stored row on (see ManualYtdVoucherRow's own docstring) - shown
    # once on this call's own return value only.
    assert store.all_drift_findings() == []


def test_process_manual_upload_skips_ytd_cross_check_when_ytd_file_omitted(store):
    _seed_matching_openings(store)
    sales_xml = (FIXTURES / "voucher_collection_sales.xml").read_text()
    tb_xml = (FIXTURES / "ledger_closing_balances.xml").read_text()

    result = process_manual_upload(
        store, "KOL", "Kolkata", date(2026, 4, 7),
        weekly_voucher_xml={"Sales": sales_xml},
        trial_balance_xml=tb_xml,
    )
    assert result.ytd_vs_register_rows == []


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
