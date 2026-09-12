"""One-off diagnostic: try progressively more complex voucher Collection
requests against a real Tally instance to find exactly which piece causes
the silent hang seen with the full voucher_export_request().

Usage: python diag_variants.py "Exact Company Name" 2026-04-01
Run from inside the project folder (same place you ran pip install -e .).
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

variants["A: flat fields only, ISINITIALIZE (matches working company-list style)"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag A</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{d}</SVFROMDATE>
    <SVTODATE>{d}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Diag A" ISINITIALIZE="Yes">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

variants["B: same flat fields, but ISMODIFY=No (matches current code's attribute)"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag B</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{d}</SVFROMDATE>
    <SVTODATE>{d}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Diag B" ISMODIFY="No">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

variants["C: adds ALLLEDGERENTRIES.LIST (no bill allocations yet)"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag C</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{d}</SVFROMDATE>
    <SVTODATE>{d}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Diag C" ISINITIALIZE="Yes">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME,ALLLEDGERENTRIES.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.LEDGERNAME,ALLLEDGERENTRIES.AMOUNT,ALLLEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

variants["D: full original request (BILLALLOCATIONS.LIST added) -- expected to hang, included for comparison"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag D</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{d}</SVFROMDATE>
    <SVTODATE>{d}</SVTODATE>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="ARMIS Diag D" ISINITIALIZE="Yes">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME,ALLLEDGERENTRIES.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.LEDGERNAME,ALLLEDGERENTRIES.AMOUNT,ALLLEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
      <FETCH>ALLLEDGERENTRIES.BILLALLOCATIONS.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.BILLALLOCATIONS.NAME,ALLLEDGERENTRIES.BILLALLOCATIONS.AMOUNT</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 15.0

for label, body in variants.items():
    print(f"\n=== {label} ===")
    try:
        raw = client._post(body)
        print(f"RESULT: responded in time. First 300 chars:\n{raw[:300]}")
    except Exception as exc:
        print(f"RESULT: FAILED -- {exc}")
