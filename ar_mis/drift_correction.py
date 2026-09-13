"""The correction mechanism for a backdated entry - client's explicit
ask this session, deferred from the drift-persistence round until it
could be designed properly (design doc item 18).

"Incorporating" a finding means: take its original voucher (persisted
whole - see DriftFinding's own docstring) and run it through the exact
same pipeline a normal week's extraction uses
(ar_mis.pipeline.process_branch_data), under a NEW week_ending the Maker
chooses. This is deliberately NOT an edit to any existing, already-locked
weekly_snapshot row - that would break the append-only principle this
entire codebase is built on ("a correction is a new row for a later
week", per ar_mis.storage's own module docstring). Instead, the missing
voucher becomes a new row, dated with its own real historical
voucher_date (so ageing/DSO still treat it correctly), but recorded
under the correction week - the same "prior period adjustment" pattern
real accounting uses: you don't reopen a closed period, you post the
correction now, referencing what it's correcting.

This still requires a real, Tally-sourced closing balance for that one
party as of the correction week - typed in by the Maker (the same trust
level as Manual Upload's own Trial Balance file: human-provided, but
must come from Tally, never invented) - because reconciliation is not
suspended for a correction. If the figure the Maker enters doesn't
fully explain the gap, that's not hidden: the party will still show a
difference on the TB Reconciliation Cross-Check sheet afterward, exactly
as it would for any other mismatch. Incorporating only claims "this
specific missing voucher is now in the system" - never "this party's
books are now clean".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ar_mis.orchestration import BranchRunOutcome
from ar_mis.pipeline import process_branch_data
from ar_mis.reconciliation import DriftFindingRecord
from ar_mis.storage import Store


class DriftFindingAlreadyIncorporated(Exception):
    """Refuses a second incorporation attempt for the same finding - the
    voucher is already in the system under a prior correction week."""


class DriftFindingCorrectionWeekConflict(Exception):
    """Refuses when this party already has a recorded position for the
    chosen correction week_ending - pick a different date rather than
    silently colliding with an existing row (weekly_snapshot's own
    append-only uniqueness, surfaced here as a clear message instead of
    a raw database error).
    """


@dataclass
class DriftFindingCorrection:
    outcome: BranchRunOutcome
    week_ending: date


def incorporate_drift_finding(
    store: Store,
    branch_id: str,
    branch_name: str,
    finding_record: DriftFindingRecord,
    week_ending: date,
    party_closing_extracted: Decimal,
    incorporated_by: str,
    incorporated_at: datetime,
) -> DriftFindingCorrection:
    """Replays `finding_record`'s original voucher into the registers and
    weekly_snapshot under `week_ending`, then marks the finding
    incorporated. `party_closing_extracted` is that ONE party's Tally
    Sundry Debtors closing balance as of `week_ending` - the ground
    truth process_branch_data's own zero-tolerance check reconciles
    against, exactly like every other week.
    """
    if finding_record.incorporated:
        raise DriftFindingAlreadyIncorporated(
            f"Finding {finding_record.id} was already incorporated on "
            f"{finding_record.incorporated_at} under week ending {finding_record.incorporated_week_ending}."
        )

    party_id = finding_record.finding.party_ledger_name
    if store.has_weekly_snapshot_for_party_week(party_id, branch_id, week_ending):
        raise DriftFindingCorrectionWeekConflict(
            f"'{party_id}' already has a recorded position for the week ending {week_ending.isoformat()}. "
            "Choose a different date for this correction."
        )

    outcome = process_branch_data(
        store, branch_id, branch_name, week_ending,
        [finding_record.finding.voucher], {party_id: party_closing_extracted},
    )
    store.mark_drift_finding_incorporated(finding_record.id, incorporated_by, incorporated_at, week_ending)
    return DriftFindingCorrection(outcome=outcome, week_ending=week_ending)
