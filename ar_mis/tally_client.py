"""HTTP transport to Tally's XML gateway, and the Section 2.2 mandatory
pre-extraction company check.

Tally's HTTP/XML gateway (Gateway of Tally > F1 > Advanced Configuration
> "Enable ODBC/HTTP Server", default port 9000) accepts a raw XML request
body via POST to the root URL and returns a raw XML response. No auth,
no headers beyond Content-Type - this is a LAN-local trust model, matching
the spec's "LAN-only environment, no cloud dependency" framing.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.config import BranchConfig
from ar_mis.models import Voucher, VoucherType
from ar_mis.parsers import (
    parse_currently_loaded_companies,
    parse_ledger_closing_balances,
    parse_voucher_collection,
)
from ar_mis.xml_requests import (
    list_of_companies_request,
    voucher_export_request,
    ytd_sundry_debtors_request,
)


class TallyConnectionError(RuntimeError):
    """Raised for any transport-level failure talking to Tally: refused
    connection, timeout, or a malformed/empty response. This is the
    exception ar_mis.orchestration catches to decide a branch has failed
    extraction (Section 4.3) - it is deliberately one broad exception
    type so the orchestrator doesn't need to know the difference between
    a dropped socket and garbage XML.
    """


class CompanyMismatchError(RuntimeError):
    """Raised when the company currently loaded in Tally does not match
    the expected branch. Section 2.2: this must halt with a clear error,
    never proceed on the operator's word alone.
    """

    def __init__(self, expected: str, actual: list[str]):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Expected company '{expected}' to be loaded, but Tally reports: {actual or '(none open)'}"
        )


@dataclass
class TallyClient:
    branch: BranchConfig
    timeout_seconds: float = 30.0

    def _post(self, xml_request: str) -> str:
        data = xml_request.encode("utf-8")
        req = urllib.request.Request(
            self.branch.gateway_url,
            data=data,
            headers={"Content-Type": "text/xml"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TallyConnectionError(
                f"Could not reach Tally at {self.branch.gateway_url} for branch "
                f"'{self.branch.branch_name}': {exc}"
            ) from exc
        if not raw:
            raise TallyConnectionError(
                f"Empty response from Tally at {self.branch.gateway_url} for branch "
                f"'{self.branch.branch_name}'"
            )
        # Tally typically responds UTF-8 or UTF-16 depending on version;
        # try UTF-8 first (the common case) and fall back.
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-16")

    def list_open_companies(self) -> list[str]:
        """Every company currently open/reachable at this gateway, with no
        opinion about which one is "right" - the raw building block behind
        both confirm_current_company() (checks membership) and the webapp's
        Discover Companies page (lets a human pick from this same list
        instead of having to already know the exact name to type in).
        """
        raw = self._post(list_of_companies_request())
        return parse_currently_loaded_companies(raw)

    def confirm_current_company(self) -> None:
        """Section 2.2 mandatory check. Raises CompanyMismatchError if the
        company loaded in this Tally instance isn't the expected branch.
        Must be called, and must succeed, before any extraction request
        for this branch.
        """
        loaded = self.list_open_companies()
        if self.branch.tally_company_name not in loaded:
            raise CompanyMismatchError(self.branch.tally_company_name, loaded)

    def fetch_vouchers(self, from_date: date, to_date: date) -> list[Voucher]:
        """One request for every voucher in the period - see
        xml_requests.voucher_export_request for why this isn't filtered
        by type server-side. Only vouchers that categorize into one of
        the five AR-relevant types (parsers.categorize_voucher_type) come
        back; everything else (Payment, Contra, Purchase, ...) is
        already excluded by parse_voucher_collection.
        """
        raw = self._post(voucher_export_request(self.branch.tally_company_name, from_date, to_date))
        return parse_voucher_collection(raw, self.branch.branch_id)

    def fetch_all_voucher_types(
        self, from_date: date, to_date: date
    ) -> dict[VoucherType, list[Voucher]]:
        """Same vouchers as fetch_vouchers(), bucketed by category - kept
        as a separate method because callers (pipeline.py, the webapp,
        the weekly CLI) want per-type breakdowns, not because it costs a
        second request; it's the same one request as fetch_vouchers().
        """
        buckets: dict[VoucherType, list[Voucher]] = {vt: [] for vt in VoucherType}
        for voucher in self.fetch_vouchers(from_date, to_date):
            buckets[voucher.voucher_type].append(voucher)
        return buckets

    def fetch_ytd_sundry_debtors(self, fy_start: date, as_of: date) -> dict[str, Decimal]:
        raw = self._post(ytd_sundry_debtors_request(self.branch.tally_company_name, fy_start, as_of))
        return parse_ledger_closing_balances(raw)
