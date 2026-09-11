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

from ar_mis.models import VoucherType

_TALLY_DATE_FMT = "%Y%m%d"


def _tally_date(d: date) -> str:
    return d.strftime(_TALLY_DATE_FMT)


def voucher_export_request(
    company_name: str,
    voucher_type: VoucherType,
    from_date: date,
    to_date: date,
) -> str:
    """Collection export request for one voucher type over a date range,
    fetching bill-wise allocation detail via ALLLEDGERENTRIES.LIST /
    BILLALLOCATIONS.LIST.
    """
    company = escape(company_name)
    vtype = escape(voucher_type.value)
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Voucher Collection</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{_tally_date(from_date)}</SVFROMDATE>
    <SVTODATE>{_tally_date(to_date)}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Voucher Collection" ISMODIFY="No">
      <TYPE>Voucher</TYPE>
      <FILTERS>ARMISVoucherTypeFilter</FILTERS>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME,ALLLEDGERENTRIES.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.LEDGERNAME,ALLLEDGERENTRIES.AMOUNT,ALLLEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
      <FETCH>ALLLEDGERENTRIES.BILLALLOCATIONS.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.BILLALLOCATIONS.NAME,ALLLEDGERENTRIES.BILLALLOCATIONS.AMOUNT</FETCH>
     </COLLECTION>
     <SYSTEM TYPE="Formulae" NAME="ARMISVoucherTypeFilter">$VoucherTypeName = "{vtype}"</SYSTEM>
    </TDLMESSAGE>
   </TDL>
  </DESC>
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
