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
    # Fixture reflects a real live-Tally response: multiple companies can
    # be open at once (Tally's multi-company feature). The expected
    # branch just needs to be *among* them, not the only one open.
    branch = BranchConfig(
        branch_id="MUN", branch_name="Mundra", tally_company_name="SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)"
    )
    raw = (FIXTURES / "list_of_companies.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    client.confirm_current_company()  # must not raise


def test_confirm_current_company_raises_on_mismatch():
    branch = BranchConfig(branch_id="DEL", branch_name="Delhi", tally_company_name="Delhi Branch")
    raw = (FIXTURES / "list_of_companies.xml").read_text()  # actually loaded: the 4 SPEEDWAYS companies
    client = _client_with_stubbed_response(branch, raw)
    with pytest.raises(CompanyMismatchError) as excinfo:
        client.confirm_current_company()
    assert excinfo.value.expected == "Delhi Branch"
    assert excinfo.value.actual == [
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (NAGPUR) - (from-1.4.23)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED VIZAG - (From 1.4.23)",
        "SPEEDWAYS LOGISTICS PVT. LTD. (DELHI) (from 1-Apr-23)",
    ]


def test_list_open_companies_returns_raw_list_with_no_matching_opinion():
    branch = BranchConfig(branch_id="_probe", branch_name="_probe", tally_company_name="")
    raw = (FIXTURES / "list_of_companies.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    companies = client.list_open_companies()
    assert companies == [
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (NAGPUR) - (from-1.4.23)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED VIZAG - (From 1.4.23)",
        "SPEEDWAYS LOGISTICS PVT. LTD. (DELHI) (from 1-Apr-23)",
    ]


def test_fetch_vouchers_returns_parsed_vouchers():
    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    from datetime import date

    vouchers = client.fetch_vouchers(date(2026, 4, 1), date(2026, 4, 7))
    assert len(vouchers) == 2
    assert all(v.branch_id == "KOL" for v in vouchers)


def test_fetch_all_voucher_types_buckets_from_a_single_fetch():
    from datetime import date

    from ar_mis.models import VoucherType

    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    buckets = client.fetch_all_voucher_types(date(2026, 4, 1), date(2026, 4, 7))
    assert len(buckets[VoucherType.SALES]) == 2
    assert buckets[VoucherType.RECEIPT] == []
