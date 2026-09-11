"""Flask test-client coverage for the Branch Master and Test Extraction
pages. Test Extraction hits ar_mis.tally_client.TallyClient, which is
monkeypatched here the same way test_cli.py does - no live Tally is
reachable from this build environment (see README).
"""
from datetime import date
from decimal import Decimal

import pytest

from ar_mis.models import LedgerEntry, Voucher, VoucherType
from ar_mis.webapp.app import create_app


@pytest.fixture
def client(tmp_path):
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
    resp = client.post("/test-extraction", data={"branch_id": "KOL", "days": "7"})
    assert b"ALL PASSED" in resp.data
    assert b"Company check" in resp.data
    assert b"Voucher extraction" in resp.data
    assert b"Sundry Debtors pull" in resp.data


def test_test_extraction_reports_connection_failure_clearly(client, monkeypatch):
    monkeypatch.setattr("ar_mis.webapp.app.TallyClient", FakeTallyClientUnreachable)
    _add_branch(client)
    resp = client.post("/test-extraction", data={"branch_id": "KOL", "days": "7"})
    assert b"FAILED" in resp.data
    assert b"Could not reach Tally" in resp.data
    # A connection failure must not go on to attempt voucher extraction.
    assert b"Voucher extraction" not in resp.data


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
