"""Parses Tally XML responses into ar_mis.models objects.

Known real-world wrinkle (not a hypothetical): Tally's XML export is
widely reported to emit bare, unescaped `&` characters inside text values
(e.g. a ledger literally named "A & B Transport") even though that is
invalid XML. `_sanitize_xml` patches only bare `&` not already part of a
valid entity, before handing the payload to ElementTree, rather than
silently dropping or crashing on those vouchers.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

from ar_mis.models import LedgerEntry, Voucher, VoucherType

_BARE_AMPERSAND = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")

# Ordered deliberately: "Credit Note" / "Debit Note" are checked before
# "Sales" so a custom name like "Sales Credit Note" (if a deployment ever
# has one) categorizes as a credit note, the more specific match, rather
# than as a sale. Matching is substring/case-insensitive because real
# Tally deployments commonly customize voucher type names with prefixes
# or suffixes (e.g. "Sales - Export") while the underlying category is
# unchanged - Section 2.1's five categories, not Tally's literal name.
_CATEGORY_KEYWORDS: list[tuple[VoucherType, str]] = [
    (VoucherType.CREDIT_NOTE, "credit note"),
    (VoucherType.DEBIT_NOTE, "debit note"),
    (VoucherType.RECEIPT, "receipt"),
    (VoucherType.JOURNAL, "journal"),
    (VoucherType.SALES, "sales"),
]

# "Stock Journal" is Tally's built-in name for an inventory transfer
# between godowns - structurally unrelated to an accounting Journal
# despite sharing the word "journal". A naive substring match on
# "journal" would wrongly pull every stock transfer into AR movement.
_EXCLUDED_NAME_SUBSTRINGS = ["stock journal"]


def categorize_voucher_type(raw_voucher_type_name: str) -> VoucherType | None:
    """Maps a raw Tally VOUCHERTYPENAME to one of the five AR-relevant
    categories, or None if it doesn't match any of them - which is the
    normal, expected outcome for the many voucher types a real company
    has that are irrelevant to debtor movement (Payment, Contra,
    Purchase, Stock Journal, and so on). None is not an error case.
    """
    lowered = raw_voucher_type_name.strip().lower()
    if any(excluded in lowered for excluded in _EXCLUDED_NAME_SUBSTRINGS):
        return None
    for category, keyword in _CATEGORY_KEYWORDS:
        if keyword in lowered:
            return category
    return None


def _sanitize_xml(raw: str) -> str:
    return _BARE_AMPERSAND.sub("&amp;", raw)


def _parse_tally_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def parse_voucher_collection(raw_xml: str, branch_id: str) -> list[Voucher]:
    """Parses every voucher in the response, keeping only the ones that
    categorize into one of Section 2.1's five AR-relevant types
    (categorize_voucher_type). A voucher whose type doesn't match any of
    them (Payment, Contra, Purchase, Stock Journal, ...) is silently
    skipped - that's the normal case for most of a company's vouchers,
    not an error. This request is no longer filtered server-side by
    voucher type (see xml_requests.voucher_export_request), so this
    function is what actually separates AR-relevant vouchers from
    everything else in the company's books.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    vouchers: list[Voucher] = []
    for v_el in root.iter("VOUCHER"):
        raw_voucher_type_name = _text(v_el.find("VOUCHERTYPENAME"))
        voucher_type = categorize_voucher_type(raw_voucher_type_name)
        if voucher_type is None:
            continue

        entries: list[LedgerEntry] = []
        for le_el in v_el.findall("ALLLEDGERENTRIES.LIST"):
            party_ledger = _text(le_el.find("LEDGERNAME"))
            raw_amount = _text(le_el.find("AMOUNT"), "0")
            bill_allocs = le_el.findall("BILLALLOCATIONS.LIST")
            if bill_allocs:
                for bill_el in bill_allocs:
                    bill_name = _text(bill_el.find("NAME")) or None
                    bill_amount = _text(bill_el.find("AMOUNT"), raw_amount)
                    entries.append(
                        LedgerEntry(
                            party_ledger_name=party_ledger,
                            amount_as_extracted=Decimal(bill_amount),
                            bill_name=bill_name,
                        )
                    )
            else:
                entries.append(
                    LedgerEntry(
                        party_ledger_name=party_ledger,
                        amount_as_extracted=Decimal(raw_amount),
                        bill_name=None,
                    )
                )

        vouchers.append(
            Voucher(
                voucher_type=voucher_type,
                voucher_date=_parse_tally_date(_text(v_el.find("DATE"))),
                voucher_number=_text(v_el.find("VOUCHERNUMBER")),
                branch_id=branch_id,
                entries=entries,
                raw_voucher_type_name=raw_voucher_type_name,
            )
        )
    return vouchers


def parse_ledger_closing_balances(raw_xml: str) -> dict[str, Decimal]:
    """Parses the YTD Sundry Debtors Collection response into
    {ledger_name: closing_balance_as_extracted}. Sign-flip is applied
    downstream, not here.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    balances: dict[str, Decimal] = {}
    for ledger_el in root.iter("LEDGER"):
        name = ledger_el.get("NAME") or _text(ledger_el.find("NAME"))
        closing = _text(ledger_el.find("CLOSINGBALANCE"), "0")
        if name:
            balances[name] = Decimal(closing)
    return balances


def parse_currently_loaded_companies(raw_xml: str) -> list[str]:
    """Parses a 'List of Companies' response. Tally reports only
    currently-open companies here, which is what Section 2.2's
    pre-extraction confirmation check relies on.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    names: list[str] = []
    for el in root.iter():
        if el.tag in ("COMPANY", "NAME") and el.text and el.text.strip():
            names.append(el.text.strip())
    # Dedupe while preserving order (COMPANY and nested NAME can both match).
    seen: set[str] = set()
    result = []
    for n in names:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result
