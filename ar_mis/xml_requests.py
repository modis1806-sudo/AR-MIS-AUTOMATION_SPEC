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

    CONFIRMED CORRECT (v5) against a live TallyPrime instance: pulled the
    same real ledger (AARTH ELECTRICALS) for two genuinely different
    `as_of` dates and got two genuinely different closing balances -
    -730.00 for 2026-04-02, -79484.00 for 2026-09-14 - matching Tally's
    own F2:Period-scoped figure for that exact ledger exactly. Four
    earlier shapes were tried and live-disproven first (kept below as
    engineering history - each taught something the next one needed):

    v1: raw `<TYPE>Collection</TYPE>` of `<TYPE>Ledger</TYPE>` with
    `<FETCH>CLOSINGBALANCE,OPENINGBALANCE</FETCH>`. BROKEN: both dates
    returned the exact same cached figure - the raw object fields ignore
    SVFROMDATE/SVTODATE entirely. Ledger SELECTION itself (BELONGSTO/
    CHILDOF under Sundry Debtors) was separately confirmed correct even
    here - it found all 964 real debtor ledgers, including ones under
    custom sub-groups - which is why v5 below keeps it unchanged.

    v2: REPORTNAME-based "Trial Balance" export with SVCURRENTGROUP set
    to Sundry Debtors. PARTIALLY WORKING: the two dates returned
    genuinely different figures - but SVCURRENTGROUP had no scoping
    effect. The response was the whole company's Trial Balance at
    primary-group level; Sundry Debtors doesn't even appear as its own
    row there (nested inside Current Assets), and group totals aren't
    what a per-party cross-check needs anyway.

    v3: v1's ledger selection + a COMPUTE field using `$$ClosingBalance`.
    BROKEN WORSE: the CLOSINGBALANCE field vanished from the response
    entirely - `$$ClosingBalance` is not a recognized TDL system formula
    (most likely confused with UI terminology), so Tally silently
    dropped the whole field rather than erroring.

    v4: REPORTNAME "Group Summary" (the built-in screen shown when a
    human drills from Trial Balance into one specific group), target
    group passed via both SVVIEWNAME and SVCURRENTGROUP. BROKEN: byte-
    identical to v2's output - "Group Summary" as a REPORTNAME had no
    effect at all.

    v5 (current, client-supplied candidate - the one that worked): keeps
    v1's exact ledger selection but replaces `<FETCH>` with
    `<NATIVEMETHOD>` for each field, plus `ISFIXED="No"
    ISINITIALIZE="Yes"` on the COLLECTION. FETCH apparently just
    serializes whatever's already cached on the object (matching v1's
    proven-stale result); NATIVEMETHOD explicitly invokes the object's
    own native computation method, which genuinely reads the
    STATICVARIABLES period context instead of a cached attribute.

    One output-shape consequence worth remembering: a NATIVEMETHOD field
    renders using the exact case given in the request (e.g.
    ClosingBalance), unlike a FETCH field which always renders ALL CAPS
    (CLOSINGBALANCE) - parsers.parse_ledger_closing_balances was hardened
    with a case-insensitive lookup (parsers._find_ci) to handle both
    shapes, since the Manual Upload path's own real fixtures still use
    the all-caps form.
    """
    company = escape(company_name)
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>EXPORT</TALLYREQUEST>
  <TYPE>COLLECTION</TYPE>
  <ID>GroupLedgerTrialBalance</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE TYPE="Date">{_tally_date(fy_start)}</SVFROMDATE>
    <SVTODATE TYPE="Date">{_tally_date(as_of)}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="GroupLedgerTrialBalance" ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes">
      <TYPE>Ledger</TYPE>
      <CHILDOF>Sundry Debtors</CHILDOF>
      <BELONGSTO>Yes</BELONGSTO>
      <NATIVEMETHOD>Name</NATIVEMETHOD>
      <NATIVEMETHOD>Parent</NATIVEMETHOD>
      <NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
      <NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
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
