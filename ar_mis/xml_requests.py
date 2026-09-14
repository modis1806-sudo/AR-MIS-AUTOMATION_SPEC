"""Builds Tally XML/HTTP request envelopes.

Per Section 2.1, this uses Tally's TDL-based "Collection" export request
uniformly for all five voucher types, because that is the only request
shape that serializes nested BILLALLOCATIONS.LIST detail (a plain
"Export Data" report request does not reliably flatten it for Receipt and
Journal vouchers).

CAVEAT (see README "Known limitation"): these templates follow Tally's
publicly documented TDL/XML request schema. They have been validated
against fixture responses shaped like real Tally output, not against a
live Tally session. Confirm field names against the client's actual
Tally version (TallyPrime vs Tally.ERP 9) before the first live run.
"""
from __future__ import annotations

from datetime import date
from xml.sax.saxutils import escape

_TALLY_DATE_FMT = "%Y%m%d"


def _tally_date(d: date) -> str:
    return d.strftime(_TALLY_DATE_FMT)


def voucher_export_request(
    company_name: str,
    from_date: date,
    to_date: date,
) -> str:
    """Voucher Register export request for every voucher over a date
    range, returning full native voucher objects (bill-wise allocation
    detail included via ALLLEDGERENTRIES.LIST / BILLALLOCATIONS.LIST).

    CONFIRMED against a live TallyPrime instance (see
    docs/registers_and_reporting_design.md / commit history for the
    session that diagnosed this) across two real bugs, in order:

    1. A TDL Collection request (`<TYPE>Collection</TYPE>` with a
       `<FETCH>` list) hangs indefinitely - no response, no error - the
       moment any nested LIST-type field (ALLLEDGERENTRIES.LIST) is
       requested, regardless of exact FETCH syntax. Flat-field Collection
       requests work fine, which is how this was isolated. Fixed by
       switching to a REPORTNAME-based "Export Data" request instead,
       which returns the full native voucher object with no FETCH list
       needed at all.
    2. The first REPORTNAME tried, "Day Book", silently **ignored**
       SVFROMDATE/SVTODATE entirely - confirmed by requesting two
       non-overlapping date ranges and getting a byte-identical response
       both times, always showing whatever period Tally's own UI session
       currently has set rather than the requested range. Day Book is
       apparently tied to Tally's live UI period state in a way other
       reports aren't. "Voucher Register" was tested as an alternative
       and confirmed to (a) correctly return different vouchers for
       different date ranges and (b) still carry the exact same
       ALLLEDGERENTRIES.LIST/BILLALLOCATIONS.LIST/PARTYLEDGERNAME detail
       Day Book did. This function now uses Voucher Register.

    Takeaway for future maintainers: a REPORTNAME-based request's
    date-range behavior is not something to assume from one working
    example - different named reports can behave differently against the
    same static variables, and this must be verified per report name
    against a live instance, not just assumed to generalize.

    Deliberately fetches ALL voucher types in one request rather than
    filtering server-side per type (an earlier version did, via a TDL
    `$VoucherTypeName = "Sales"` formula). Real Tally deployments
    commonly have custom-named voucher types with prefixes/suffixes
    (e.g. "Sales - Export") that are still fundamentally that category -
    exact server-side filtering would silently miss them. Categorization
    instead happens client-side in parsers.py, where it's ordinary
    Python string matching we can trust and test. Voucher Register
    naturally returns every voucher type for the period, so this still
    holds.

    No FETCH/field list is specified - a REPORTNAME-based Export Data
    request returns the full native voucher object regardless, which
    parsers.parse_voucher_collection reads by plain tag name (LEDGERNAME,
    AMOUNT, NAME) exactly as confirmed present in the live response - no
    parser changes were needed for either fix.
    """
    company = escape(company_name)
    return f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
     <SVFROMDATE>{_tally_date(from_date)}</SVFROMDATE>
     <SVTODATE>{_tally_date(to_date)}</SVTODATE>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""


