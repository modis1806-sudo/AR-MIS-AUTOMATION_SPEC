"""Checks whether the Day Book Export Data request actually respects
SVFROMDATE/SVTODATE, or silently ignores them and returns whatever Tally's
own UI has as its currently-set period. Requests two genuinely
non-overlapping date ranges and compares the actual voucher numbers/dates
returned for each.

Usage: python diag_variants.py "Exact Company Name"
"""
import re
import sys
from datetime import date

from ar_mis.config import BranchConfig
from ar_mis.tally_client import TallyClient
from ar_mis.xml_requests import voucher_export_request

company_name = sys.argv[1]

client = TallyClient(branch=BranchConfig(
    branch_id="_diag", branch_name="_diag",
    tally_company_name=company_name, tally_host="localhost", tally_port=9000,
))
client.timeout_seconds = 30.0

ranges = [
    ("Range A: 2026-04-01 to 2026-04-01", date(2026, 4, 1), date(2026, 4, 1)),
    ("Range B: 2026-09-05 to 2026-09-12", date(2026, 9, 5), date(2026, 9, 12)),
]

results = {}
for label, from_date, to_date in ranges:
    print(f"\n=== {label} ===")
    body = voucher_export_request(company_name, from_date, to_date)
    raw = client._post(body)
    voucher_numbers = re.findall(r"<VOUCHERNUMBER>([^<]*)</VOUCHERNUMBER>", raw)
    dates = sorted(set(re.findall(r"<DATE>(\d{8})</DATE>", raw)))
    print(f"Response length: {len(raw)} chars")
    print(f"Voucher count: {len(voucher_numbers)}")
    print(f"Distinct DATE values found in response: {dates[:10]}{' ...' if len(dates) > 10 else ''}")
    print(f"First 5 voucher numbers: {voucher_numbers[:5]}")
    results[label] = (len(voucher_numbers), tuple(voucher_numbers))

print("\n=== COMPARISON ===")
labels = list(results.keys())
if results[labels[0]] == results[labels[1]]:
    print("IDENTICAL results for two different date ranges -- SVFROMDATE/SVTODATE is being IGNORED. Real bug.")
else:
    print("Results DIFFER between date ranges -- the date range IS being respected correctly.")
