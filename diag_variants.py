"""Round 4: G worked (real data, in under a second) but the previous run's
saved file only captured the print()'s 3000-char preview, not the full 5.4
million character response -- so we haven't actually seen the ledger/bill
allocation detail yet. This version finds and prints a window around the
first ALLLEDGERENTRIES.LIST and BILLALLOCATIONS.LIST occurrences directly,
instead of transferring the whole response.

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

body = f"""<ENVELOPE>
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
client.timeout_seconds = 30.0

raw = client._post(body)
print(f"Total length: {len(raw)} chars\n")

# Also save the full thing to a file, in case we need it later.
with open("full_dump.xml", "w", encoding="utf-8") as f:
    f.write(raw)
print("Full response saved to full_dump.xml\n")

for tag in ["ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST", "BILLALLOCATIONS.LIST", "PARTYLEDGERNAME", "LEDGERNAME"]:
    idx = raw.find(f"<{tag}")
    if idx == -1:
        print(f"--- <{tag}> NOT FOUND anywhere in the response ---\n")
    else:
        print(f"--- First <{tag}> found at position {idx}, showing 1500 chars from there: ---")
        print(raw[idx:idx + 1500])
        print()
