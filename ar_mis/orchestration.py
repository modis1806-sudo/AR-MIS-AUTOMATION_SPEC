"""Section 2.2: human-in-the-loop multi-branch orchestration.

Two distinct failure modes get two distinct responses, per the spec:

1. Technical extraction failure (Tally unreachable, malformed XML,
   wrong company loaded) - Section 4.3 / Section 8 item 2, resolved with
   the client as: skip that branch, flag it clearly, continue with the
   rest, then retry the failed branch(es) once at the end of the run
   (client's explicit follow-up instruction). Only a branch that still
   fails on that retry pass is recorded as a final FAILED branch for the
   week.

2. A reconciliation gate FAIL (Section 4.1) - the branch extracted fine,
   but its data didn't tie out for one or more parties. Section 2.2 is
   explicit here: "operator's only decision: green -> proceed, red ->
   stop and escalate. No interpretive judgment calls." This is not a
   skip-and-continue case - it halts the run so bad data can't roll
   forward into other branches' consolidated view while unresolved.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ar_mis.config import BranchConfig
from ar_mis.tally_client import CompanyMismatchError, TallyConnectionError


class ExtractionOutcome(str, Enum):
    PASS = "PASS"
    RECON_FAIL = "RECON_FAIL"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


@dataclass(frozen=True)
class BranchRunOutcome:
    branch_id: str
    branch_name: str
    outcome: ExtractionOutcome
    detail: str
    failed_parties: list[str] = field(default_factory=list)
    new_parties: list[str] = field(default_factory=list)
    """Parties auto-created this run because extraction found them (in the
    Sundry Debtors YTD pull) with no existing customer_master record.
    Populated even on a PASS outcome - a new customer isn't a failure,
    but it's worth a human noticing, so it's still surfaced separately in
    the report's Exceptions section rather than blended into Party Detail.
    """
    register_build_exceptions: list[tuple[str, str]] = field(default_factory=list)
    """(voucher_number, reason) pairs this run's registers.py build step
    could not confidently place into a register (registers.
    RegisterBuildExceptions) - e.g. a voucher with no PARTYLEDGERNAME.
    Populated only on a PASS outcome, since registers are only built
    once reconciliation clears (see pipeline.process_branch_data).
    """


class EscalationRequired(Exception):
    """Raised to halt the weekly cycle immediately on a Section 4.1
    reconciliation FAIL, per Section 2.2's "red -> stop and escalate."
    Carries the offending branch's outcome so the caller can surface it
    without re-deriving anything.
    """

    def __init__(self, outcome: BranchRunOutcome):
        self.outcome = outcome
        super().__init__(
            f"Branch '{outcome.branch_name}' failed reconciliation for "
            f"{len(outcome.failed_parties)} part(y/ies): {outcome.detail}"
        )


@dataclass
class WeeklyCycleReport:
    results: list[BranchRunOutcome]

    @property
    def final_failed_branches(self) -> list[BranchRunOutcome]:
        return [r for r in self.results if r.outcome == ExtractionOutcome.EXTRACTION_FAILED]

    @property
    def passed_branches(self) -> list[BranchRunOutcome]:
        return [r for r in self.results if r.outcome == ExtractionOutcome.PASS]


def run_weekly_cycle(
    branches: list[BranchConfig],
    run_branch: Callable[[BranchConfig], BranchRunOutcome],
    announce: Callable[[str], None] = print,
    confirm: Callable[[str], str] = input,
    max_retry_passes: int = 1,
) -> WeeklyCycleReport:
    """Drives the branch-by-branch loop. `run_branch` is the caller's
    extraction+reconciliation pipeline for one branch (see
    build_branch_runner) - kept as an injected callable so this
    control-flow logic is testable without a live Tally connection or a
    real database.

    Raises EscalationRequired the instant any branch posts a
    reconciliation FAIL - remaining branches (including anything still
    in the retry queue) are left unprocessed, matching Section 2.2.
    """
    results: dict[str, BranchRunOutcome] = {}
    pending = list(branches)
    attempt = 1

    while pending:
        is_final_pass = attempt > max_retry_passes
        if attempt > 1:
            announce("--- Retry pass: revisiting branch(es) that failed extraction ---")
        next_retry: list[BranchConfig] = []

        for branch in pending:
            confirm(f"Open [{branch.branch_name}] in Tally, then press Enter to continue.")
            try:
                outcome = run_branch(branch)
            except (TallyConnectionError, CompanyMismatchError) as exc:
                announce(f"[{branch.branch_name}] EXTRACTION FAILED: {exc}")
                if is_final_pass:
                    results[branch.branch_id] = BranchRunOutcome(
                        branch_id=branch.branch_id,
                        branch_name=branch.branch_name,
                        outcome=ExtractionOutcome.EXTRACTION_FAILED,
                        detail=str(exc),
                    )
                else:
                    announce(f"[{branch.branch_name}] will be retried at the end of this run.")
                    next_retry.append(branch)
                continue

            results[branch.branch_id] = outcome
            if outcome.outcome == ExtractionOutcome.RECON_FAIL:
                announce(f"[{branch.branch_name}] FAIL - {outcome.detail}")
                raise EscalationRequired(outcome)
            announce(f"[{branch.branch_name}] PASS")

        pending = next_retry
        attempt += 1

    return WeeklyCycleReport(results=list(results.values()))
