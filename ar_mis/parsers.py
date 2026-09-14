"""Parses Tally XML responses into ar_mis.models objects.

Three known real-world wrinkles (not hypotheticals):

1. Tally's XML export is widely reported to emit bare, unescaped `&`
   characters inside text values (e.g. a ledger literally named "A & B
   Transport") even though that is invalid XML. `_BARE_AMPERSAND` patches
   only bare `&` not already part of a valid entity.
2. CONFIRMED against a live TallyPrime instance: Tally emits numeric
   character references to control codepoints that are illegal in XML
   1.0 - e.g. `<GSTCLASS>&#4; Not Applicable</GSTCLASS>` - apparently as
   an internal placeholder for an unset enum-style field, and crashes
   ElementTree outright (`reference to invalid character number`) rather
   than just mis-reading that one field. `_NUMERIC_CHARREF` /
   `_strip_illegal_charref` strip exactly these references before
   parsing - the field's real text ("Not Applicable" etc.) survives.
3. CONFIRMED against the same live instance: Tally is not consistent
   about *how* it emits an illegal control codepoint - elsewhere in the
   same export it appears as a raw, literal control byte sitting directly
   in the text (not written out as `&#N;` at all), which crashes
   ElementTree differently (`not well-formed (invalid token)`).
   `_RAW_CONTROL_CHAR` strips these too. Between the two patches, every
   illegal-control-character form seen in live output is covered without
   guessing at every individual field that might carry one.

`_sanitize_xml` applies all three patches before handing the payload to
ElementTree, rather than silently dropping or crashing on affected
vouchers.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from ar_mis.models import LedgerEntry, Voucher, VoucherType

_BARE_AMPERSAND = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")

_NUMERIC_CHARREF = re.compile(r"&#(\d+);|&#x([0-9a-fA-F]+);")

# Raw (non-entity) characters illegal in XML 1.0 text content: the C0
# control range minus tab/newline/carriage-return, plus DEL, plus the two
# Unicode noncharacters U+FFFE/U+FFFF that XML 1.0's Char production also
# excludes (mirrors the upper bound already applied in
# _is_valid_xml_codepoint for the numeric-reference case, so a raw literal
# noncharacter is treated the same as one written out as `&#xfffe;`).
# Deliberately does not touch \t \n \r, which are legal and meaningful.
# Lone surrogates (U+D800-U+DFFF) are not included: a strict UTF-8 decode
# (what TallyClient._post uses) cannot produce them in the first place -
# that byte sequence would already have failed to decode.
_RAW_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def _is_valid_xml_codepoint(codepoint: int) -> bool:
    """XML 1.0's legal character ranges (spec section 2.2) - anything
    outside these, chiefly the low control codes below 0x20 other than
    tab/newline/carriage-return, cannot appear in a conforming document
    even as a numeric character reference.
    """
    return (
        codepoint == 0x9
        or codepoint == 0xA
        or codepoint == 0xD
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _strip_illegal_charref(match: re.Match) -> str:
    decimal_group, hex_group = match.groups()
    codepoint = int(decimal_group) if decimal_group is not None else int(hex_group, 16)
    return match.group(0) if _is_valid_xml_codepoint(codepoint) else ""

# Ordered deliberately: "Credit Note" / "Debit Note" are checked before
# "Sales" so a custom name like "Sales Credit Note" (if a deployment ever
# has one) categorizes as a credit note, the more specific match, rather
# than as a sale. Matching is substring/case-insensitive because real
# Tally deployments commonly customize voucher type names with prefixes
# or suffixes (e.g. "Sales - Export") while the underlying category is
# unchanged - Section 2.1's five categories, not Tally's literal name.
#
# "tax invoice" is a second SALES keyword, not just a suffix variant:
# confirmed against a real client's data that they run a genuine, separate
# Tally voucher type literally named "Tax Invoice" (its own numbering
# series, distinct from their "Sales" type's numbering) alongside "Sales"
# - both fundamentally the same AR-relevant category. Without this, any
# company that renamed/added a Sales-equivalent voucher type this way
# (common post-GST/e-invoicing) would have those vouchers silently
# excluded entirely, not just mis-labeled.
#
# "transport invoice" is a third, found the same way: confirmed against
# Speedways' own real one-week export that 154 of 171 real Sales vouchers
# that week (90%) used this exact voucher type name - their own renaming
# of Sales for a logistics business - with the rest split under "Sales
# 26-27" (already caught by the "sales" keyword above). Structurally
# confirmed as a genuine outward sales invoice (customer debit + CGST/SGST
# + a real bill reference), not a different category that happens to share
# the word "invoice" - this is deliberately NOT a bare "invoice" keyword,
# which would also wrongly catch an inward Purchase Invoice.
_CATEGORY_KEYWORDS: list[tuple[VoucherType, str]] = [
    (VoucherType.CREDIT_NOTE, "credit note"),
    (VoucherType.DEBIT_NOTE, "debit note"),
    (VoucherType.RECEIPT, "receipt"),
    (VoucherType.JOURNAL, "journal"),
    (VoucherType.SALES, "sales"),
    (VoucherType.SALES, "tax invoice"),
    (VoucherType.SALES, "transport invoice"),
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
    without_illegal_charrefs = _NUMERIC_CHARREF.sub(_strip_illegal_charref, raw)
    without_raw_control_chars = _RAW_CONTROL_CHAR.sub("", without_illegal_charrefs)
    return _BARE_AMPERSAND.sub("&amp;", without_raw_control_chars)


def _parse_tally_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _text(el: ET.Element | None, default: str = "") -> str:
    """Returns `default` for a missing element, a `None` .text (Tally's
    shape for a genuinely empty tag like `<AMOUNT></AMOUNT>`), AND a
    whitespace-only .text (confirmed present in real exports, e.g. an
    indented-but-content-free `<AMOUNT>\\n</AMOUNT>`) - the last case
    isn't hypothetical: it crashed Decimal(bill_amount) with
    ConversionSyntax against a real 8.7MB Sales register before this was
    caught, because .text was whitespace (not None) and so skipped the
    original None-only check, stripping down to "" right where a numeric
    caller expected either a real value or its own explicit default.
    """
    if el is None or el.text is None:
        return default
    stripped = el.text.strip()
    return stripped if stripped else default


def _find_ci(el: ET.Element, tag: str) -> ET.Element | None:
    """Case-insensitive direct-child lookup. A field requested via a
    plain `<FETCH>` always renders in ALL CAPS (CLOSINGBALANCE), but a
    field requested via `<NATIVEMETHOD>` renders using the exact case
    given in the request (e.g. ClosingBalance) - the same live Sundry
    Debtors ledger can come back either way depending on which mechanism
    a given request uses, and this codebase has needed both. `.find()`
    is case-sensitive and would silently miss the second shape, reading
    back a default rather than the real value with no error at all.
    """
    target = tag.lower()
    for child in el:
        if child.tag.lower() == target:
            return child
    return None


_FOREX_AMOUNT = re.compile(r"=\s*(-?[\d,]+\.?\d*)\s*[^\d\s]*\s*$")


def _parse_decimal_amount(raw: str) -> Decimal:
    """CONFIRMED against real client data: a plain `<AMOUNT>` field is not
    always a plain number. A company with foreign-currency (forex) ledgers
    reports those transactions as a formatted display string instead, e.g.
    `-$6200.00 @ 95.95 ₹/$ = -594890.00 ₹` - the foreign amount,
    the exchange rate, and the converted home-currency amount all in one
    field. AR reconciliation needs the home-currency figure (it's what
    Sundry Debtors' Trial Balance is always stated in, regardless of any
    individual invoice's billing currency), which is the number
    immediately after "=". Silently defaulting a forex line to 0 would
    understate that customer's real receivable - this raises instead of
    guessing wrong if a genuinely unrecognized amount format shows up.
    """
    try:
        return Decimal(raw)
    except InvalidOperation:
        pass
    match = _FOREX_AMOUNT.search(raw)
    if match:
        return Decimal(match.group(1).replace(",", ""))
    raise ValueError(f"Could not parse amount {raw!r} as a plain number or a forex display string")


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

    CONFIRMED against real client data (fixtures/real_samples/): Tally
    does not consistently use one tag name for a voucher's ledger entries.
    Sales, Credit Note, and Debit Note vouchers from one real company used
    plain `LEDGERENTRIES.LIST`; Receipt and Journal vouchers from the same
    export, and every voucher type from a different real company, used
    `ALLLEDGERENTRIES.LIST`. Checking only the "ALL" form (the original
    assumption) silently parses real Sales/CN/DN vouchers with zero
    entries - no amount, no party, no bill allocation - rather than
    failing loudly. Both tag names are checked; a real voucher has only
    ever been observed using one or the other, never both, but nothing
    here assumes that stays true.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    vouchers: list[Voucher] = []
    for v_el in root.iter("VOUCHER"):
        # A cancelled voucher never happened, by definition, in Tally's own
        # accounting - it must never count towards AR movement, exactly
        # like an irrelevant voucher type below. Confirmed against real
        # Speedways data: 9 real cancelled Sales vouchers in one week's
        # export, each retained by Tally as a stub with empty ledger
        # entries. Skipped here explicitly rather than relying on that
        # emptiness to accidentally fall out downstream - a cancelled
        # voucher that still carried its original entries would otherwise
        # be silently counted as a real sale.
        if _text(v_el.find("ISCANCELLED")).strip().lower() == "yes":
            continue

        raw_voucher_type_name = _text(v_el.find("VOUCHERTYPENAME"))
        voucher_type = categorize_voucher_type(raw_voucher_type_name)
        if voucher_type is None:
            continue

        entries: list[LedgerEntry] = []
        ledger_entry_elements = v_el.findall("ALLLEDGERENTRIES.LIST") + v_el.findall("LEDGERENTRIES.LIST")
        for le_el in ledger_entry_elements:
            party_ledger = _text(le_el.find("LEDGERNAME"))
            raw_amount = _text(le_el.find("AMOUNT"), "0")
            bill_allocs = le_el.findall("BILLALLOCATIONS.LIST")
            if bill_allocs:
                for bill_el in bill_allocs:
                    bill_name = _text(bill_el.find("NAME")) or None
                    bill_type = _text(bill_el.find("BILLTYPE")) or None
                    bill_amount = _text(bill_el.find("AMOUNT"), raw_amount)
                    entries.append(
                        LedgerEntry(
                            party_ledger_name=party_ledger,
                            amount_as_extracted=_parse_decimal_amount(bill_amount),
                            bill_name=bill_name,
                            bill_type=bill_type,
                        )
                    )
            else:
                entries.append(
                    LedgerEntry(
                        party_ledger_name=party_ledger,
                        amount_as_extracted=_parse_decimal_amount(raw_amount),
                        bill_name=None,
                        bill_type=None,
                    )
                )

        # CONFIRMED against real Speedways data: an "as invoice" voucher
        # entered against stock items sometimes carries only the party's
        # own top-level ledger entry - the real revenue/tax breakdown
        # lives one level down, in each stock item's own
        # ACCOUNTINGALLOCATIONS.LIST inside ALLINVENTORYENTRIES.LIST (8
        # real vouchers in one week, one summing 5 x Rs 20000 stock-item
        # allocations to the exact Rs 100000 the party was debited).
        # Only used as a fallback when top-level entries are just the
        # party's own side (fewer than 2) - most real invoices in the
        # same export already carry a complete top-level picture (party +
        # CGST + SGST) AND their own inventory detail for line-item
        # breakdown, so unconditionally adding inventory allocations on
        # top would double-count revenue for those (confirmed: 154 real
        # vouchers in the same file have both).
        if len(entries) < 2:
            for inv_el in v_el.findall("ALLINVENTORYENTRIES.LIST"):
                for alloc_el in inv_el.findall("ACCOUNTINGALLOCATIONS.LIST"):
                    ledger_name = _text(alloc_el.find("LEDGERNAME"))
                    if not ledger_name:
                        continue
                    entries.append(
                        LedgerEntry(
                            party_ledger_name=ledger_name,
                            amount_as_extracted=_parse_decimal_amount(_text(alloc_el.find("AMOUNT"), "0")),
                            bill_name=None,
                            bill_type=None,
                        )
                    )

        vouchers.append(
            Voucher(
                voucher_type=voucher_type,
                voucher_date=_parse_tally_date(_text(v_el.find("DATE"))),
                voucher_number=_text(v_el.find("VOUCHERNUMBER")),
                branch_id=branch_id,
                party_ledger_name=_text(v_el.find("PARTYLEDGERNAME")),
                entries=entries,
                raw_voucher_type_name=raw_voucher_type_name,
            )
        )
    return vouchers


def parse_ledger_closing_balances(raw_xml: str) -> dict[str, Decimal]:
    """Parses a Trial Balance / Sundry Debtors closing-balance export into
    {ledger_name: closing_balance_as_extracted}. Sign-flip is applied
    downstream, not here.

    Handles two real, structurally unrelated shapes, since which one a
    given file uses depends on how it left Tally, not anything this
    codebase controls:

    1. The live gateway's Collection response - `<LEDGER NAME="...">`
       with a `<CLOSINGBALANCE>` child. What xml_requests.ytd_sundry_debtors_request
       produces and the original shape this function was written for.
    2. Tally's own "Display Report" shape - confirmed against a real
       manually-exported Trial Balance (fixtures/real_samples/TBDebtors.xml,
       389 parties) - which has no LEDGER element at all. Each party is a
       `<DSPACCNAME><DSPDISPNAME>name</DSPDISPNAME></DSPACCNAME>` element
       immediately followed by a sibling
       `<DSPACCINFO>...<DSPCLAMT><DSPCLAMTA>amount</DSPCLAMTA></DSPCLAMT></DSPACCINFO>`
       - name and closing balance live in two separate sibling elements,
       not one. Manual Upload's Trial Balance slot only ever receives
       this shape in practice (an operator exporting from Tally's UI, not
       the live gateway), which is what this fix targets.

    The two DSPACCNAME/DSPACCINFO tag streams are paired positionally by
    document order (confirmed 1:1 alternating in the real sample, with
    matching counts of every one of the four DSP* tags used here) rather
    than by any shared key, because Tally's Display Report genuinely
    doesn't put one on either element - there is nothing else to join on.
    A blank DSPCLAMTA (confirmed present for a handful of real zero-
    balance accounts) is treated as "0", not skipped or errored - a
    zero-balance debtor is a real, meaningful data point for the
    reconciliation this feeds, not missing data.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    ledger_elements = root.findall(".//LEDGER")
    if ledger_elements:
        balances: dict[str, Decimal] = {}
        for ledger_el in ledger_elements:
            # CONFIRMED against a real Charze ledger: the NAME attribute
            # can carry stray whitespace/control characters baked into
            # the ledger's own name inside Tally itself (a real ledger
            # came back as "RAASHI ENTERPRISES\r\n\r\n") - unlike every
            # voucher-side name field in this module (PARTYLEDGERNAME,
            # LEDGERNAME), which is always read through _text() and so
            # always comes back trimmed. Reading the attribute raw here
            # let the exact same real ledger produce two different
            # strings depending on which extraction pulled it, so a
            # voucher posted against it could never match this pull's
            # own key. Stripped the same way every other name in this
            # file already is - not a new rule, just applying the
            # existing one consistently.
            name = (ledger_el.get("NAME") or "").strip() or _text(_find_ci(ledger_el, "NAME"))
            closing = _text(_find_ci(ledger_el, "CLOSINGBALANCE"), "0")
            if name:
                balances[name] = _parse_decimal_amount(closing)
        return balances

    names = [_text(el.find("DSPDISPNAME")) for el in root.iter("DSPACCNAME")]
    closings = [
        _text(el.find("DSPCLAMT/DSPCLAMTA"), "0") for el in root.iter("DSPACCINFO")
    ]
    if len(names) != len(closings):
        raise ValueError(
            f"Trial Balance Display Report has {len(names)} DSPACCNAME entries but "
            f"{len(closings)} DSPACCINFO entries - cannot pair name to closing balance "
            "by position when the two streams don't line up 1:1."
        )
    return {name: _parse_decimal_amount(closing) for name, closing in zip(names, closings) if name}


def parse_currently_loaded_companies(raw_xml: str) -> list[str]:
    """Parses a 'List of Companies' response. Tally reports only
    currently-open companies here, which is what Section 2.2's
    pre-extraction confirmation check relies on.

    CONFIRMED against a live TallyPrime instance: the response envelope's
    BODY/DESC/CMPINFO block is a diagnostics section Tally always
    includes, carrying plain counts under tags that happen to share names
    with the real payload - <COMPANY>0</COMPANY>, <GROUP>0</GROUP>,
    <LEDGER>0</LEDGER>, and many more, all just counters, nothing to do
    with actual company names. A tree-wide search for any element named
    COMPANY or NAME (the original approach) picks up that stray
    <COMPANY>0</COMPANY> as a second, phantom company literally named
    "0". The real payload only ever lives inside BODY/DATA/COLLECTION, so
    the search is scoped to there and nowhere else in the envelope.
    """
    root = ET.fromstring(_sanitize_xml(raw_xml))
    names: list[str] = []
    for collection in root.iter("COLLECTION"):
        for el in collection.iter():
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
