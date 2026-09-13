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
    # Pre-selects the Preparer role so every existing test here (written
    # before role gating existed) keeps exercising full access, exactly
    # as it did before - role-gating behavior itself (a Viewer being
    # blocked, the picker, the switcher) has its own dedicated tests
    # below using a client that does NOT pre-select a role.
    app = create_app(db_path=str(tmp_path / "webapp.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["role"] = "preparer"
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


def test_choosing_preparer_grants_extraction_access(roleless_client):
    resp = roleless_client.post("/choose-role", data={"role": "preparer"}, follow_redirects=True)
    assert resp.status_code == 200
    resp = roleless_client.get("/branches")
    assert resp.status_code == 200


def test_choosing_viewer_blocks_extraction_routes(roleless_client):
    roleless_client.post("/choose-role", data={"role": "viewer"})
    for path in ("/branches", "/test-extraction", "/discover", "/manual-upload"):
        resp = roleless_client.get(path, follow_redirects=True)
        assert resp.status_code == 200
        assert b"isn&#39;t available for your role" in resp.data or b"isn't available for your role" in resp.data


def test_viewer_can_reach_registers_and_reports(roleless_client):
    roleless_client.post("/choose-role", data={"role": "viewer"})
    for path in ("/customers", "/registers/sales-dn", "/registers/credit-notes", "/registers/receipts-journals", "/reports"):
        resp = roleless_client.get(path)
        assert resp.status_code == 200

    # The nav itself must not even offer the Extraction links to a Viewer.
    resp = roleless_client.get("/")
    assert b"Branch Master" not in resp.data
    assert b"Test Extraction" not in resp.data


def test_viewer_cannot_mark_a_customer_reconciled(roleless_client):
    roleless_client.post("/choose-role", data={"role": "viewer"})
    resp = roleless_client.post(
        "/customers/reconcile", data={"party_id": "P1", "branch_id": "KOL", "reconciled_by": "CFO"},
        follow_redirects=True,
    )
    assert b"isn&#39;t available for your role" in resp.data or b"isn't available for your role" in resp.data


def test_preparer_sees_extraction_links_in_nav(client):
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


def test_credit_note_register_and_receipt_journal_register_render_when_empty(client):
    for path in ("/registers/credit-notes", "/registers/receipts-journals"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert b"No " in resp.data  # the empty-state card text
