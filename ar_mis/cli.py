"""The script the firm's staff member actually runs each Monday
(Section 2.2). Ties together orchestration, extraction/reconciliation
(pipeline), the 4.2 YTD drift check, the 4.4 output gate, and reporting
into one weekly command.

NOTE (see README "Known limitation"): every module this composes has its
own unit tests; this composition itself is exercised in
tests/test_cli.py against a fake TallyClient, not a live Tally session.
Validate against a real branch (Section 5's required parallel run)
before removing any manual step.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date, timedelta

from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.gate import evaluate_output_gate
from ar_mis.orchestration import EscalationRequired, run_weekly_cycle
from ar_mis.pipeline import build_branch_runner
from ar_mis.reconciliation import isolate_drift
from ar_mis.reporting import generate_report
from ar_mis.storage import Store
from ar_mis.tally_client import TallyClient


def run(
    week_ending: date,
    db_path: str = "data/ar_mis.db",
    report_dir: str = "reports",
    branches: list[BranchConfig] | None = None,
    announce: Callable[[str], None] = print,
    confirm: Callable[[str], str] = input,
) -> int:
    from_date = week_ending - timedelta(days=6)
    store = Store(db_path)
    try:
        # Branch list is user-editable master data (Branch Master screen /
        # storage.list_branches), not a hardcoded Python list - `branches`
        # is only overridable here for tests.
        branches = branches if branches is not None else store.list_branches()
        if not branches:
            print("No branches configured. Add at least one in Branch Master before running.")
            return 1
        run_branch = build_branch_runner(store, week_ending, from_date, week_ending)
        try:
            cycle_report = run_weekly_cycle(branches, run_branch, announce=announce, confirm=confirm)
        except EscalationRequired as exc:
            print(f"HALTED: {exc}")
            print("Resolve the reconciliation failure before re-running this week's cycle.")
            return 1

        # Section 4.2: a separate full YTD pull per branch that completed
        # this cycle, diffed against everything ever logged, to catch
        # backdated entries an incremental weekly pull cannot see.
        drift_findings = []
        fy_start = financial_year_start(week_ending)
        branch_by_id = {b.branch_id: b for b in branches}
        for outcome in cycle_report.passed_branches:
            branch = branch_by_id[outcome.branch_id]
            client = TallyClient(branch=branch)
            ytd_vouchers_by_type = client.fetch_all_voucher_types(fy_start, week_ending)
            ytd_vouchers = [v for vs in ytd_vouchers_by_type.values() for v in vs]
            closing_extracted = client.fetch_ytd_sundry_debtors(fy_start, week_ending)
            party_names = set(closing_extracted)
            logged_keys = {p: store.logged_voucher_keys(branch.branch_id, p) for p in party_names}
            week_boundaries = store.all_week_endings()
            drift_findings.extend(
                isolate_drift(branch.branch_id, ytd_vouchers, party_names, logged_keys, week_boundaries)
            )

        gate_status = evaluate_output_gate(cycle_report, drift_findings)
        report_path = f"{report_dir}/AR_MIS_{week_ending.isoformat()}.xlsx"
        generate_report(
            store, week_ending, gate_status, cycle_report.final_failed_branches, drift_findings, report_path
        )

        print(f"Report written to {report_path}")
        print(f"Gate status: {'CLEAN' if gate_status.clean else 'NOT CLEAN'}")
        if not gate_status.clean:
            for reason in gate_status.reasons:
                print(f"  - {reason}")
            print("Report held pending manual sign-off; not auto-sent.")
        return 0 if gate_status.clean else 2
    finally:
        store.close()


if __name__ == "__main__":
    target_week = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    sys.exit(run(target_week))
