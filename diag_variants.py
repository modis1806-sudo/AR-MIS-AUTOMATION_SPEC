"""Round 3: E and F (separating the LIST fetch, trying the other field name)
both hang identically to C/D. The problem isn't field-line syntax -- nested
list fetches on a Collection-type Voucher request seem to be broadly blocked
on this Tally build.

But we have direct proof Tally CAN produce this data fast: the manual
Day Book -> Alt+E -> Export XML test completed in under a second. That's a
REPORTNAME-based "Export Data" request, a different mechanism from the
Collection-type TDL request used everywhere else in this codebase. Testing
that mechanism directly.

Usage: python diag_variants.py "Exact Company Name" 2026-04-01
"""
import sys
from datetime import date
from xml.sax.saxutils import escape

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient

company_name = sys.argv[1]
day = date.fromisoformat(sys.argv[2])
d = day.strftime("%Y%m%d")
company = escape(company_name)

variants = {}

variants["G: REPORTNAME=Day Book, Export Data request (mirrors the manual Alt+E test)"] = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Day Book</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
     <SVFROMDATE>{d}</SVFROMDATE>
     <SVTODATE>{d}</SVTODATE>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 20.0

for label, body in variants.items():
    print(f"\n=== {label} ===")
    try:
        raw = client._post(body)
        has_ledger = "LEDGERNAME" in raw or "AMOUNT" in raw or "BILLALLOCATIONS" in raw
        print(f"RESULT: responded in time. Contains ledger/amount/bill data: {has_ledger}")
        print(f"Length: {len(raw)} chars. First 3000 chars:\n{raw[:3000]}")
    except Exception as exc:
        print(f"RESULT: FAILED -- {exc}")