def ytd_sundry_debtors_request(company_name: str, fy_start: date, as_of: date) -> str:
    """Export request for the full Sundry Debtors ledger group's closing
    balances as of a specific date, for the Section 4.1/4.2 cross-check.

    CANDIDATE FIX v2, NOT YET LIVE-CONFIRMED. History, in order:

    v1 (original): a raw `<TYPE>Collection</TYPE>` of `<TYPE>Ledger</TYPE>`
    with `<FETCH>CLOSINGBALANCE,OPENINGBALANCE</FETCH>` and SVFROMDATE/
    SVTODATE. CONFIRMED BROKEN against a real Charze ledger dump: two live
    pulls for the same ledger, one asking for 2026-04-02 and one for
    2026-09-14, came back byte-identical (-79484.00 both times), while
    Tally's own UI (F2: Period set to that exact 2-day window) showed the
    true figure (730). The raw CLOSINGBALANCE/OPENINGBALANCE object
    fields are apparently a cached master-level value, not scoped by the
    STATICVARIABLES actually sent - same class of bug voucher_export_
    request's own docstring documents for "Day Book" silently reading
    Tally's live UI/session period instead.

    v2 (this version) went the other direction - a REPORTNAME-based
    "Trial Balance" export, with SVCURRENTGROUP added to scope it to
    Sundry Debtors. CONFIRMED PARTIALLY WORKING: the two dates now
    genuinely return different figures (the date IS being respected) -
    but SVCURRENTGROUP had no scoping effect at all. The response was
    the whole company's Trial Balance at primary-group level (Capital
    Account, Current Liabilities, Current Assets, ...) - Sundry Debtors
    doesn't even appear as its own row (it's nested inside Current
    Assets), and group-level totals aren't what's needed anyway - this
    cross-check is per-party, not a portfolio total.

    v3 (current): keeps v1's proven-correct ledger selection
    (`BELONGSTO`/`CHILDOF` under Sundry Debtors, confirmed via a live
    diagnostics run to correctly find all 964 real debtor ledgers,
    including ones nested under custom sub-groups) but replaces the raw
    CLOSINGBALANCE field with a COMPUTE using Tally's own `$$ClosingBalance`
    system formula function, which (per Tally's TDL function reference)
    is explicitly date-aware within a Collection's own SVFROMDATE/SVTODATE
    context - unlike the raw object field, which is not. The computed
    field is still named CLOSINGBALANCE in the output, so parsers.
    parse_ledger_closing_balances needs no change either way.

    MUST be re-verified the same way both previous attempts were: pull
    the same ledger for two different `as_of` dates and confirm the
    values now genuinely differ AND match Tally's own F2:Period-scoped
    figure for that exact ledger - before trusting this in a real save.
    """
    company = escape(company_name)
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Sundry Debtors YTD</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{_tally_date(fy_start)}</SVFROMDATE>
    <SVTODATE>{_tally_date(as_of)}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Sundry Debtors YTD" ISMODIFY="No">
      <TYPE>Ledger</TYPE>
      <BELONGSTO>Yes</BELONGSTO>
      <CHILDOF>Sundry Debtors</CHILDOF>
      <FETCH>NAME,PARENT,OPENINGBALANCE</FETCH>
      <COMPUTE>CLOSINGBALANCE:$$ClosingBalance:$Name</COMPUTE>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def list_of_companies_request() -> str:
    """Returns only the company/companies currently OPEN in the target
    Tally instance -- the basis for confirm_current_company() (Section
    2.2's mandatory pre-extraction check).

    Uses a TDL Collection over the built-in `Company` object type, not
    the simpler `REPORTNAME: List of Companies` EXPORTDATA request that
    an earlier version of this function used. That simpler form was
    never validated against a live Tally instance during development
    (see README's "Known limitation") and, confirmed against a real
    TallyPrime Gold server, returns
    `<RESPONSE>Unknown Request, cannot be processed</RESPONSE>` instead
    of company data -- a real version/edition difference, not a
    hypothetical one. The Collection form below was verified against
    the same live instance and correctly returns every currently-open
    company (Tally supports multiple companies open at once, which is
    exactly why this is a membership check against a list, not a
    single-value comparison).
    """
    return """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS List of Companies</ID>
 </HEADER>
 <BODY>
  <DESC>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS List of Companies" ISINITIALIZE="Yes">
      <TYPE>Company</TYPE>
      <FETCH>NAME</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""
