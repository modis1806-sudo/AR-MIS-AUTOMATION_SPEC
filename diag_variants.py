"""Checks whether the April 2026 full-month timeout is genuinely just slow
(needs more than 30 seconds for a real company's data volume) or actually
stuck indefinitely, by retrying the same request with a much longer
timeout and timing how long it actually takes.

Usage: python diag_variants.py "Exact Company Name"
"""
import sys
import time
from datetime import date

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient
from ar_mis.xml_requests import voucher_export_request

company_name = sys.argv[1]
from_date, to_date = date(2026, 4, 1), date(2026, 4, 30)

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 180.0

print(f"Requesting {from_date} to {to_date} with a 180 second timeout...")
start = time.time()
try:
    raw = client._post(voucher_export_request(company_name, from_date, to_date))
    elapsed = time.time() - start
    voucher_count = raw.count("<VOUCHERNUMBER>")
    print(f"SUCCEEDED after {elapsed:.1f} seconds. Response length: {len(raw)} chars. Voucher count: {voucher_count}")
except Exception as exc:
    elapsed = time.time() - start
    print(f"FAILED after {elapsed:.1f} seconds -- {exc}")
