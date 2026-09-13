from datetime import date
from pathlib import Path

import pytest

from ar_mis.config import BranchConfig
from ar_mis.tally_client import (
    DEFAULT_TIMEOUT_SECONDS,
    CompanyMismatchError,
    TallyClient,
    _date_chunks,
)

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
    from ar_mis.models import VoucherType

    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    client = _client_with_stubbed_response(branch, raw)
    buckets = client.fetch_all_voucher_types(date(2026, 4, 1), date(2026, 4, 7))
    assert len(buckets[VoucherType.SALES]) == 2
    assert buckets[VoucherType.RECEIPT] == []


# ---- Chunked fetching for large date ranges ------------------------------
#
# Confirmed via a live-Tally diagnostic bisection (see tally_client.py's
# _VOUCHER_FETCH_CHUNK_DAYS comment): a large date range in one request can
# hang/time out in a way that doesn't scale linearly with calendar days.
# fetch_vouchers must transparently split any range wider than the chunk
# size into several smaller requests and stitch the results together.


def test_date_chunks_splits_a_wide_range_with_no_gaps_or_overlaps():
    chunks = _date_chunks(date(2026, 4, 1), date(2026, 4, 30), chunk_days=14)
    assert chunks == [
        (date(2026, 4, 1), date(2026, 4, 14)),
        (date(2026, 4, 15), date(2026, 4, 28)),
        (date(2026, 4, 29), date(2026, 4, 30)),
    ]


def test_date_chunks_returns_one_chunk_when_range_fits():
    chunks = _date_chunks(date(2026, 4, 1), date(2026, 4, 7), chunk_days=14)
    assert chunks == [(date(2026, 4, 1), date(2026, 4, 7))]


def test_date_chunks_handles_a_single_day_range():
    chunks = _date_chunks(date(2026, 4, 1), date(2026, 4, 1), chunk_days=14)
    assert chunks == [(date(2026, 4, 1), date(2026, 4, 1))]


def test_fetch_vouchers_issues_one_request_for_a_normal_weekly_range():
    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    call_count = 0

    def fake_post(xml_request):
        nonlocal call_count
        call_count += 1
        return raw

    client = TallyClient(branch=branch)
    client._post = fake_post  # type: ignore[method-assign]
    vouchers = client.fetch_vouchers(date(2026, 4, 1), date(2026, 4, 7))
    assert call_count == 1
    assert len(vouchers) == 2


def test_fetch_vouchers_chunks_a_wide_range_and_concatenates_results():
    branch = BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ")
    sales_raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    receipt_raw = (FIXTURES / "voucher_collection_receipt.xml").read_text()
    responses = [sales_raw, receipt_raw]
    requested_ranges: list[str] = []

    def fake_post(xml_request):
        requested_ranges.append(xml_request)
        return responses[len(requested_ranges) - 1]

    client = TallyClient(branch=branch)
    client._post = fake_post  # type: ignore[method-assign]
    # A 20-day range with a 14-day chunk size must split into exactly two
    # requests - one per real fixture file below - not one giant request.
    vouchers = client.fetch_vouchers(date(2026, 4, 1), date(2026, 4, 20))

    assert len(requested_ranges) == 2
    # Both fixtures' vouchers must be present - proof this is a real
    # concatenation across chunks, not just the last chunk's result.
    assert len(vouchers) == 3  # 2 from the sales fixture + 1 from the receipt fixture
    # Each chunk's own date range must actually reach the request, not a
    # single range reused for every chunk.
    assert "20260401" in requested_ranges[0] and "20260414" in requested_ranges[0]
    assert "20260415" in requested_ranges[1] and "20260420" in requested_ranges[1]


def test_default_timeout_is_generous_enough_for_a_real_chunk():
    # Confirmed via live-Tally diagnostic: a single 15-day chunk can
    # legitimately take close to a minute against a busy company. The old
    # 15s/30s timeouts used across this codebase would abort that
    # perfectly healthy request before it finished - this is the fix.
    assert DEFAULT_TIMEOUT_SECONDS >= 60.0
