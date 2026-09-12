"""Round 6: Voucher Register correctly respects SVFROMDATE/SVTODATE.
Confirming it also still carries full ledger-entry and bill-allocation
detail (the reason Day Book was chosen in the first place), same as
round 4 checked for Day Book.

Usage: python diag_variants.py "Exact Company Name"
"""
import sys
from datetime import date
from xml.sax.saxutils import escape

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient

company_name = sys.argv[1]
company = escape(company_name)
from_date, to_date = date(2026, 9, 5), date(2026, 9, 12)

body = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Voucher Register</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
     <SVFROMDATE>{from_date.strftime('%Y%m%d')}</SVFROMDATE>
     <SVTODATE>{to_date.strftime('%Y%m%d')}</SVTODATE>
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
client.timeout_seconds = 30.0

raw = client._post(body)
print(f"Total length: {len(raw)} chars\n")

for tag in ["ALLLEDGERENTRIES.LIST", "BILLALLOCATIONS.LIST", "PARTYLEDGERNAME", "LEDGERNAME", "VOUCHERTYPENAME"]:
    idx = raw.find(f"<{tag}")
    if idx == -1:
        print(f"--- <{tag}> NOT FOUND anywhere in the response ---\n")
    else:
        print(f"--- First <{tag}> found at position {idx}, showing 800 chars from there: ---")
        print(raw[idx:idx + 800])
        print()
