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
    """Collection export request for the full Sundry Debtors ledger group
    from day 1 of the financial year, for the Section 4.2 YTD cross-check.
    Fetches closing balance per ledger (party).
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
      <FETCH>NAME,PARENT,CLOSINGBALANCE,OPENINGBALANCE</FETCH>
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
