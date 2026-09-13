"""Standalone diagnostic commands for troubleshooting a live Tally
connection directly from the command line.

This is the generalized, committed form of one-off scripts hand-built
during this project's first real-Tally test session (check_company.py,
check_vouchers.py, check_data.py) - those lived only on the test server
and were never in the repo. They earned their place here: Tally's XML
responses vary enough between versions, editions, and deployment setups
(a standalone Tally Gateway Server service, in this project's case) that
"what does it actually say" beat every guess made without looking.

Usage:
    python -m ar_mis.diagnostics companies --host localhost --port 9000 [--raw]
    python -m ar_mis.diagnostics vouchers --company "NAME" [--host H] [--port P] [--days N] [--output FILE]
    python -m ar_mis.diagnostics ledgers --company "NAME" [--host H] [--port P] [--fy-start YYYY-MM-DD] [--as-of YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.parsers import parse_currently_loaded_companies, parse_voucher_collection
from ar_mis.tally_client import TallyClient
from ar_mis.xml_requests import (
    list_of_companies_request,
    voucher_export_request,
    ytd_sundry_debtors_request,
)


def _client(host: str, port: int, company: str = "") -> TallyClient:
    branch = BranchConfig(
        branch_id="_diag", branch_name="_diag", tally_company_name=company, tally_host=host, tally_port=port
    )
    # Uses TallyClient's default timeout (tally_client.DEFAULT_TIMEOUT_SECONDS)
    # rather than a short fixed value - a real diagnostic run against a
    # non-trivial range needs the same headroom production code does.
    return TallyClient(branch=branch)


def cmd_companies(args: argparse.Namespace) -> int:
    client = _client(args.host, args.port)
    raw = client._post(list_of_companies_request())
    if args.raw:
        print(raw)
        print()
    companies = parse_currently_loaded_companies(raw)
    if companies:
        print("Currently open:")
        for name in companies:
            print(f"  - {name}")
    else:
        print("No companies currently open.")
    return 0


def cmd_vouchers(args: argparse.Namespace) -> int:
    """One request now, not five - see xml_requests.voucher_export_request.
    Prints/saves the raw response, then the categorized breakdown so you
    can see both what Tally actually said and what this codebase made of
    it (categorize_voucher_type in parsers.py).
    """
    client = _client(args.host, args.port, args.company)
    to_date = date.fromisoformat(args.to) if args.to else date.today()
    from_date = date.fromisoformat(args.frm) if args.frm else to_date - timedelta(days=args.days)

    raw = client._post(voucher_export_request(args.company, from_date, to_date))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"Raw response written to {args.output}")
    else:
        print(raw)

    counts: dict[str, int] = {}
    for voucher in parse_voucher_collection(raw, branch_id="_diag"):
        counts[voucher.voucher_type.value] = counts.get(voucher.voucher_type.value, 0) + 1
    print("\nCategorized as AR-relevant:")
    if counts:
        for category, count in counts.items():
            print(f"  - {category}: {count}")
    else:
        print("  (none)")
    return 0


def cmd_ledgers(args: argparse.Namespace) -> int:
    client = _client(args.host, args.port, args.company)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    fy_start = date.fromisoformat(args.fy_start) if args.fy_start else financial_year_start(as_of)
    request = ytd_sundry_debtors_request(args.company, fy_start, as_of)
    print(client._post(request))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose a live Tally XML/HTTP connection")
    sub = parser.add_subparsers(dest="command", required=True)

    p_companies = sub.add_parser("companies", help="List companies currently open at the gateway")
    p_companies.add_argument("--host", default="localhost")
    p_companies.add_argument("--port", type=int, default=9000)
    p_companies.add_argument("--raw", action="store_true", help="Also print the raw XML response")
    p_companies.set_defaults(func=cmd_companies)

    p_vouchers = sub.add_parser("vouchers", help="Dump raw voucher extraction responses for all 5 types")
    p_vouchers.add_argument("--company", required=True)
    p_vouchers.add_argument("--host", default="localhost")
    p_vouchers.add_argument("--port", type=int, default=9000)
    p_vouchers.add_argument("--days", type=int, default=7, help="Look-back window if --from is not given")
    p_vouchers.add_argument("--from", dest="frm", help="YYYY-MM-DD")
    p_vouchers.add_argument("--to", help="YYYY-MM-DD")
    p_vouchers.add_argument("--output", help="Write to this file instead of stdout")
    p_vouchers.set_defaults(func=cmd_vouchers)

    p_ledgers = sub.add_parser("ledgers", help="Dump the raw Sundry Debtors YTD collection response")
    p_ledgers.add_argument("--company", required=True)
    p_ledgers.add_argument("--host", default="localhost")
    p_ledgers.add_argument("--port", type=int, default=9000)
    p_ledgers.add_argument("--fy-start", dest="fy_start", help="YYYY-MM-DD, defaults to the current FY's April 1")
    p_ledgers.add_argument("--as-of", dest="as_of", help="YYYY-MM-DD, defaults to today")
    p_ledgers.set_defaults(func=cmd_ledgers)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
