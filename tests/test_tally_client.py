from pathlib import Path

import pytest

from ar_mis.config import BranchConfig
from ar_mis.tally_client import CompanyMismatchError, TallyClient

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _client_with_stubbed_response(branch: BranchConfig, raw_response: str) -> TallyClient:
    client = TallyClient(branch=branch)
    client._post = lambda xml_request: raw_response  # type: ignore[method-assign]
    return client


def test_confirm_current_company_passes_when_loaded_company_matches():
    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "list_of_companies.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    client.confirm_current_company()  # must not raise


def test_confirm_current_company_raises_on_mismatch():
    branch = BranchConfig(branch_id="DEL", branch_name="Delhi", tally_company_name="Delhi Branch")
    raw = (FIXTURES / "list_of_companies.xml").read_text()  # actually loaded: Kolkata HQ
    client = _client_with_stubbed_response(branch, raw)
    with pytest.raises(CompanyMismatchError) as excinfo:
        client.confirm_current_company()
    assert excinfo.value.expected == "Delhi Branch"
    assert excinfo.value.actual == ["Kolkata HQ"]


def test_fetch_vouchers_returns_parsed_vouchers():
    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    from datetime import date

    from ar_mis.models import VoucherType

    vouchers = client.fetch_vouchers(VoucherType.SALES, date(2026, 4, 1), date(2026, 4, 7))
    assert len(vouchers) == 2
    assert all(v.branch_id == "KOL" for v in vouchers)
