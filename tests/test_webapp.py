"""Flask test-client coverage for the Branch Master and Test Extraction
pages. Test Extraction hits ar_mis.tally_client.TallyClient, which is
monkeypatched here the same way test_cli.py does - no live Tally is
reachable from this build environment (see README).
"""
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

from ar_mis.models import LedgerEntry, Voucher, VoucherType
from ar_mis.webapp.app import create_app


@pytest.fixture
def client(tmp_path):
    # Pre-selects the Maker role so every existing test here (written
    # before role gating existed) keeps exercising full access, exactly
    # as it did before - role-gating behavior itself (a Checker being
    # blocked, the picker, the switcher) has its own dedicated tests
    # below using a client that does NOT pre-select a role.
    app = create_app(db_path=str(tmp_path / "webapp.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["role"] = "maker"
        yield c


@pytest.fixture
def roleless_client(tmp_path):
    app = create_app(db_path=str(tmp_path / "webapp.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_home_page_loads_with_zero_branches(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"0 branch(es) configured" in resp.data


def test_add_branch_then_appears_in_list(client):
    resp = client.post(
        "/branches/new",
        data={
            "branch_id": "KOL",
            "branch_name": "Kolkata",
            "tally_company_name": "Kolkata HQ",
            "tally_host": "192.168.1.10",
            "tally_port": "9000",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Kolkata" in resp.data
    assert b"192.168.1.10" in resp.data

    home = client.get("/")
    assert b"1 branch(es) configured" in home.data


def test_add_branch_rejects_duplicate_branch_id(client):
    payload = {
        "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
        "tally_host": "localhost", "tally_port": "9000",
    }
    client.post("/branches/new", data=payload, follow_redirects=True)
    resp = client.post("/branches/new", data=payload, follow_redirects=True)
    assert b"already exists" in resp.data


def test_add_branch_rejects_missing_required_fields(client):
    resp = client.post(
        "/branches/new",
        data={"branch_id": "", "branch_name": "", "tally_company_name": "", "tally_host": "", "tally_port": ""},
    )
    assert b"are all required" in resp.data


def test_edit_branch_updates_fields(client):
    client.post(
        "/branches/new",
        data={"branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
              "tally_host": "localhost", "tally_port": "9000"},
    )
    resp = client.post(
        "/branches/KOL/edit",
        data={"branch_name": "Kolkata Renamed", "tally_company_name": "Kolkata HQ",
              "tally_host": "10.0.0.5", "tally_port": "9001"},
        follow_redirects=True,
    )
    assert b"Kolkata Renamed" in resp.data
    assert b"10.0.0.5" in resp.data


def test_edit_nonexistent_branch_redirects_with_error(client):
    resp = client.get("/branches/GHOST/edit", follow_redirects=True)
    assert b"No such branch" in resp.data


def test_delete_branch_removes_it(client):
    client.post(
        "/branches/new",
        data={"branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
              "tally_host": "localhost", "tally_port": "9000"},
    )
    resp = client.post("/branches/KOL/delete", follow_redirects=True)
    assert b"Branch removed" in resp.data
    assert b"No branches configured yet" in resp.data


def test_test_extraction_page_with_no_branches_prompts_to_add_one(client):
    resp = client.get("/test-extraction")
    assert b"Add one in Branch Master" in resp.data


# ---- Pre-MIS Outstanding CSV upload (Branch Master create/edit) ----------


def test_branch_creation_with_pre_mis_csv_loads_parties(client):
    csv_text = (
        "party_name,pre_mis_outstanding\n"
        "Acme Traders,125000.00\n"
        "Global Enterprises,-5000.00\n"
    )
    resp = client.post(
        "/branches/new",
        data={
            "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (BytesIO(csv_text.encode()), "pre_mis.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"2 loaded" in resp.data

    from ar_mis.storage import Store
    s = Store(client.application.config["DB_PATH"])
    # party_id is always the exact ledger name (Acme Traders) - never a
    # separately-typed code - since that's the only string the live
    # extraction will ever match against.
    assert s.get_opening_balance("Acme Traders", "KOL") == Decimal("125000.00")
    assert s.get_opening_balance("Global Enterprises", "KOL") == Decimal("-5000.00")
    s.close()


def test_branch_creation_without_pre_mis_csv_is_unaffected(client):
    resp = client.post(
        "/branches/new",
        data={
            "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Kolkata" in resp.data
    assert b"loaded" not in resp.data


def test_branch_edit_with_pre_mis_csv_loads_parties(client):
    client.post(
        "/branches/new",
        data={"branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
              "tally_host": "localhost", "tally_port": "9000"},
    )
    csv_text = "party_name,pre_mis_outstanding\nAcme Traders,125000.00\n"
    resp = client.post(
        "/branches/KOL/edit",
        data={
            "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (BytesIO(csv_text.encode()), "pre_mis.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"1 loaded" in resp.data


def test_pre_mis_csv_with_missing_columns_is_rejected_wholesale(client):
    resp = client.post(
        "/branches/new",
        data={
            "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (BytesIO(b"party_name\nAcme\n"), "bad.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"rejected" in resp.data
    assert b"missing required column" in resp.data
    # The branch itself must still be created - only the CSV load failed.
    assert b"Kolkata" in resp.data


def test_pre_mis_csv_with_bad_row_is_rejected_wholesale_nothing_loaded(client):
    csv_text = (
        "party_name,pre_mis_outstanding\n"
        "Acme,not-a-number\n"
        "Good Row,500.00\n"
    )
    resp = client.post(
        "/branches/new",
        data={
            "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (BytesIO(csv_text.encode()), "bad.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"rejected" in resp.data
    assert b"not a valid decimal amount" in resp.data

    from ar_mis.storage import Store
    s = Store(client.application.config["DB_PATH"])
    assert s.customer_master_exists("Good Row", "KOL") is False
    s.close()


def test_pre_mis_csv_corrects_a_party_auto_created_at_zero_by_an_earlier_extraction(client):
    # Mirrors a real failure mode: a Test & Save Extraction run before any
    # Pre-MIS CSV existed auto-creates every new party at a placeholder
    # 0.00 (pipeline.process_branch_data's own new-party fallback). A
    # later CSV upload correcting that placeholder must succeed outright
    # - a maker has no way to pass --force through a browser, so the
    # upload route must apply it for them (still gated by
    # has_weekly_snapshots, covered by the test below).
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.storage import Store

    client.post(
        "/branches/new",
        data={"branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
              "tally_host": "localhost", "tally_port": "9000"},
    )
    s = Store(client.application.config["DB_PATH"])
    s.upsert_customer_master(CustomerMasterRecord("Acme Traders", "Acme Traders", "KOL", Decimal("0.00")))
    s.close()

    resp = client.post(
        "/branches/KOL/edit",
        data={
            "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (
                BytesIO(b"party_name,pre_mis_outstanding\nAcme Traders,125000.00\n"),
                "pre_mis.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"1 corrected" in resp.data

    s = Store(client.application.config["DB_PATH"])
    assert s.get_opening_balance("Acme Traders", "KOL") == Decimal("125000.00")
    s.close()


def test_pre_mis_csv_skips_party_that_already_has_weekly_snapshots(client):
    from ar_mis.models import WeeklySnapshotRow
    from ar_mis.storage import Store

    client.post(
        "/branches/new",
        data={
            "branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (
                BytesIO(b"party_name,pre_mis_outstanding\nAcme Traders,125000.00\n"),
                "pre_mis.csv",
            ),
        },
        content_type="multipart/form-data",
    )

    s = Store(client.application.config["DB_PATH"])
    s.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="Acme Traders", branch_id="KOL", week_ending=date(2026, 1, 5),
            opening=Decimal("125000.00"), sales=Decimal("0.00"), credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"), receipts=Decimal("0.00"), journals=Decimal("0.00"),
            closing_computed=Decimal("125000.00"), closing_extracted=Decimal("125000.00"),
            reconciled=True, difference=Decimal("0.00"),
        )
    )
    s.close()

    resp = client.post(
        "/branches/KOL/edit",
        data={
            "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
            "tally_host": "localhost", "tally_port": "9000",
            "pre_mis_csv": (
                BytesIO(b"party_name,pre_mis_outstanding\nAcme Traders,999999.00\n"),
                "pre_mis.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"1 skipped" in resp.data

    s = Store(client.application.config["DB_PATH"])
    assert s.get_opening_balance("Acme Traders", "KOL") == Decimal("125000.00")
    s.close()


def test_pre_mis_template_download_route(client):
    resp = client.get("/branches/pre-mis-template.csv")
    assert resp.status_code == 200
    assert b"party_name,pre_mis_outstanding" in resp.data


def test_branches_list_shows_no_data_yet_before_any_extraction(client):
    _add_branch(client)
    resp = client.get("/branches")
    assert b"No data yet" in resp.data


def test_branches_list_shows_data_coverage_after_a_save(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientForCommit)
    _add_branch(client)
    client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    resp = client.get("/branches")
    assert b"2026-01-05" in resp.data and b"2026-01-11" in resp.data
    assert b"1 week" in resp.data
    assert b"Delete latest week" in resp.data


def test_branch_delete_week_removes_it_and_allows_reextraction(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientForCommit)
    _add_branch(client)
    client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    resp = client.post(
        "/branches/KOL/delete-week", data={"week_ending": _SINGLE_WEEK_END}, follow_redirects=True
    )
    assert b"Deleted the week ending" in resp.data
    assert b"No data yet" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert store.all_sales_dn_rows("KOL") == []
    assert store.week_endings_for_branch("KOL") == []
    store.close()

    # Re-extracting the same week now succeeds rather than being skipped.
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    assert b"RECONCILED CLEAN" in resp.data


def test_branch_delete_week_refuses_a_week_that_is_not_the_latest(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientForCommit)
    _add_branch(client)
    client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": "2026-01-05", "to_date": "2026-01-18"},
    )
    resp = client.post(
        "/branches/KOL/delete-week", data={"week_ending": "2026-01-11"}, follow_redirects=True
    )
    assert b"most recently recorded" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert store.week_endings_for_branch("KOL") == [date(2026, 1, 11), date(2026, 1, 18)]
    store.close()


def test_checker_cannot_reach_branch_delete_week(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.post(
        "/branches/KOL/delete-week", data={"week_ending": "2026-01-11"}, follow_redirects=True
    )
    assert b"available for your role" in resp.data


class FakeTallyClientOK:
    def __init__(self, branch, timeout_seconds=15.0):
        self.branch = branch

    def confirm_current_company(self):
        return None

    def fetch_all_voucher_types(self, from_date, to_date):
        voucher = Voucher(
            voucher_type=VoucherType.SALES, voucher_date=date(2026, 1, 3), voucher_number="SB/1",
            branch_id=self.branch.branch_id,
            entries=[LedgerEntry(party_ledger_name="Acme", amount_as_extracted=Decimal("-1000.00"))],
        )
        return {vt: ([voucher] if vt == VoucherType.SALES else []) for vt in VoucherType}

    def fetch_ytd_sundry_debtors(self, fy_start, as_of):
        return {"Acme": Decimal("-1000.00")}


class FakeTallyClientUnreachable:
    def __init__(self, branch, timeout_seconds=15.0):
        self.branch = branch

    def confirm_current_company(self):
        from ar_mis.tally_client import TallyConnectionError

        raise TallyConnectionError(f"Could not reach Tally at {self.branch.gateway_url}")


def _add_branch(client):
    client.post(
        "/branches/new",
        data={"branch_id": "KOL", "branch_name": "Kolkata", "tally_company_name": "Kolkata HQ",
              "tally_host": "localhost", "tally_port": "9000"},
    )


def test_test_extraction_reports_all_passed(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientOK)
    _add_branch(client)
    resp = client.post(
        "/test-extraction",
        data={"branch_id": "KOL", "from_date": "2026-01-01", "to_date": "2026-01-07"},
    )
    assert b"ALL PASSED" in resp.data
    assert b"Company check" in resp.data
    assert b"Voucher extraction" in resp.data
    assert b"Sundry Debtors pull" in resp.data
    assert b"2026-01-01" in resp.data and b"2026-01-07" in resp.data


def test_test_extraction_accepts_a_full_year_range(client, monkeypatch):
    # No artificial min/max on the range - a first-time backfill needs a
    # full financial year, not just a week.
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientOK)
    _add_branch(client)
    resp = client.post(
        "/test-extraction",
        data={"branch_id": "KOL", "from_date": "2025-04-01", "to_date": "2026-03-31"},
    )
    assert b"ALL PASSED" in resp.data
    assert b"2025-04-01" in resp.data and b"2026-03-31" in resp.data


def test_test_extraction_rejects_from_date_after_to_date(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientOK)
    _add_branch(client)
    resp = client.post(
        "/test-extraction",
        data={"branch_id": "KOL", "from_date": "2026-01-10", "to_date": "2026-01-01"},
        follow_redirects=True,
    )
    assert b"From Date must be on or before To Date" in resp.data


def test_test_extraction_reports_connection_failure_clearly(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientUnreachable)
    _add_branch(client)
    resp = client.post(
        "/test-extraction",
        data={"branch_id": "KOL", "from_date": "2026-01-01", "to_date": "2026-01-07"},
    )
    assert b"FAILED" in resp.data
    assert b"Could not reach Tally" in resp.data
    # A connection failure must not go on to attempt voucher extraction.
    assert b"Voucher extraction" not in resp.data


# ---- Test & Save Extraction: the save half ------------------------------
# ---- (POST /test-extraction/save, only ever reached from a successful ---
# ---- Test Extraction result on the same page) ----------------------------

# A 7-day span - exactly one ar_mis.config.split_into_chunks piece at
# the default chunk_days=7, so these tests exercise a single run without
# needing a separate multi-chunk test (that's covered in test_config.py
# and the dedicated multi-chunk test below).
_SINGLE_WEEK_START = "2026-01-05"
_SINGLE_WEEK_END = "2026-01-11"


class FakeTallyClientForCommit:
    """Unlike FakeTallyClientOK above (diagnostic-only, never actually
    parsed into registers), this voucher sets party_ledger_name at the
    VOUCHER level too - required for build_sales_dn_register_row to
    attribute it to a customer, per Voucher.party_ledger_name's own
    docstring. Ignores the requested date range and always returns the
    same voucher - fine here since these tests exercise the save/skip/
    fail plumbing, not date filtering (that's parser-level, tested
    elsewhere).
    """

    def __init__(self, branch, timeout_seconds=15.0):
        self.branch = branch

    def confirm_current_company(self):
        return None

    def fetch_all_voucher_types(self, from_date, to_date):
        voucher = Voucher(
            voucher_type=VoucherType.SALES, voucher_date=date(2026, 1, 5), voucher_number="SB/1",
            branch_id=self.branch.branch_id, party_ledger_name="Acme",
            entries=[
                LedgerEntry(party_ledger_name="Acme", amount_as_extracted=Decimal("-1000.00"), bill_name="SB/1", bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("1000.00")),
            ],
        )
        return {vt: ([voucher] if vt == VoucherType.SALES else []) for vt in VoucherType}

    def fetch_ytd_sundry_debtors(self, fy_start, as_of):
        return {"Acme": Decimal("-1000.00")}


class FakeTallyClientMismatch(FakeTallyClientForCommit):
    """Same voucher stream, but the YTD closing balance disagrees with
    the roll-forward - the reconciliation-mismatch case that must now
    still write data, just flagged as not clean.
    """

    def fetch_ytd_sundry_debtors(self, fy_start, as_of):
        return {"Acme": Decimal("-999.00")}


def _run_test_extraction(client, from_date, to_date):
    return client.post(
        "/test-extraction",
        data={"branch_id": "KOL", "from_date": from_date, "to_date": to_date},
    )


def test_test_extraction_save_writes_data_and_shows_reconciled_clean(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientForCommit)
    _add_branch(client)
    _run_test_extraction(client, _SINGLE_WEEK_START, _SINGLE_WEEK_START)
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    assert resp.status_code == 200
    assert b"RECONCILED CLEAN" in resp.data
    assert b"No YTD drift found" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert len(store.weekly_snapshots_for_week(date(2026, 1, 11))) == 1
    assert len(store.all_sales_dn_rows("KOL")) == 1
    store.close()


def test_test_extraction_save_mismatch_still_writes_data(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientMismatch)
    _add_branch(client)
    _run_test_extraction(client, _SINGLE_WEEK_START, _SINGLE_WEEK_START)
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    assert b"RECONCILIATION MISMATCH" in resp.data
    assert b"still recorded" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert len(store.weekly_snapshots_for_week(date(2026, 1, 11))) == 1
    store.close()


def test_test_extraction_save_skips_a_week_already_recorded(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientForCommit)
    _add_branch(client)
    _run_test_extraction(client, _SINGLE_WEEK_START, _SINGLE_WEEK_START)
    client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    assert b"SKIPPED" in resp.data
    assert b"already has recorded data" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert len(store.weekly_snapshots_for_week(date(2026, 1, 11))) == 1
    store.close()


def test_test_extraction_save_reports_connection_failure_and_writes_nothing(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientUnreachable)
    _add_branch(client)
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
    )
    assert b"FAILED" in resp.data
    assert b"Could not reach Tally" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert store.weekly_snapshots_for_week(date(2026, 1, 11)) == []
    store.close()


def test_test_extraction_save_stops_at_first_failed_week_in_a_multi_week_range(client, monkeypatch):
    # A week request spanning 2026-01-05..2026-01-18 covers two Monday-
    # Sunday weeks. FakeTallyClientFailsSecondWeek fails only on the
    # second one - the run must save the first week, then stop rather
    # than guess an opening balance for anything past the failure.
    class FakeTallyClientFailsSecondWeek(FakeTallyClientForCommit):
        calls = 0

        def confirm_current_company(self):
            type(self).calls += 1
            if type(self).calls > 1:
                from ar_mis.tally_client import TallyConnectionError

                raise TallyConnectionError("Could not reach Tally")

    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientFailsSecondWeek)
    _add_branch(client)
    resp = client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": "2026-01-05", "to_date": "2026-01-18"},
    )
    assert b"RECONCILED CLEAN" in resp.data  # first week
    assert b"FAILED" in resp.data  # second week
    assert b"was not attempted" in resp.data

    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert len(store.weekly_snapshots_for_week(date(2026, 1, 11))) == 1
    assert store.weekly_snapshots_for_week(date(2026, 1, 18)) == []
    store.close()


def test_checker_cannot_reach_test_extraction_save(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.post(
        "/test-extraction/save",
        data={"branch_id": "KOL", "from_date": _SINGLE_WEEK_START, "to_date": _SINGLE_WEEK_END},
        follow_redirects=True,
    )
    assert b"available for your role" in resp.data


def _seed_customer(client, party_id="P1", party_name="Acme", branch_id="KOL", pre_mis="50000.00"):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord(party_id, party_name, branch_id, Decimal(pre_mis)))
    store.close()


def test_customers_list_shows_never_reconciled_parties(client):
    _seed_customer(client)
    resp = client.get("/customers")
    assert b"Acme" in resp.data
    assert b"Never" in resp.data


def test_mark_reconciled_updates_the_list(client):
    _seed_customer(client)
    resp = client.post(
        "/customers/reconcile",
        data={"party_id": "P1", "branch_id": "KOL", "reconciled_by": "Priya"},
        follow_redirects=True,
    )
    assert b"Marked reconciled by Priya" in resp.data
    assert b"Priya" in resp.data
    assert date.today().isoformat().encode() in resp.data


def test_mark_reconciled_requires_a_name(client):
    _seed_customer(client)
    resp = client.post(
        "/customers/reconcile",
        data={"party_id": "P1", "branch_id": "KOL", "reconciled_by": ""},
        follow_redirects=True,
    )
    assert b"Enter who is confirming this reconciliation" in resp.data
    assert b"Never" in resp.data  # unchanged


def test_customers_list_puts_never_reconciled_before_reconciled(client):
    _seed_customer(client, party_id="P1", party_name="Already Reconciled", branch_id="KOL")
    _seed_customer(client, party_id="P2", party_name="Needs Review", branch_id="KOL")
    client.post("/customers/reconcile", data={"party_id": "P1", "branch_id": "KOL", "reconciled_by": "Priya"})
    resp = client.get("/customers")
    text = resp.data.decode()
    assert text.index("Needs Review") < text.index("Already Reconciled")


class FakeTallyClientDiscover:
    def __init__(self, branch, timeout_seconds=15.0):
        self.branch = branch

    def list_open_companies(self):
        return ["SHAIMARINE CONTAINER LINE PRIVATE LIMITED", "SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)"]


class FakeTallyClientDiscoverUnreachable:
    def __init__(self, branch, timeout_seconds=15.0):
        self.branch = branch

    def list_open_companies(self):
        from ar_mis.tally_client import TallyConnectionError

        raise TallyConnectionError(f"Could not reach Tally at {self.branch.gateway_url}")


def test_discover_companies_lists_open_companies_with_use_links(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientDiscover)
    resp = client.post("/discover", data={"tally_host": "localhost", "tally_port": "9000"})
    assert b"SHAIMARINE CONTAINER LINE PRIVATE LIMITED" in resp.data
    assert b"SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)" in resp.data
    assert b"Use this company" in resp.data


def test_discover_companies_reports_connection_failure(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientDiscoverUnreachable)
    resp = client.post("/discover", data={"tally_host": "localhost", "tally_port": "9000"})
    assert b"Could not reach Tally" in resp.data


def test_use_this_company_prefills_add_branch_form(client):
    resp = client.get(
        "/branches/new?tally_company_name=SHAIMARINE+CONTAINER+LINE+PRIVATE+LIMITED"
        "&tally_host=localhost&tally_port=9000"
    )
    assert b"SHAIMARINE CONTAINER LINE PRIVATE LIMITED" in resp.data
    assert b"Pre-filled from Discover Companies" in resp.data
    # Branch ID/Name must stay blank and editable - only the Tally-side
    # fields come from discovery, a human still names the branch.
    assert b'name="branch_id" value=""' in resp.data


# ---- Manual Upload ----------------------------------------------------

MANUAL_UPLOAD_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _add_manual_upload_branch(client):
    from ar_mis.config import BranchConfig
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_branch(BranchConfig("KOL", "Kolkata", "A & B Transport Pvt Ltd Co"))
    store.close()


def _seed_manual_upload_openings(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport Pvt Ltd", "A & B Transport Pvt Ltd", "KOL", Decimal("300000.00"))
    )
    store.upsert_customer_master(
        CustomerMasterRecord("Reliable Cargo Movers", "Reliable Cargo Movers", "KOL", Decimal("62500.00"))
    )
    store.close()


def test_manual_upload_page_with_no_branches_prompts_to_add_one(client):
    resp = client.get("/manual-upload")
    assert b"Add one in Branch Master" in resp.data


def test_manual_upload_requires_trial_balance_file(client):
    _add_manual_upload_branch(client)
    sales_xml = (MANUAL_UPLOAD_FIXTURES / "voucher_collection_sales.xml").read_bytes()
    resp = client.post(
        "/manual-upload",
        data={
            "branch_id": "KOL", "from_date": "2026-04-01", "to_date": "2026-04-07",
            "voucher_Sales": (BytesIO(sales_xml), "sales.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Trial Balance" in resp.data and b"required" in resp.data


def test_manual_upload_clean_run_shows_reconciled_clean(client):
    _add_manual_upload_branch(client)
    _seed_manual_upload_openings(client)
    sales_xml = (MANUAL_UPLOAD_FIXTURES / "voucher_collection_sales.xml").read_bytes()
    tb_xml = (MANUAL_UPLOAD_FIXTURES / "ledger_closing_balances.xml").read_bytes()

    resp = client.post(
        "/manual-upload",
        data={
            "branch_id": "KOL", "from_date": "2026-04-01", "to_date": "2026-04-07",
            "voucher_Sales": (BytesIO(sales_xml), "sales.xml"),
            "trial_balance": (BytesIO(tb_xml), "tb.xml"),
        },
        content_type="multipart/form-data",
    )
    assert b"RECONCILED CLEAN" in resp.data


def test_manual_upload_mismatch_still_writes_data_and_shows_mismatch_badge(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.storage import Store

    _add_manual_upload_branch(client)
    # Deliberately wrong opening balance (999999 instead of the real
    # 300000) - reconciliation will genuinely fail for this party.
    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport Pvt Ltd", "A & B Transport Pvt Ltd", "KOL", Decimal("999999.00"))
    )
    store.upsert_customer_master(
        CustomerMasterRecord("Reliable Cargo Movers", "Reliable Cargo Movers", "KOL", Decimal("62500.00"))
    )
    store.close()

    sales_xml = (MANUAL_UPLOAD_FIXTURES / "voucher_collection_sales.xml").read_bytes()
    tb_xml = (MANUAL_UPLOAD_FIXTURES / "ledger_closing_balances.xml").read_bytes()

    resp = client.post(
        "/manual-upload",
        data={
            "branch_id": "KOL", "from_date": "2026-04-01", "to_date": "2026-04-07",
            "voucher_Sales": (BytesIO(sales_xml), "sales.xml"),
            "trial_balance": (BytesIO(tb_xml), "tb.xml"),
        },
        content_type="multipart/form-data",
    )
    # Per the client's explicit instruction, this is no longer a refusal -
    # data is recorded for BOTH parties, the mismatch is just flagged.
    assert b"RECONCILIATION MISMATCH" in resp.data
    assert b"RECONCILED CLEAN" not in resp.data
    assert b"still recorded" in resp.data

    store = Store(client.application.config["DB_PATH"])
    assert len(store.weekly_snapshots_for_week(date(2026, 4, 7))) == 2
    store.close()


def test_manual_upload_shows_register_build_exceptions_for_non_debtor_journal(client):
    # A Journal voucher between a creditor and a bank loan account -
    # nothing to do with any tracked Sundry Debtor. Reconciliation must
    # still show clean (this voucher never touches AR), but the register
    # exclusion must be visible on the result page, not silent.
    _add_manual_upload_branch(client)
    _seed_manual_upload_openings(client)
    sales_xml = (MANUAL_UPLOAD_FIXTURES / "voucher_collection_sales.xml").read_bytes()
    tb_xml = (MANUAL_UPLOAD_FIXTURES / "ledger_closing_balances.xml").read_bytes()
    journal_xml = b"""<ENVELOPE>
 <VOUCHER>
  <DATE>20260403</DATE>
  <VOUCHERNUMBER>JV/9001</VOUCHERNUMBER>
  <VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
  <PARTYLEDGERNAME>Some Creditor Pvt Ltd</PARTYLEDGERNAME>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Some Creditor Pvt Ltd</LEDGERNAME>
   <AMOUNT>-50000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Bank Loan Account</LEDGERNAME>
   <AMOUNT>50000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
 </VOUCHER>
</ENVELOPE>"""

    resp = client.post(
        "/manual-upload",
        data={
            "branch_id": "KOL", "from_date": "2026-04-01", "to_date": "2026-04-07",
            "voucher_Sales": (BytesIO(sales_xml), "sales.xml"),
            "voucher_Journal": (BytesIO(journal_xml), "journal.xml"),
            "trial_balance": (BytesIO(tb_xml), "tb.xml"),
        },
        content_type="multipart/form-data",
    )
    assert b"RECONCILED CLEAN" in resp.data
    assert b"JV/9001" in resp.data
    assert b"No leg of this voucher touches a tracked Sundry Debtor" in resp.data


def test_manual_upload_refuses_when_already_recorded(client):
    _add_manual_upload_branch(client)
    _seed_manual_upload_openings(client)
    sales_xml = (MANUAL_UPLOAD_FIXTURES / "voucher_collection_sales.xml").read_bytes()
    tb_xml = (MANUAL_UPLOAD_FIXTURES / "ledger_closing_balances.xml").read_bytes()

    def _post_kwargs():
        return dict(
            data={
                "branch_id": "KOL", "from_date": "2026-04-01", "to_date": "2026-04-07",
                "voucher_Sales": (BytesIO(sales_xml), "sales.xml"),
                "trial_balance": (BytesIO(tb_xml), "tb.xml"),
            },
            content_type="multipart/form-data",
        )

    client.post("/manual-upload", **_post_kwargs())
    resp = client.post("/manual-upload", **_post_kwargs())
    assert b"NOT PROCESSED" in resp.data
    assert b"already has recorded data" in resp.data


# ---- Role gating (placeholder access control) ----------------------------


def test_roleless_visitor_is_redirected_to_choose_role(roleless_client):
    resp = roleless_client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Who&#39;s using this?" in resp.data or b"Who's using this?" in resp.data


def test_choosing_maker_grants_extraction_access(roleless_client):
    resp = roleless_client.post("/choose-role", data={"role": "maker"}, follow_redirects=True)
    assert resp.status_code == 200
    resp = roleless_client.get("/branches")
    assert resp.status_code == 200


def test_choosing_checker_blocks_extraction_routes(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    for path in ("/branches", "/test-extraction", "/discover", "/manual-upload"):
        resp = roleless_client.get(path, follow_redirects=True)
        assert resp.status_code == 200
        assert b"isn&#39;t available for your role" in resp.data or b"isn't available for your role" in resp.data


def test_checker_can_reach_registers_and_reports(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    for path in ("/customers", "/registers/sales-dn", "/registers/credit-notes", "/registers/receipts-journals", "/reports"):
        resp = roleless_client.get(path)
        assert resp.status_code == 200

    # The nav itself must not even offer the Extraction links to a Checker.
    resp = roleless_client.get("/")
    assert b"Branch Master" not in resp.data
    assert b"Test Extraction" not in resp.data


def test_checker_cannot_mark_a_customer_reconciled(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.post(
        "/customers/reconcile", data={"party_id": "P1", "branch_id": "KOL", "reconciled_by": "CFO"},
        follow_redirects=True,
    )
    assert b"isn&#39;t available for your role" in resp.data or b"isn't available for your role" in resp.data


def test_maker_sees_extraction_links_in_nav(client):
    resp = client.get("/")
    assert b"Branch Master" in resp.data
    assert b"Test Extraction" in resp.data


# ---- Registers pages -------------------------------------------------

REGISTERS_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _run_a_real_extraction(client):
    """Seeds a customer and runs process_branch_data directly against the
    live app's own database - the same path a real Test Extraction/CLI
    run would take - so the Registers pages have real rows to show.
    """
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(
        CustomerMasterRecord("A & B Transport Pvt Ltd", "A & B Transport Pvt Ltd", "KOL", Decimal("0.00"))
    )
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 6), voucher_number="SB/0142",
        branch_id="KOL", party_ledger_name="A & B Transport Pvt Ltd",
        entries=[
            LedgerEntry(party_ledger_name="A & B Transport Pvt Ltd", amount_as_extracted=Decimal("-125000.00"),
                        bill_name="SB/0142", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("125000.00")),
        ],
    )
    process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"A & B Transport Pvt Ltd": Decimal("125000.00")},
    )
    store.close()


def test_sales_dn_register_shows_no_data_message_when_empty(client):
    resp = client.get("/registers/sales-dn")
    assert resp.status_code == 200
    assert b"No invoices on record yet" in resp.data
    assert b"No data extracted yet" in resp.data


def test_sales_dn_register_shows_a_real_extracted_invoice(client):
    _run_a_real_extraction(client)
    resp = client.get("/registers/sales-dn")
    assert resp.status_code == 200
    assert b"SB/0142" in resp.data
    assert b"A &amp; B Transport Pvt Ltd" in resp.data or b"A & B Transport Pvt Ltd" in resp.data
    assert b"Data last extracted" in resp.data


def test_sales_dn_register_respects_as_of_date_query_param(client):
    _run_a_real_extraction(client)
    # As-of a date before the invoice's due date - open, not overdue.
    resp = client.get("/registers/sales-dn?as_of=2026-04-10")
    assert resp.status_code == 200
    assert b"SB/0142" in resp.data


def test_sales_dn_register_shows_round_off_column(client):
    _run_a_real_extraction(client)
    resp = client.get("/registers/sales-dn")
    assert resp.status_code == 200
    assert b"Round Off" in resp.data


def test_credit_note_register_and_receipt_journal_register_render_when_empty(client):
    for path in ("/registers/credit-notes", "/registers/receipts-journals"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert b"No " in resp.data  # the empty-state card text


# ---- Register snapshot summaries (client's explicit ask) -----------------


def test_sales_dn_register_shows_a_running_summary_of_open_and_overdue(client):
    _run_a_real_extraction(client)
    # As of a date past the invoice's due date (2026-04-6 + 30 days), the
    # invoice is both open and overdue - both totals should reflect it.
    resp = client.get("/registers/sales-dn?as_of=2026-06-01")
    assert resp.status_code == 200
    assert b"Total Invoice Value" in resp.data
    assert b"1,25,000.00" in resp.data  # Indian grouping, confirms the real amount flowed through
    assert b"1 overdue invoice" in resp.data


def test_credit_note_register_has_an_as_of_date_and_running_summary(client):
    resp = client.get("/registers/credit-notes")
    assert resp.status_code == 200
    assert b'name="as_of"' in resp.data
    assert b"Total CN Amount" in resp.data
    assert b"Total Unapplied CN Amount" in resp.data
    assert b"Pending Review" in resp.data


def test_credit_note_register_as_of_date_excludes_a_future_dated_cn_from_the_summary(client):
    from ar_mis.models import CustomerMasterRecord, CreditNoteRegisterRow
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("ACME", "ACME", "KOL", Decimal("0.00")))
    store.append_credit_note_row(
        CreditNoteRegisterRow(
            branch_id="KOL", cn_date=date(2026, 5, 1), voucher_number="CN/1", party_id="ACME",
            cn_amount=Decimal("200.00"), bill_allocation_reference=None,
        )
    )
    store.close()

    # As of a date before the CN was even issued, it must not count yet.
    resp = client.get("/registers/credit-notes?as_of=2026-04-01")
    assert b"CN/1" in resp.data  # still listed - never hidden
    assert b"200.00" not in resp.data.split(b"Total CN Amount")[1].split(b"</table")[0][:200]

    resp2 = client.get("/registers/credit-notes?as_of=2026-05-01")
    assert b"200.00" in resp2.data


def test_receipt_journal_register_shows_a_running_summary_of_unapplied_balance(client):
    from ar_mis.models import CustomerMasterRecord, ReceiptJournalRegisterRow
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("ACME", "ACME", "KOL", Decimal("0.00")))
    store.append_receipt_journal_rows(
        [
            ReceiptJournalRegisterRow(
                branch_id="KOL", txn_date=date(2026, 4, 4), voucher_type="Receipt", voucher_number="RCPT/1",
                party_id="ACME", amount=Decimal("500.00"), target_doc_no=None,
            )
        ]
    )
    store.close()

    resp = client.get("/registers/receipts-journals?as_of=2026-04-10")
    assert resp.status_code == 200
    assert b"Total Unapplied Balance" in resp.data
    assert b"1 unapplied case" in resp.data


# ---- Excel export -----------------------------------------------------

_XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_sales_dn_register_export_downloads_a_workbook(client):
    _run_a_real_extraction(client)
    resp = client.get("/registers/sales-dn/export.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype == _XLSX_MIMETYPE
    assert "Sales_DN_Register" in resp.headers["Content-Disposition"]
    assert len(resp.data) > 0


def test_credit_note_register_export_downloads_a_workbook(client):
    resp = client.get("/registers/credit-notes/export.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype == _XLSX_MIMETYPE
    assert "Credit_Note_Register" in resp.headers["Content-Disposition"]


def test_receipt_journal_register_export_downloads_a_workbook(client):
    resp = client.get("/registers/receipts-journals/export.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype == _XLSX_MIMETYPE
    assert "Receipt_Journal_Register" in resp.headers["Content-Disposition"]


def test_checker_can_export_registers_to_excel(roleless_client):
    # Export is a Registers-level, read-only action - a Checker must be
    # able to reach it exactly like the on-screen register itself.
    roleless_client.post("/choose-role", data={"role": "checker"})
    for path in (
        "/registers/sales-dn/export.xlsx",
        "/registers/credit-notes/export.xlsx",
        "/registers/receipts-journals/export.xlsx",
    ):
        resp = roleless_client.get(path)
        assert resp.status_code == 200
        assert resp.mimetype == _XLSX_MIMETYPE


# ---- Reports: AR Snapshot and Branch Ageing Schedule ----------------------


def test_ar_snapshot_renders_with_no_data(client):
    resp = client.get("/reports/ar-snapshot")
    assert resp.status_code == 200
    assert b"AR Snapshot" in resp.data
    assert b"No data extracted yet" in resp.data


def test_ar_snapshot_shows_real_figures_after_an_extraction(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/ar-snapshot?as_of=2026-09-12")
    assert resp.status_code == 200
    assert b"1,25,000.00" in resp.data  # Total AR from the one seeded invoice, Indian-grouped


def test_ar_snapshot_reachable_by_checker_not_by_extraction_routes(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/ar-snapshot")
    assert resp.status_code == 200


def test_branch_ageing_report_renders_with_no_data(client):
    resp = client.get("/reports/branch-ageing")
    assert resp.status_code == 200
    assert b"Branch-wise Ageing Schedule" in resp.data


def test_branch_ageing_report_shows_the_extracted_branch(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/branch-ageing?as_of=2026-09-12")
    assert resp.status_code == 200
    assert b"KOL" in resp.data
    assert b"All Branches" in resp.data


def test_reports_home_links_to_both_new_reports(client):
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert b"AR Snapshot" in resp.data
    assert b"Branch-wise Ageing Schedule" in resp.data


# ---- Reports: Exception Register ------------------------------------------


def test_exception_register_renders_with_no_data(client):
    resp = client.get("/reports/exceptions")
    assert resp.status_code == 200
    assert b"Exception Register" in resp.data
    assert b"Nothing unapplied" in resp.data


def test_exception_register_shows_unapplied_cash_after_a_real_extraction(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("ACME", "Acme Corp", "KOL", Decimal("0.00")))
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 6), voucher_number="SB/0142",
        branch_id="KOL", party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-125000.00"), bill_name="SB/0142", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("125000.00")),
        ],
    )
    receipt = Voucher(
        voucher_type=VoucherType.RECEIPT, voucher_date=date(2026, 4, 10), voucher_number="RCPT/01",
        branch_id="KOL", party_ledger_name="ACME",
        entries=[LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("5000.00"), bill_name="RCPT/01", bill_type="New Ref")],
    )
    process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 7), [voucher, receipt], {"ACME": Decimal("120000.00")},
    )
    store.close()

    resp = client.get("/reports/exceptions?as_of=2026-04-15")
    assert resp.status_code == 200
    assert b"ACME" in resp.data
    assert b"5,000.00" in resp.data


def test_exception_register_reachable_by_checker(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/exceptions")
    assert resp.status_code == 200


def test_reports_home_links_to_exception_register(client):
    resp = client.get("/reports")
    assert b"Exception Register" in resp.data


# ---- Reports: Ageing Matrix ------------------------------------------------


def test_ageing_matrix_renders_with_no_data(client):
    resp = client.get("/reports/ageing-matrix")
    assert resp.status_code == 200
    assert b"Ageing Matrix" in resp.data
    assert b"No parties found" in resp.data


def test_ageing_matrix_shows_extracted_party_and_branch_total(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/ageing-matrix?as_of=2026-09-12")
    assert resp.status_code == 200
    assert b"A &amp; B Transport Pvt Ltd" in resp.data or b"A & B Transport Pvt Ltd" in resp.data
    assert b"KOL" in resp.data
    assert b"All Branches" in resp.data


def test_ageing_matrix_shows_tally_cross_check_difference_after_extraction(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/ageing-matrix?as_of=2026-09-12")
    assert resp.status_code == 200
    # process_branch_data reconciled exactly (125000.00 extracted == computed),
    # so the cross-check difference must show as zero, not blank/omitted.
    assert b"1,25,000.00" in resp.data


def test_ageing_matrix_fy_filter_narrows_customer_rows(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    _run_a_real_extraction(client)  # FY 2026-27 party: "A & B Transport Pvt Ltd"

    # Add a second party, invoiced entirely within FY 2025-26.
    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("BETA", "Beta Logistics", "KOL", Decimal("0.00")))
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 1, 6), voucher_number="SB/0200",
        branch_id="KOL", party_ledger_name="BETA",
        entries=[
            LedgerEntry(party_ledger_name="BETA", amount_as_extracted=Decimal("-50000.00"), bill_name="SB/0200", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("50000.00")),
        ],
    )
    process_branch_data(store, "KOL", "Kolkata", date(2026, 1, 7), [voucher], {"BETA": Decimal("50000.00")})
    store.close()

    resp_fy26 = client.get("/reports/ageing-matrix?as_of=2026-09-12&fy=2025-26")
    assert resp_fy26.status_code == 200
    assert b"BETA" in resp_fy26.data
    assert b"A &amp; B Transport" not in resp_fy26.data and b"A & B Transport" not in resp_fy26.data

    resp_fy27 = client.get("/reports/ageing-matrix?as_of=2026-09-12&fy=2026-27")
    assert resp_fy27.status_code == 200
    assert b"BETA" not in resp_fy27.data


def test_ageing_matrix_reachable_by_checker(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/ageing-matrix")
    assert resp.status_code == 200


def test_reports_home_links_to_ageing_matrix(client):
    resp = client.get("/reports")
    assert b"Ageing Matrix" in resp.data


# ---- Reports: Weekly Movement Register -------------------------------------


def test_weekly_movement_renders_with_no_data(client):
    resp = client.get("/reports/weekly-movement")
    assert resp.status_code == 200
    assert b"Weekly Movement Register" in resp.data
    assert b"No weeks recorded yet" in resp.data


def test_maker_can_record_a_week(client):
    _run_a_real_extraction(client)
    resp = client.post("/reports/weekly-movement/record", data={"week_ending": "2026-09-12"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Recorded the position as of 2026-09-12" in resp.data
    assert b"2026-09-12" in resp.data


def test_recording_the_same_week_twice_is_refused(client):
    _run_a_real_extraction(client)
    client.post("/reports/weekly-movement/record", data={"week_ending": "2026-09-12"})
    resp = client.post("/reports/weekly-movement/record", data={"week_ending": "2026-09-12"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"already been recorded" in resp.data
    # Still only one row - the refused attempt must not have appended a duplicate.
    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    assert len(store.all_weekly_movement_rows()) == 1
    store.close()


def test_checker_cannot_record_a_week(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.post(
        "/reports/weekly-movement/record", data={"week_ending": "2026-09-12"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"available for your role" in resp.data


def test_weekly_movement_reachable_by_checker(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/weekly-movement")
    assert resp.status_code == 200


def test_checker_does_not_see_record_form(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/weekly-movement")
    assert b"Record this week's position" not in resp.data


def test_reports_home_links_to_weekly_movement(client):
    resp = client.get("/reports")
    assert b"Weekly Movement Register" in resp.data


# ---- Reports: TB Reconciliation Cross-Check --------------------------------


def test_tb_cross_check_renders_with_no_data(client):
    resp = client.get("/reports/tb-cross-check")
    assert resp.status_code == 200
    assert b"TB Reconciliation Cross-Check" in resp.data
    assert b"No weeks recorded yet" in resp.data


def test_tb_cross_check_shows_clean_reconciliation(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/tb-cross-check")
    assert resp.status_code == 200
    assert b"A &amp; B Transport" in resp.data or b"A & B Transport" in resp.data
    assert b"1,25,000.00" in resp.data


def test_tb_cross_check_shows_mismatch_and_summary_counts(client, monkeypatch):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("ACME", "Acme Corp", "KOL", Decimal("0.00")))
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 6), voucher_number="SB/0142",
        branch_id="KOL", party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-125000.00"), bill_name="SB/0142", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("125000.00")),
        ],
    )
    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 7), [voucher], {"ACME": Decimal("124000.00")})
    store.close()

    resp = client.get("/reports/tb-cross-check")
    assert resp.status_code == 200
    assert b"1,000.00" in resp.data  # the difference, shown in full
    # Three KPI tiles: Total Debtor as per Books, 1 party currently
    # mismatched, total difference 1000.00.
    assert resp.data.count(b"kpi-value") >= 3


def test_tb_cross_check_shows_total_debtor_as_per_books(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("P2", "P2", "KOL", Decimal("0.00")))
    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 5), [], {"P1": Decimal("-1000.00"), "P2": Decimal("-500.00")})
    store.close()

    resp = client.get("/reports/tb-cross-check")
    assert resp.status_code == 200
    assert b"Total Debtor as per Books" in resp.data
    assert b"1,500.00" in resp.data


def test_tb_cross_check_date_range_scopes_the_table_but_never_shrinks_the_running_total(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("0.00")))
    process_branch_data(store, "KOL", "Kolkata", date(2026, 1, 5), [], {"P1": Decimal("-1000.00")})
    store.close()

    # A narrow later range that doesn't include week ending 2026-01-05 -
    # the row must not appear in the table, but the party's last known
    # balance must still count toward Total Debtor as per Books.
    resp = client.get("/reports/tb-cross-check?from_date=2026-06-01&to_date=2026-06-30")
    assert resp.status_code == 200
    assert b"2026-01-05" not in resp.data
    assert b"1,000.00" in resp.data  # still in the Total Debtor tile


def test_tb_cross_check_as_of_excludes_a_week_recorded_after_the_chosen_to_date(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("0.00")))
    process_branch_data(store, "KOL", "Kolkata", date(2026, 1, 5), [], {"P1": Decimal("-1000.00")})
    process_branch_data(store, "KOL", "Kolkata", date(2026, 1, 19), [], {"P1": Decimal("-1200.00")})
    store.close()

    resp = client.get("/reports/tb-cross-check?from_date=2026-01-01&to_date=2026-01-12")
    assert resp.status_code == 200
    assert b"1,000.00" in resp.data
    assert b"1,200.00" not in resp.data


def test_tb_cross_check_offers_export_link_only_when_something_is_mismatched(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("-1000.00")))
    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 5), [], {"P1": Decimal("-1000.00")})
    store.close()

    clean = client.get("/reports/tb-cross-check")
    assert b"Export list" not in clean.data

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P2", "P2", "KOL", Decimal("0.00")))
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 6), voucher_number="SB/1",
        branch_id="KOL", party_ledger_name="P2",
        entries=[
            LedgerEntry(party_ledger_name="P2", amount_as_extracted=Decimal("-1000.00"), bill_name="SB/1", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Sales", amount_as_extracted=Decimal("1000.00")),
        ],
    )
    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 12), [voucher], {"P2": Decimal("900.00")})
    store.close()

    mismatched = client.get("/reports/tb-cross-check")
    assert b"Export list" in mismatched.data


def test_export_unreconciled_parties_downloads_a_workbook(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("P1", "P1", "KOL", Decimal("0.00")))
    process_branch_data(store, "KOL", "Kolkata", date(2026, 4, 5), [], {"P1": Decimal("-1000.00")})
    store.close()

    resp = client.get("/reports/tb-cross-check/export-unreconciled?to_date=2026-04-05")
    assert resp.status_code == 200
    assert resp.mimetype == _XLSX_MIMETYPE
    assert "Unreconciled_Parties" in resp.headers["Content-Disposition"]
    assert len(resp.data) > 0


def test_export_unreconciled_parties_excludes_reconciled_parties(client):
    from ar_mis.models import CustomerMasterRecord
    from ar_mis.pipeline import process_branch_data
    from ar_mis.storage import Store
    from io import BytesIO as _BytesIO
    from openpyxl import load_workbook

    store = Store(client.application.config["DB_PATH"])
    store.upsert_customer_master(CustomerMasterRecord("CLEAN", "CLEAN", "KOL", Decimal("0.00")))
    store.upsert_customer_master(CustomerMasterRecord("BAD", "BAD", "KOL", Decimal("0.00")))
    process_branch_data(
        store, "KOL", "Kolkata", date(2026, 4, 5), [],
        {"CLEAN": Decimal("0.00"), "BAD": Decimal("-500.00")},
    )
    store.close()

    resp = client.get("/reports/tb-cross-check/export-unreconciled?to_date=2026-04-05")
    wb = load_workbook(_BytesIO(resp.data))
    ws = wb.active
    party_ids = [row[0].value for row in ws.iter_rows(min_row=4)]
    assert party_ids == ["BAD"]


# ---- Reports: Branch Sales + CN + DN Total ---------------------------------


def test_branch_totals_renders_with_no_data(client):
    resp = client.get("/reports/branch-totals")
    assert resp.status_code == 200
    assert b"Branch Sales + CN + DN Total" in resp.data
    assert b"No invoices on record" in resp.data


def test_branch_totals_shows_sales_and_all_branches_row(client):
    _run_a_real_extraction(client)
    resp = client.get("/reports/branch-totals?period_start=2026-01-01&period_end=2026-12-31")
    assert resp.status_code == 200
    assert b"KOL" in resp.data
    assert b"All Branches" in resp.data
    assert b"1,25,000.00" in resp.data


def test_branch_totals_period_filter_excludes_out_of_range_invoice(client):
    _run_a_real_extraction(client)  # invoice dated 2026-04-06
    resp = client.get("/reports/branch-totals?period_start=2026-06-01&period_end=2026-12-31")
    assert resp.status_code == 200
    # The branch still shows (it has activity elsewhere), but zero for
    # this period - the April invoice must not leak into a June-onward window.
    assert b"KOL" in resp.data
    assert b"1,25,000.00" not in resp.data


def test_branch_totals_reachable_by_checker(roleless_client):
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/branch-totals")
    assert resp.status_code == 200


def test_reports_home_links_to_branch_totals(client):
    resp = client.get("/reports")
    assert b"Branch Sales + CN + DN Total" in resp.data


def test_reports_home_links_to_tb_cross_check(client):
    resp = client.get("/reports")
    assert b"TB Reconciliation Cross-Check" in resp.data


# ---- Reports: Backdated Entry (Drift) Findings -----------------------------


def _seed_drift_finding(client, voucher_number="SB/0099-BACKDATED"):
    from datetime import datetime

    from ar_mis.reconciliation import DriftFinding
    from ar_mis.storage import Store

    store = Store(client.application.config["DB_PATH"])
    finding = DriftFinding(
        party_ledger_name="ACME", voucher_type="Sales", voucher_number=voucher_number,
        voucher_date=date(2026, 3, 1), flipped_amount=Decimal("50000.00"), attributed_week=date(2026, 3, 8),
        voucher=Voucher(
            voucher_type=VoucherType.SALES, voucher_date=date(2026, 3, 1), voucher_number=voucher_number,
            branch_id="KOL", party_ledger_name="ACME",
            entries=[
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-50000.00"), bill_name=voucher_number, bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("50000.00")),
            ],
        ),
    )
    store.record_drift_findings("KOL", [finding], datetime(2026, 9, 1, 10, 0))
    store.close()


def test_drift_findings_renders_with_no_data(client):
    resp = client.get("/reports/drift-findings")
    assert resp.status_code == 200
    assert b"Backdated Entry Findings" in resp.data
    assert b"No backdated entries found yet" in resp.data


def test_drift_findings_shows_outstanding_finding_and_count(client):
    _seed_drift_finding(client)
    resp = client.get("/reports/drift-findings")
    assert resp.status_code == 200
    assert b"SB/0099-BACKDATED" in resp.data
    assert b"Outstanding" in resp.data
    assert b"50,000.00" in resp.data


def test_maker_can_acknowledge_a_finding(client):
    _seed_drift_finding(client)
    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    resp = client.post(
        f"/reports/drift-findings/{finding_id}/acknowledge",
        data={"acknowledged_by": "AR Manager"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Acknowledged" in resp.data
    assert b"AR Manager" in resp.data

    store = Store(client.application.config["DB_PATH"])
    record = store.all_drift_findings()[0]
    assert record.acknowledged is True
    assert record.acknowledged_by == "AR Manager"
    store.close()


def test_checker_can_view_but_not_acknowledge_drift_findings(roleless_client):
    _seed_drift_finding(roleless_client)
    roleless_client.post("/choose-role", data={"role": "checker"})
    resp = roleless_client.get("/reports/drift-findings")
    assert resp.status_code == 200
    assert b"Acknowledge" not in resp.data  # no button/form for a Checker

    from ar_mis.storage import Store
    store = Store(roleless_client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    resp = roleless_client.post(
        f"/reports/drift-findings/{finding_id}/acknowledge",
        data={"acknowledged_by": "Someone"},
        follow_redirects=True,
    )
    assert b"available for your role" in resp.data


def test_reports_home_links_to_drift_findings(client):
    resp = client.get("/reports")
    assert b"Backdated Entry Findings" in resp.data


def test_maker_can_incorporate_a_finding_and_reconcile_clean(client):
    _seed_drift_finding(client)  # ACME, SB/0099-BACKDATED, 50000.00, dated 2026-03-01
    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    resp = client.post(
        f"/reports/drift-findings/{finding_id}/incorporate",
        data={"incorporated_by": "AR Manager", "week_ending": "2026-09-12", "party_closing_extracted": "50000.00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"reconciled clean" in resp.data
    assert b"Incorporated" in resp.data
    assert b"AR Manager" in resp.data

    store = Store(client.application.config["DB_PATH"])
    record = store.all_drift_findings()[0]
    assert record.incorporated is True
    assert record.incorporated_week_ending == date(2026, 9, 12)
    rows = store.all_sales_dn_rows("KOL")
    assert len(rows) == 1
    assert rows[0].invoice_date == date(2026, 3, 1)
    store.close()


def test_incorporate_with_wrong_closing_still_incorporates_but_flags_mismatch(client):
    _seed_drift_finding(client)
    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    resp = client.post(
        f"/reports/drift-findings/{finding_id}/incorporate",
        data={"incorporated_by": "AR Manager", "week_ending": "2026-09-12", "party_closing_extracted": "999999.00"},
        follow_redirects=True,
    )
    assert b"still shows a difference" in resp.data

    store = Store(client.application.config["DB_PATH"])
    record = store.all_drift_findings()[0]
    assert record.incorporated is True  # still incorporated - the voucher is in the system
    store.close()


def test_incorporate_a_second_time_is_refused(client):
    _seed_drift_finding(client)
    from ar_mis.storage import Store
    store = Store(client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    client.post(
        f"/reports/drift-findings/{finding_id}/incorporate",
        data={"incorporated_by": "AR Manager", "week_ending": "2026-09-12", "party_closing_extracted": "50000.00"},
    )
    resp = client.post(
        f"/reports/drift-findings/{finding_id}/incorporate",
        data={"incorporated_by": "AR Manager", "week_ending": "2026-09-19", "party_closing_extracted": "50000.00"},
        follow_redirects=True,
    )
    assert b"already incorporated" in resp.data


def test_checker_cannot_incorporate_a_finding(roleless_client):
    _seed_drift_finding(roleless_client)
    roleless_client.post("/choose-role", data={"role": "checker"})

    from ar_mis.storage import Store
    store = Store(roleless_client.application.config["DB_PATH"])
    finding_id = store.all_drift_findings()[0].id
    store.close()

    resp = roleless_client.post(
        f"/reports/drift-findings/{finding_id}/incorporate",
        data={"incorporated_by": "Someone", "week_ending": "2026-09-12", "party_closing_extracted": "50000.00"},
        follow_redirects=True,
    )
    assert b"available for your role" in resp.data
