"""Round 5: REPORTNAME=Day Book confirmed to ignore SVFROMDATE/SVTODATE
entirely (byte-identical response for two non-overlapping date ranges).
Testing several hypotheses for why, each against Range B (2026-09-05 to
2026-09-12) specifically, checking whether the response actually changes
from the known Range-A-only result (49 vouchers, all dated 20260401).

Usage: python diag_variants.py "Exact Company Name"
"""
import re
import sys
from datetime import date
from xml.sax.saxutils import escape

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient

company_name = sys.argv[1]
company = escape(company_name)
from_date, to_date = date(2026, 9, 5), date(2026, 9, 12)


def envelope(report_name, from_str, to_str, extra_static=""):
    return f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>{report_name}</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
     <SVFROMDATE>{from_str}</SVFROMDATE>
     <SVTODATE>{to_str}</SVTODATE>
     {extra_static}
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""


variants = {
    "H: Day Book, dates as DD-MMM-YYYY": envelope(
        "Day Book", from_date.strftime("%d-%b-%Y"), to_date.strftime("%d-%b-%Y")
    ),
    "I: Day Book, SVFROMDATE/SVTODATE with TYPE=Date attribute": f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Day Book</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
     <SVFROMDATE TYPE="Date">{from_date.strftime('%Y%m%d')}</SVFROMDATE>
     <SVTODATE TYPE="Date">{to_date.strftime('%Y%m%d')}</SVTODATE>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>""",
    "J: REPORTNAME=Voucher Register instead of Day Book": envelope(
        "Voucher Register", from_date.strftime("%Y%m%d"), to_date.strftime("%Y%m%d")
    ),
    "K: Day Book with explicit SVISDAYBOOK/EXPLODEFLAG": envelope(
        "Day Book", from_date.strftime("%Y%m%d"), to_date.strftime("%Y%m%d"),
        extra_static="<EXPLODEFLAG>Yes</EXPLODEFLAG>",
    ),
}

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 30.0

for label, body in variants.items():
    print(f"\n=== {label} ===")
    try:
        raw = client._post(body)
        voucher_numbers = re.findall(r"<VOUCHERNUMBER>([^<]*)</VOUCHERNUMBER>", raw)
        dates = sorted(set(re.findall(r"<DATE>(\d{8})</DATE>", raw)))
        print(f"Response length: {len(raw)} chars, voucher count: {len(voucher_numbers)}")
        print(f"Distinct DATE values: {dates[:10]}{' ...' if len(dates) > 10 else ''}")
        changed = dates != ["20260401"]
        print("CHANGED from the broken Range-A-only result!" if changed else "Still stuck on 20260401 -- no improvement.")
    except Exception as exc:
        print(f"RESULT: FAILED -- {exc}")
