import pytest

from ar_mis.config import BranchConfig
from ar_mis.orchestration import (
    BranchRunOutcome,
    EscalationRequired,
    ExtractionOutcome,
    run_weekly_cycle,
)
from ar_mis.tally_client import CompanyMismatchError, TallyConnectionError

KOL = BranchConfig("KOL", "Kolkata", "Kolkata HQ")
DEL = BranchConfig("DEL", "Delhi", "Delhi Branch")
MUM = BranchConfig("MUM", "Mumbai", "Mumbai Branch")


def _pass_outcome(branch):
    return BranchRunOutcome(branch.branch_id, branch.branch_name, ExtractionOutcome.PASS, "clean")


def test_all_branches_pass_first_try():
    def run_branch(branch):
        return _pass_outcome(branch)

    report = run_weekly_cycle([KOL, DEL, MUM], run_branch, announce=lambda *_: None, confirm=lambda *_: "")
    assert {r.branch_id for r in report.passed_branches} == {"KOL", "DEL", "MUM"}
    assert report.final_failed_branches == []


def test_failed_branch_is_retried_at_end_and_succeeds():
    attempts = {"DEL": 0}

    def run_branch(branch):
        if branch.branch_id == "DEL":
            attempts["DEL"] += 1
            if attempts["DEL"] == 1:
                raise TallyConnectionError("connection refused")
        return _pass_outcome(branch)

    log = []
    report = run_weekly_cycle(
        [KOL, DEL, MUM], run_branch, announce=log.append, confirm=lambda *_: ""
    )
    # DEL failed on the first pass but must appear as PASS after the retry -
    # this is the client's explicit clarification: a skipped branch is
    # taken up again at the end, not just flagged and left out.
    assert attempts["DEL"] == 2
    ids = {r.branch_id for r in report.passed_branches}
    assert ids == {"KOL", "DEL", "MUM"}
    assert report.final_failed_branches == []
    # KOL and MUM must not have been re-run just because DEL was retried.
    assert any("retried" in line.lower() for line in log)


def test_branch_still_failing_on_retry_is_recorded_as_final_failure():
    def run_branch(branch):
        if branch.branch_id == "DEL":
            raise TallyConnectionError("connection refused")
        return _pass_outcome(branch)

    report = run_weekly_cycle([KOL, DEL, MUM], run_branch, announce=lambda *_: None, confirm=lambda *_: "")
    assert {r.branch_id for r in report.final_failed_branches} == {"DEL"}
    # KOL and MUM still complete normally despite DEL's persistent failure.
    assert {r.branch_id for r in report.passed_branches} == {"KOL", "MUM"}


def test_company_mismatch_is_treated_as_extraction_failure_not_a_crash():
    def run_branch(branch):
        if branch.branch_id == "DEL":
            raise CompanyMismatchError("Delhi Branch", ["Kolkata HQ"])
        return _pass_outcome(branch)

    report = run_weekly_cycle([KOL, DEL], run_branch, announce=lambda *_: None, confirm=lambda *_: "")
    assert {r.branch_id for r in report.final_failed_branches} == {"DEL"}


def test_reconciliation_fail_halts_the_run_immediately():
    def run_branch(branch):
        if branch.branch_id == "DEL":
            return BranchRunOutcome(
                "DEL", "Delhi", ExtractionOutcome.RECON_FAIL, "1 party did not reconcile", ["Acme"]
            )
        return _pass_outcome(branch)

    with pytest.raises(EscalationRequired) as excinfo:
        run_weekly_cycle([KOL, DEL, MUM], run_branch, announce=lambda *_: None, confirm=lambda *_: "")
    assert excinfo.value.outcome.branch_id == "DEL"
    # MUM (queued after DEL) must never have been attempted.


def test_reconciliation_fail_does_not_process_branches_after_it():
    processed = []

    def run_branch(branch):
        processed.append(branch.branch_id)
        if branch.branch_id == "DEL":
            return BranchRunOutcome("DEL", "Delhi", ExtractionOutcome.RECON_FAIL, "fail", ["Acme"])
        return _pass_outcome(branch)

    with pytest.raises(EscalationRequired):
        run_weekly_cycle([KOL, DEL, MUM], run_branch, announce=lambda *_: None, confirm=lambda *_: "")
    assert processed == ["KOL", "DEL"]
    assert "MUM" not in processed
