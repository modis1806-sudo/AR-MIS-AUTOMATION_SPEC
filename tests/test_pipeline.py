"""Tests for ar_mis.pipeline.process_branch_data's register-building step
(_build_and_persist_registers) - the wiring that turns extracted vouchers
into the actual stored Sales & DN / Credit Note / Receipt & Journal
registers (docs/registers_and_reporting_design.md items 1-2), which
ar_mis.registers' own tests exercise as pure functions but nothing
previously called from a real extraction run.

Vouchers are constructed directly (matching ar_mis.tests.test_registers'
own convention) rather than via XML fixtures, since this module is
testing the pipeline's own wiring - not parsing, which is already
covered elsewhere.
"""
from datetime import date, datetime
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, LedgerEntry, Voucher, VoucherType
from ar_mis.orchestration import ExtractionOutcome
from ar_mis.pipeline import process_branch_data
from ar_mis.storage import Store

import pytest


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def _sales_voucher(voucher_number, party, amount, voucher_date=date(2026, 4, 6)):
    return Voucher(
        voucher_type=VoucherType.SALES, voucher_date=voucher_date, voucher_number=voucher_number,
        branch_id="KOL", party_ledger_name=party,
        entries=[
            LedgerEntry(party_ledger_name=party, amount_as_extracted=-amount, bill_name=voucher_number, bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=amount),
        ],
    )


def test_pass_run_builds_and_persists_sales_dn_register(store):
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))

    outcome = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport": Decimal("125000.00")}
    )

    assert outcome.outcome == ExtractionOutcome.PASS
    rows = store.all_sales_dn_rows("KOL")
    assert len(rows) == 1
    assert rows[0].voucher_number == "SB/0142"
    assert rows[0].invoice_value == Decimal("125000.00")
    assert rows[0].party_id == "A & B Transport"


def test_pass_run_uses_customer_credit_period_for_due_date(store):
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00"), credit_period_days=45)
    )
    voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"), voucher_date=date(2026, 4, 6))

    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport": Decimal("125000.00")})

    row = store.all_sales_dn_rows("KOL")[0]
    assert row.due_date == date(2026, 5, 21)  # 2026-04-06 + 45 days


def test_recon_fail_still_writes_register_rows_and_weekly_snapshot(store):
    # Wrong closing balance - reconciliation fails for this party, but
    # per the client's explicit instruction data must never be discarded:
    # the invoice-level register rows and the weekly_snapshot row (with
    # its real, nonzero difference) are still written, for this party AND
    # any other party in the same branch/week.
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))

    outcome = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport": Decimal("999999.00")}
    )

    assert outcome.outcome == ExtractionOutcome.PASS
    assert outcome.failed_parties == ["A & B Transport"]
    rows = store.all_sales_dn_rows("KOL")
    assert len(rows) == 1
    assert rows[0].voucher_number == "SB/0142"

    snapshot_rows = store.weekly_snapshots_for_week(date(2026, 4, 7))
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["reconciled"] == 0
    assert Decimal(snapshot_rows[0]["difference"]) != Decimal("0.00")


def test_recon_fail_does_not_block_other_parties_in_the_same_branch(store):
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("Clean Party", "Clean Party", "KOL", Decimal("0.00")))
    bad_voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))
    good_voucher = _sales_voucher("SB/0200", "Clean Party", Decimal("50000.00"))

    outcome = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [bad_voucher, good_voucher],
        {"A & B Transport": Decimal("999999.00"), "Clean Party": Decimal("50000.00")},
    )

    assert outcome.outcome == ExtractionOutcome.PASS
    assert outcome.failed_parties == ["A & B Transport"]
    # Clean Party's data must be written even though A & B Transport failed.
    voucher_numbers = {row.voucher_number for row in store.all_sales_dn_rows("KOL")}
    assert voucher_numbers == {"SB/0142", "SB/0200"}


def test_next_week_rolls_forward_from_tally_closing_after_a_mismatch(store):
    # Week 1: computed 125000 (the sale) but Tally's own YTD closing says
    # 124000 - a genuine, unresolved 1000 difference. Week 2 must open
    # from Tally's 124000, not this app's own 125000, so the gap doesn't
    # silently compound - see Store.get_latest_closing's docstring.
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    week1_voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"), voucher_date=date(2026, 4, 6))
    outcome1 = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [week1_voucher], {"A & B Transport": Decimal("124000.00")}
    )
    assert outcome1.failed_parties == ["A & B Transport"]

    week2_voucher = _sales_voucher("SB/0200", "A & B Transport", Decimal("10000.00"), voucher_date=date(2026, 4, 9))
    outcome2 = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 14), [week2_voucher], {"A & B Transport": Decimal("134000.00")}
    )
    # 124000 (Tally's week-1 closing) + 10000 (week-2 sale) = 134000 -
    # reconciles clean, confirming week 2 opened from 124000, not 125000.
    assert outcome2.failed_parties == []
    week2_row = store.weekly_snapshots_for_week(date(2026, 4, 14))[0]
    assert Decimal(week2_row["opening"]) == Decimal("124000.00")


