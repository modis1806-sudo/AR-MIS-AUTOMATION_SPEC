"""Round 2: A and B (flat fields) confirmed working with real voucher data.
C and D (mixing ALLLEDGERENTRIES.LIST on the same FETCH line as flat fields)
hang. Testing two hypotheses for why: (E) the LIST field needs its own
FETCH line, separate from flat fields; (F) this Tally version wants
LEDGERENTRIES.LIST instead of ALLLEDGERENTRIES.LIST for non-inventory
vouchers.

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

variants["E: ALLLEDGERENTRIES.LIST on its own FETCH line, separate from flat fields"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag E</ID>
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
     <COLLECTION NAME="ARMIS Diag E" ISINITIALIZE="Yes">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME</FETCH>
      <FETCH>ALLLEDGERENTRIES.LIST</FETCH>
      <FETCH>ALLLEDGERENTRIES.LEDGERNAME</FETCH>
      <FETCH>ALLLEDGERENTRIES.AMOUNT</FETCH>
      <FETCH>ALLLEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""

variants["F: LEDGERENTRIES.LIST instead of ALLLEDGERENTRIES.LIST, own line each"] = f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>ARMIS Diag F</ID>
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
     <COLLECTION NAME="ARMIS Diag F" ISINITIALIZE="Yes">
      <TYPE>Voucher</TYPE>
      <FETCH>DATE,VOUCHERNUMBER,VOUCHERTYPENAME</FETCH>
      <FETCH>LEDGERENTRIES.LIST</FETCH>
      <FETCH>LEDGERENTRIES.LEDGERNAME</FETCH>
      <FETCH>LEDGERENTRIES.AMOUNT</FETCH>
      <FETCH>LEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
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
        has_ledger = "LEDGERNAME" in raw or "AMOUNT" in raw
        print(f"RESULT: responded in time. Contains ledger/amount data: {has_ledger}")
        print(f"Length: {len(raw)} chars. First 2000 chars:\n{raw[:2000]}")
    except Exception as exc:
        print(f"RESULT: FAILED -- {exc}")
