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
from ar_mis.models import VoucherType
from ar_mis.parsers import parse_currently_loaded_companies
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
    return TallyClient(branch=branch, timeout_seconds=15.0)


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
    client = _client(args.host, args.port, args.company)
    to_date = date.fromisoformat(args.to) if args.to else date.today()
    from_date = date.fromisoformat(args.frm) if args.frm else to_date - timedelta(days=args.days)

    chunks = []
    for vtype in VoucherType:
        chunks.append(f"\n\n===== {vtype.value} =====\n\n")
        request = voucher_export_request(args.company, vtype, from_date, to_date)
        chunks.append(client._post(request))
    text = "".join(chunks)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Written to {args.output}")
    else:
        print(text)
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
