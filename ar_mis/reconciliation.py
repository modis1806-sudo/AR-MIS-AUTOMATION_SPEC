"""Section 4.1 (party-level reconciliation) and 4.2 (YTD full-pull
cross-check).

Both checks operate at individual party level, never total/branch level
only - the spec is explicit that total-level checks are structurally
blind to offsetting errors (Party A overstated, Party B understated, net
zero at total), which is the exact failure class behind the original
Pre-MIS corruption. Branch/company rollups are for review, computed by
summing these per-party results - they are never computed independently
in a way that could pass while a party-level check fails.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ar_mis.models import Voucher
from ar_mis.money import difference, is_match
from ar_mis.rollforward import PartyMovement
from ar_mis.sign import flip_sign


@dataclass(frozen=True)
class PartyReconciliationResult:
    party_ledger_name: str
    branch_id: str
    closing_computed: Decimal
    closing_extracted: Decimal
    reconciled: bool
    difference: Decimal


def reconcile_party(
    party_ledger_name: str,
    branch_id: str,
    movement: PartyMovement,
    closing_extracted: Decimal,
) -> PartyReconciliationResult:
    """Section 4.1: extracted YTD Trial Balance closing per party vs
    Opening + Sales + CN + DN + Receipts + Journals computed from the
    appended registers for that party. Zero tolerance (client's Section 8
    decision) - is_match() is an exact comparison after both sides are
    rounded to paise, not a fuzzy/percentage comparison.
    """
    diff = difference(movement.closing_computed, closing_extracted)
    return PartyReconciliationResult(
        party_ledger_name=party_ledger_name,
        branch_id=branch_id,
        closing_computed=movement.closing_computed,
        closing_extracted=closing_extracted,
        reconciled=is_match(movement.closing_computed, closing_extracted),
        difference=diff,
    )


def reconcile_all_parties(
    branch_id: str,
    movements: dict[str, PartyMovement],
    closing_extracted_by_party: dict[str, Decimal],
) -> list[PartyReconciliationResult]:
    """Every party in `movements` must have a corresponding extracted
    closing balance - a party present in the appended build but missing
    from the YTD extract (or vice versa) is itself a reconciliation
    failure, not something to skip silently.
    """
    results = []
    all_parties = set(movements) | set(closing_extracted_by_party)
    for party in sorted(all_parties):
        if party not in movements:
            results.append(
                PartyReconciliationResult(
                    party_ledger_name=party,
                    branch_id=branch_id,
                    closing_computed=Decimal("0.00"),
                    closing_extracted=closing_extracted_by_party[party],
                    reconciled=False,
                    difference=difference(Decimal("0.00"), closing_extracted_by_party[party]),
                )
            )
            continue
        if party not in closing_extracted_by_party:
            results.append(
                PartyReconciliationResult(
                    party_ledger_name=party,
                    branch_id=branch_id,
                    closing_computed=movements[party].closing_computed,
                    closing_extracted=Decimal("0.00"),
                    reconciled=False,
                    difference=difference(movements[party].closing_computed, Decimal("0.00")),
                )
            )
            continue
        results.append(
            reconcile_party(party, branch_id, movements[party], closing_extracted_by_party[party])
        )
    return results


# ---- Section 4.2: YTD full-pull cross-check ---------------------------


@dataclass(frozen=True)
class DriftFinding:
    """One voucher line present in the fresh YTD pull but not found in
    the historical voucher_log - i.e. a voucher the original incremental
    weekly run(s) never captured (the canonical case: a backdated entry).
    `attributed_week` is the week_ending whose date range contains this
    voucher's date - the week the drift should be attributed to, per the
    spec's requirement to surface "which week and which voucher", not
    just that a mismatch exists somewhere.

    `voucher` is the FULL original Voucher (all entries - party, revenue,
    tax, round-off lines alike), not just this finding's own summary
    fields - added this session so a finding can actually be incorporated
    later (ar_mis.drift_correction) by replaying the exact same voucher
    through the normal register-building pipeline, rather than needing a
    second live Tally pull to reconstruct it from scratch.
    """

    party_ledger_name: str
    voucher_type: str
    voucher_number: str
    voucher_date: date
    flipped_amount: Decimal
    attributed_week: date | None
    voucher: Voucher


@dataclass(frozen=True)
class DriftFindingRecord:
    """A DriftFinding as persisted (Store.record_drift_findings) - found
    and fixed this session: previously a finding was only ever shown once,
    on the result page of the run that found it, then gone. `id` is the
    drift_finding table's own row id, needed to acknowledge or incorporate
    a specific finding later. `discovered_at` is the real wall-clock
    moment this finding was FIRST detected - a still-unresolved backdated
    voucher re-appears in isolate_drift's output on every subsequent
    extraction until it's actually incorporated, so
    Store.record_drift_findings only inserts a genuinely new finding once
    and leaves an already-recorded one alone, rather than spawning a
    fresh row every week it stays unresolved.

    Two distinct, independent signals, never conflated:
    `acknowledged` is a human saying "I've seen this" - an audit note
    only, it never touches weekly_snapshot or any register.
    `incorporated` is the real fix (ar_mis.drift_correction): the missing
    voucher has actually been written into the registers and
    weekly_snapshot, under a new correction week. Incorporating does NOT
    guarantee that party now reconciles clean going forward - it only
    means this specific missing voucher is now in the system; whether
    the party's books are fully clean is the TB Reconciliation
    Cross-Check sheet's job to show, not this flag's.
    """

    id: int
    branch_id: str
    finding: DriftFinding
    discovered_at: datetime
    acknowledged: bool
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    incorporated: bool
    incorporated_by: str | None
    incorporated_at: datetime | None
    incorporated_week_ending: date | None


def ytd_cross_check_party(
    party_ledger_name: str,
    latest_layer2_closing_computed: Decimal,
    fresh_ytd_closing_extracted: Decimal,
) -> bool:
    """True if the appended weekly build's latest cumulative closing
    agrees with a fresh from-day-1-of-FY pull. Per the confirmed
    source-of-truth rule, the YTD pull always wins on disagreement - this
    function only detects disagreement; it never adjusts the YTD figure
    to match the appended build.
    """
    return is_match(latest_layer2_closing_computed, fresh_ytd_closing_extracted)


def attribute_week(voucher_date: date, week_boundaries: list[date]) -> date | None:
    """`week_boundaries` is the sorted list of week_ending dates already
    on record for this party/branch. Returns the earliest week_ending
    that is >= voucher_date (i.e. the week whose 7-day window the voucher
    falls into), or None if the voucher postdates every known week (a
    genuinely new, not-yet-reported voucher rather than drift).
    """
    for week_ending in sorted(week_boundaries):
        if voucher_date <= week_ending:
            return week_ending
    return None


def isolate_drift(
    branch_id: str,
    fresh_ytd_vouchers: list[Voucher],
    party_ledger_names: set[str],
    logged_keys_by_party: dict[str, set[tuple[str, str, str, str]]],
    week_boundaries: list[date],
) -> list[DriftFinding]:
    """Diffs a fresh full-YTD voucher pull against everything previously
    logged for each party, surfacing exactly which voucher(s) were never
    part of any prior weekly run and which week they belong to - so the
    output is "Party X, week of 12-Jan, Sales voucher SB/0091, Rs 45,000
    backdated entry", never two conflicting totals for a human to
    arbitrate (spec's explicit requirement).
    """
    findings: list[DriftFinding] = []
    for voucher in fresh_ytd_vouchers:
        for entry in voucher.entries:
            if entry.party_ledger_name not in party_ledger_names:
                continue
            flipped = flip_sign(entry.amount_as_extracted)
            key = (
                voucher.voucher_type.value,
                voucher.voucher_number,
                voucher.voucher_date.isoformat(),
                str(flipped),
            )
            logged = logged_keys_by_party.get(entry.party_ledger_name, set())
            if key in logged:
                continue
            findings.append(
                DriftFinding(
                    party_ledger_name=entry.party_ledger_name,
                    voucher_type=voucher.voucher_type.value,
                    voucher_number=voucher.voucher_number,
                    voucher_date=voucher.voucher_date,
                    flipped_amount=flipped,
                    attributed_week=attribute_week(voucher.voucher_date, week_boundaries),
                    voucher=voucher,
                )
            )
    return findings
