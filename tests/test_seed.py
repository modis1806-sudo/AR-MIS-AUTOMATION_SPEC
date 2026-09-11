from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.models import WeeklySnapshotRow
from ar_mis.seed import parse_seed_csv, seed
from ar_mis.storage import Store

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_parse_seed_csv_reads_valid_rows():
    records, errors = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    assert errors == []
    assert len(records) == 3
    acme = next(r for r in records if r.party_id == "ACME001")
    assert acme.pre_mis_outstanding == Decimal("325000.00")
    assert acme.branch_id == "KOL"


def test_parse_seed_csv_rejects_missing_columns(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("party_id,party_name\nP1,Acme\n")
    with pytest.raises(ValueError, match="missing required column"):
        parse_seed_csv(str(bad_csv))


def test_parse_seed_csv_collects_row_level_errors(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "party_id,party_name,branch_id,pre_mis_outstanding\n"
        "P1,Acme,KOL,not-a-number\n"
        ",Missing Id,KOL,100.00\n"
        "P2,Good Row,KOL,500.00\n"
    )
    records, errors = parse_seed_csv(str(bad_csv))
    assert len(records) == 1
    assert records[0].party_id == "P2"
    assert len(errors) == 2


def test_seed_loads_new_parties(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    records, _ = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    summary = seed(store, records, announce=lambda *_: None)
    assert summary.loaded == 3
    assert store.get_opening_balance("ACME001", "KOL") == Decimal("325000.00")
    store.close()


def test_seed_is_idempotent_for_unchanged_balances(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    records, _ = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    seed(store, records, announce=lambda *_: None)
    # Re-running against the same CSV must be a safe no-op, not an error.
    summary = seed(store, records, announce=lambda *_: None)
    assert summary.unchanged == 3
    assert summary.loaded == 0
    store.close()


def test_seed_refuses_to_overwrite_once_weekly_snapshots_exist(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    records, _ = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    seed(store, records, announce=lambda *_: None)

    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="ACME001", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("325000.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("325000.00"), closing_extracted=Decimal("325000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )

    # A revised CSV tries to correct ACME001's baseline after the fact.
    corrected = [r for r in records]
    corrected[0] = type(corrected[0])(
        party_id="ACME001", party_name="Acme Corp Pvt Ltd", branch_id="KOL",
        pre_mis_outstanding=Decimal("999999.00"),
    )
    summary = seed(store, corrected, force=True, announce=lambda *_: None)
    assert summary.overwritten == 0
    assert len(summary.skipped) == 1
    # The balance must be untouched - force is not enough once real data exists.
    assert store.get_opening_balance("ACME001", "KOL") == Decimal("325000.00")
    store.close()


def test_seed_allows_force_correction_before_any_weekly_snapshot(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    records, _ = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    seed(store, records, announce=lambda *_: None)

    corrected = list(records)
    corrected[0] = type(corrected[0])(
        party_id="ACME001", party_name="Acme Corp Pvt Ltd", branch_id="KOL",
        pre_mis_outstanding=Decimal("300000.00"),
    )

    # Without --force: skipped, not silently applied.
    summary_no_force = seed(store, corrected, force=False, announce=lambda *_: None)
    assert summary_no_force.overwritten == 0
    assert len(summary_no_force.skipped) == 1
    assert store.get_opening_balance("ACME001", "KOL") == Decimal("325000.00")

    # With --force, and no weekly data yet: allowed.
    summary_forced = seed(store, corrected, force=True, announce=lambda *_: None)
    assert summary_forced.overwritten == 1
    assert store.get_opening_balance("ACME001", "KOL") == Decimal("300000.00")
    store.close()


def test_dry_run_reports_without_writing(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    records, _ = parse_seed_csv(str(FIXTURES / "sample_pre_mis_outstanding.csv"))
    summary = seed(store, records, dry_run=True, announce=lambda *_: None)
    assert summary.loaded == 3
    # Nothing was actually written.
    assert store.customer_master_exists("ACME001", "KOL") is False
    store.close()
