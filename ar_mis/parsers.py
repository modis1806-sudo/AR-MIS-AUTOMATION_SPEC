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


def _sanitize_xml(raw: str) -> str:
    return _BARE_AMPERSAND.sub("&amp;", raw)


def _parse_tally_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def parse_voucher_collection(raw_xml: str, branch_id: str) -> list[Voucher]:
    root = ET.fromstring(_sanitize_xml(raw_xml))
    vouchers: list[Voucher] = []
    for v_el in root.iter("VOUCHER"):
        voucher_type_name = _text(v_el.find("VOUCHERTYPENAME"))
        try:
            voucher_type = VoucherType(voucher_type_name)
        except ValueError:
            # Unknown/unexpected voucher type in the response for a
            # collection that filtered on a specific type - surfacing
            # this as a hard error is safer than silently coercing it.
            raise ValueError(
                f"Unrecognized VOUCHERTYPENAME '{voucher_type_name}' in extraction response"
            ) from None

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
