"""The full month (Apr 1-30) hangs even at 180s -- not just slow, stuck,
same signature as the original Collection-type hang. Bisecting the month
in half to find out whether this is a data-volume threshold (both halves
succeed, only the combined total is too much) or a specific problem
voucher somewhere in the month (one half hangs, the other doesn't).

Usage: python diag_variants.py "Exact Company Name"
"""
import sys
import time
from datetime import date

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient
from ar_mis.xml_requests import voucher_export_request

company_name = sys.argv[1]

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 90.0

ranges = [
    ("First half: 2026-04-01 to 2026-04-15", date(2026, 4, 1), date(2026, 4, 15)),
    ("Second half: 2026-04-16 to 2026-04-30", date(2026, 4, 16), date(2026, 4, 30)),
]

for label, from_date, to_date in ranges:
    print(f"\n=== {label} ===")
    start = time.time()
    try:
        raw = client._post(voucher_export_request(company_name, from_date, to_date))
        elapsed = time.time() - start
        voucher_count = raw.count("<VOUCHERNUMBER>")
        print(f"SUCCEEDED after {elapsed:.1f}s. Length: {len(raw)} chars. Voucher count: {voucher_count}")
    except Exception as exc:
        elapsed = time.time() - start
        print(f"FAILED after {elapsed:.1f}s -- {exc}")
