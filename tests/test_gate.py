from datetime import date
from decimal import Decimal

from ar_mis.gate import evaluate_output_gate, may_auto_send
from ar_mis.orchestration import BranchRunOutcome, ExtractionOutcome, WeeklyCycleReport
from ar_mis.reconciliation import DriftFinding


def _pass(branch_id, branch_name):
    return BranchRunOutcome(branch_id, branch_name, ExtractionOutcome.PASS, "clean")


def _extraction_failed(branch_id, branch_name):
    return BranchRunOutcome(branch_id, branch_name, ExtractionOutcome.EXTRACTION_FAILED, "connection refused")


def test_clean_cycle_with_no_drift_passes_the_gate():
    report = WeeklyCycleReport(results=[_pass("KOL", "Kolkata"), _pass("DEL", "Delhi")])
    status = evaluate_output_gate(report, drift_findings=[])
    assert status.clean is True
    assert status.reasons == []
    assert may_auto_send(status) is True


def test_final_failed_branch_blocks_the_gate():
    report = WeeklyCycleReport(results=[_pass("KOL", "Kolkata"), _extraction_failed("DEL", "Delhi")])
    status = evaluate_output_gate(report, drift_findings=[])
    assert status.clean is False
    assert any("Delhi" in r for r in status.reasons)
    assert may_auto_send(status) is False


def test_failed_parties_block_the_gate_even_though_branch_still_passed():
    # Reversed this session: a reconciliation mismatch no longer halts
    # the run (ar_mis.orchestration), so this gate is now the only place
    # that turns it into "not clean" - the branch outcome itself is
    # still PASS (data was recorded), but failed_parties must still show
    # up as a reason.
    outcome = BranchRunOutcome("KOL", "Kolkata", ExtractionOutcome.PASS, "1 of 2 reconciled", ["Acme"])
    report = WeeklyCycleReport(results=[outcome])
    status = evaluate_output_gate(report, drift_findings=[])
    assert status.clean is False
    assert any("Acme" in r for r in status.reasons)
    assert may_auto_send(status) is False


def test_ytd_drift_blocks_the_gate_even_if_every_branch_passed():
    report = WeeklyCycleReport(results=[_pass("KOL", "Kolkata")])
    finding = DriftFinding(
        party_ledger_name="Acme",
        voucher_type="Sales",
        voucher_number="SB/999",
        voucher_date=date(2026, 1, 4),
        flipped_amount=Decimal("13000000.00"),
        attributed_week=date(2026, 1, 5),
    )
    status = evaluate_output_gate(report, drift_findings=[finding])
    assert status.clean is False
    assert any("SB/999" in r for r in status.reasons)
    assert may_auto_send(status) is False
