"""Section 4.4: the output gate.

By the time evaluate_output_gate() runs, a WeeklyCycleReport already
exists - which means no branch posted a Section 4.1 reconciliation FAIL
(that raises EscalationRequired and aborts the cycle before any report is
built; see ar_mis.orchestration). This function catches everything else
that Section 4 requires but a clean per-branch reconciliation pass
doesn't by itself guarantee:

  - a branch that never got extracted at all after retry (Section 4.3
    final failure) - its parties are simply absent from the week's data,
    which is a gap the report must not paper over as "all good".
  - Section 4.2 YTD full-pull drift - a check that can fail even when
    every party's incremental weekly movement tied out this week,
    because it specifically catches backdated entries an incremental
    pull structurally cannot see.

"Never a silent pass-through" (spec's words): may_auto_send() is the one
function anything resembling an auto-email step is allowed to gate on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ar_mis.orchestration import WeeklyCycleReport
from ar_mis.reconciliation import DriftFinding


@dataclass(frozen=True)
class GateStatus:
    clean: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_output_gate(
    cycle_report: WeeklyCycleReport,
    drift_findings: list[DriftFinding] | None = None,
) -> GateStatus:
    reasons: list[str] = []

    for branch in cycle_report.final_failed_branches:
        reasons.append(
            f"Branch '{branch.branch_name}' failed extraction after retry and is "
            f"excluded from this week's data: {branch.detail}"
        )

    for finding in drift_findings or []:
        week_desc = finding.attributed_week.isoformat() if finding.attributed_week else "an unassigned week"
        reasons.append(
            f"YTD cross-check drift: {finding.party_ledger_name} - "
            f"{finding.voucher_type} {finding.voucher_number} dated {finding.voucher_date.isoformat()} "
            f"was not captured in the original run for the week of {week_desc}"
        )

    return GateStatus(clean=not reasons, reasons=reasons)


def may_auto_send(gate_status: GateStatus) -> bool:
    """The report/email does not go out automatically unless the gate is
    clean. A non-clean gate means the report must be generated with the
    UNVALIDATED flag (ar_mis.reporting) and held for manual sign-off
    rather than sent - the spec explicitly rejects a silent pass-through,
    and "generate nothing" is its own kind of silent failure, so the
    report is still produced, just not auto-distributed.
    """
    return gate_status.clean
