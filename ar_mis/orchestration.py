"""Section 2.2: the per-branch run outcome shape shared across this app's
real paths - a live webapp Extract & Save, a Manual Upload, and
ar_mis.pipeline.process_branch_data, which both of those call.

Technical extraction failure (Tally unreachable, malformed XML, wrong
company loaded) - Section 4.3 / Section 8 item 2 - is reported as
ExtractionOutcome.EXTRACTION_FAILED for that one branch; the operator
retries it themselves from the webapp (there is no automated multi-
branch retry queue here - that was ar_mis.cli's own run_weekly_cycle,
removed once the client confirmed the webapp, not that standalone
script, is the only path ever actually used in practice).

A reconciliation gate FAIL (Section 4.1) - one or more parties' data
didn't tie out - used to be a second, distinct failure mode here: the
original spec's explicit rule was "operator's only decision: green ->
proceed, red -> stop and escalate," halting the entire weekly cycle so
unresolved data couldn't roll forward. **Reversed this session, client's
explicit instruction: data must never be discarded, and one party's
mismatch must never block every other party in the branch from having
its own good data recorded.** ar_mis.pipeline.process_branch_data now
always writes and always returns PASS; a party that didn't reconcile is
reported via BranchRunOutcome.failed_parties instead of halting
anything - the webapp's own combined Cross-Check banner is what now
flags it, a report is still produced, just held for manual sign-off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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


