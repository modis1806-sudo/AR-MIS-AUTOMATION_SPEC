"""Section 2.2: human-in-the-loop multi-branch orchestration.

Technical extraction failure (Tally unreachable, malformed XML, wrong
company loaded) - Section 4.3 / Section 8 item 2, resolved with the
client as: skip that branch, flag it clearly, continue with the rest,
then retry the failed branch(es) once at the end of the run (client's
explicit follow-up instruction). Only a branch that still fails on that
retry pass is recorded as a final FAILED branch for the week.

A reconciliation gate FAIL (Section 4.1) - one or more parties' data
didn't tie out - used to be a second, distinct failure mode here: the
original spec's explicit rule was "operator's only decision: green ->
proceed, red -> stop and escalate," halting the entire weekly cycle so
unresolved data couldn't roll forward. **Reversed this session, client's
explicit instruction: data must never be discarded, and one party's
mismatch must never block every other party in the branch (or every
other branch in the run) from having its own good data recorded.**
ar_mis.pipeline.process_branch_data now always writes and always returns
PASS; a party that didn't reconcile is reported via
BranchRunOutcome.failed_parties instead of halting anything, and
ar_mis.gate.evaluate_output_gate is what now flags it as a reason the
week isn't "clean" - a report is still produced, just held for manual
sign-off, exactly like every other non-clean condition it already
handled.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ar_mis.config import BranchConfig
from ar_mis.tally_client import CompanyMismatchError, TallyConnectionError


class ExtractionOutcome(str, Enum):
    PASS = "PASS"
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
    register_build_exceptions: list[tuple[str, str, str, str]] = field(default_factory=list)
    """(voucher_number, party_ledger_name, voucher_type, reason) 4-tuples
    this run's registers.py build step could not confidently place into a
    register (registers.RegisterBuildExceptions) - e.g. a voucher with no
    PARTYLEDGERNAME. party_ledger_name is "" when the voucher itself
    carries no usable party hint.
    """


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

    A branch whose data didn't fully reconcile no longer halts anything
    (see this module's own docstring for the reversal) - every branch in
    `branches` is always attempted, PASS-with-failed_parties included;
    only a genuine technical extraction failure (Tally unreachable, wrong
    company) that still fails after the retry pass is excluded from the
    week's results.
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
            if outcome.failed_parties:
                announce(
                    f"[{branch.branch_name}] PASS WITH EXCEPTIONS - {outcome.detail} "
                    "(data recorded; see the TB Reconciliation Cross-Check sheet)"
                )
            else:
                announce(f"[{branch.branch_name}] PASS")

        pending = next_retry
        attempt += 1

    return WeeklyCycleReport(results=list(results.values()))