def test_credit_note_in_the_same_run_resolves_against_this_runs_own_sales_row(store):
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    sales_voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))
    cn_voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE, voucher_date=date(2026, 4, 10), voucher_number="CN/01",
        branch_id="KOL", party_ledger_name="A & B Transport",
        entries=[
            LedgerEntry(party_ledger_name="A & B Transport", amount_as_extracted=Decimal("5000.00"),
                        bill_name="SB/0142", bill_type="Agst Ref"),
        ],
    )
    # Net closing must reflect the CN too: 125000 (sale) - 5000 (CN) = 120000.
    outcome = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 12), [sales_voucher, cn_voucher],
        {"A & B Transport": Decimal("120000.00")},
    )

    assert outcome.outcome == ExtractionOutcome.PASS
    cn_rows = store.all_credit_note_rows("KOL")
    assert len(cn_rows) == 1
    from ar_mis.models import RegisterClassification
    assert cn_rows[0].classification == RegisterClassification.CURRENT
    assert cn_rows[0].bill_allocation_reference == "SB/0142"


def test_voucher_with_no_party_ledger_name_is_flagged_not_dropped_silently(store):
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 6), voucher_number="SB/0999",
        branch_id="KOL", party_ledger_name="",
        entries=[LedgerEntry(party_ledger_name="", amount_as_extracted=Decimal("-1000.00"))],
    )
    # No party at all means nothing for reconciliation to check - pass an
    # empty closing_extracted so the run still completes as a PASS with
    # zero parties, exercising the register-build step in isolation.
    outcome = process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {})

    assert outcome.outcome == ExtractionOutcome.PASS
    assert outcome.register_build_exceptions == [
        ("SB/0999", "", "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")
    ]
    assert store.all_sales_dn_rows("KOL") == []


def test_sales_voucher_for_party_missing_from_customer_master_is_flagged(store):
    # A Sales voucher can reference a party that never showed up in the
    # Sundry Debtors YTD pull (e.g. a data-entry typo on the party name) -
    # process_branch_data only auto-creates customer_master for parties
    # IN that pull, so this party genuinely has no master record.
    voucher = _sales_voucher("SB/0142", "Ghost Party", Decimal("1000.00"))
    outcome = process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {})

    assert outcome.outcome == ExtractionOutcome.PASS
    assert outcome.register_build_exceptions == [("SB/0142", "Ghost Party", "No customer_master record for 'Ghost Party'")]
    assert store.all_sales_dn_rows("KOL") == []


def test_receipt_voucher_is_persisted_to_receipt_journal_register(store):
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    sales_voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))
    receipt_voucher = Voucher(
        voucher_type=VoucherType.RECEIPT, voucher_date=date(2026, 4, 10), voucher_number="RCPT/01",
        branch_id="KOL", party_ledger_name="A & B Transport",
        entries=[
            LedgerEntry(party_ledger_name="A & B Transport", amount_as_extracted=Decimal("50000.00"),
                        bill_name="SB/0142", bill_type="Agst Ref"),
        ],
    )
    outcome = process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 12), [sales_voucher, receipt_voucher],
        {"A & B Transport": Decimal("75000.00")},  # 125000 sale - 50000 receipt
    )

    assert outcome.outcome == ExtractionOutcome.PASS
    rj_rows = store.all_receipt_journal_rows("KOL")
    assert len(rj_rows) == 1
    assert rj_rows[0].target_doc_no == "SB/0142"
    assert rj_rows[0].amount == Decimal("50000.00")


def test_pass_run_records_extraction_timestamp_for_freshness_indicator(store):
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))

    assert store.last_extraction_at("KOL") is None
    process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport": Decimal("125000.00")},
        extracted_at=datetime(2026, 4, 8, 14, 30, 0),
    )
    assert store.last_extraction_at("KOL") == datetime(2026, 4, 8, 14, 30, 0)


def test_recon_fail_still_records_an_extraction_timestamp(store):
    # The extraction genuinely happened and data was written - the
    # freshness indicator must reflect that, even though this party
    # didn't reconcile.
    store.upsert_customer_master(CustomerMasterRecord("A & B Transport", "A & B Transport", "KOL", Decimal("0.00")))
    voucher = _sales_voucher("SB/0142", "A & B Transport", Decimal("125000.00"))

    process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport": Decimal("999999.00")},
        extracted_at=datetime(2026, 4, 8, 14, 30, 0),
    )
    assert store.last_extraction_at("KOL") == datetime(2026, 4, 8, 14, 30, 0)
