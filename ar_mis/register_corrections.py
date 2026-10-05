"""Audited corrections (Delete/Modify) overlaid on the three master
registers - docs/registers_and_reporting_design.md item 56, Phase 6b.

The client's explicit, repeated rejection of silent edit/delete of
original voucher data applies here exactly as it already does to drift
findings and Pre-MIS adjustments: a correction is never an UPDATE/DELETE
against a register row. It is a new, separate, audited row (who, when,
why) that every downstream total must consult - the register row itself
stays exactly as Tally/the pipeline originally built it, forever.

Two actions:
- EXCLUDE ("Delete" in the UI) - stop counting this register row in any
  total, as if it never existed for computation purposes. For an "Extra
  in Registers" row (a register entry Tally's own Sundry-Debtors list
  doesn't recognize), this is how you make the register agree with
  Tally's own independent list.
- CORRECT_AMOUNT ("Modify" in the UI) - replace what this voucher
  contributes with a Maker-entered figure, for an "Amount Mismatch" row
  where the register captured the wrong number. Entered in the SAME
  convention the register's own field already uses (SalesDNRegisterRow.
  invoice_value's always-positive gross total; CreditNoteRegisterRow.
  cn_amount / ReceiptJournalRegisterRow.amount's raw Tally-at-source
  signed figure) - a Maker is correcting "what the register should have
  said," not some separately-normalized figure.
- REINSTATE undoes a previous EXCLUDE/CORRECT_AMOUNT, reverting to the
  register's own raw figure - recorded as a new row too, never by
  deleting the correction it reverses, so the full history of who did
  what stays intact.

Corrections are identified by (branch_id, voucher_type, voucher_number,
party_id) - the exact same identity Check 1 (ar_mis.ytd_debtor_cross_
check) already uses, voucher-wise, never bill-wise. `voucher_type` is
always one of the five exact labels Check 1 and the registers already
use: "Sales", "Debit Note", "Credit Note", "Receipt", "Journal" - never
a type-group - so a correction can never silently leak across a sibling
type sharing the same register (e.g. a "Debit Note" correction never
touching a same-keyed "Sales" row's own figure).

The per-(branch, voucher_number, party) reduction below (`sales_dn_
amounts`/`cn_amounts`/`rj_amounts`) is shared, single-source-of-truth
infrastructure for both of this app's register-dependent checks (Check 1
and Check 2's `ar_mis.rollforward.aggregate_party_movements_from_
registers`) for exactly the reason this codebase has already hit three
times: two different pieces of code computing "the same per-voucher
total" independently, in slightly different shapes, is exactly how the
sign-convention trap keeps recurring. Everything here stays in each
register's own NATIVE, unflipped convention - converting to whichever
final convention a caller needs is each caller's own explicit job,
deliberately kept out of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ar_mis.models import CreditNoteRegisterRow, ReceiptJournalRegisterRow, SalesDNRegisterRow

EXCLUDE = "Exclude"
CORRECT_AMOUNT = "Correct Amount"
REINSTATE = "Reinstate"

_ACTIONS = (EXCLUDE, CORRECT_AMOUNT, REINSTATE)

SALES_DN_TYPES = frozenset({"Sales", "Debit Note"})
CREDIT_NOTE_TYPES = frozenset({"Credit Note"})
RECEIPT_JOURNAL_TYPES = frozenset({"Receipt", "Journal"})

RegisterKey = tuple[str, str, str]  # (branch_id, voucher_number, party_id)
_CorrectionKey = tuple[str, str, str, str]  # (branch_id, voucher_type, voucher_number, party_id)


@dataclass(frozen=True)
class RegisterRowCorrection:
    branch_id: str
    voucher_type: str
    voucher_number: str
    party_id: str
    action: str
    corrected_amount: Decimal | None
    reason: str
    corrected_by: str
    corrected_at: datetime

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError(f"Unknown register row correction action: {self.action!r}")
        if self.action == CORRECT_AMOUNT and self.corrected_amount is None:
            raise ValueError("Correct Amount requires a corrected_amount")


@dataclass(frozen=True)
class RegisterRowCorrectionRecord:
    """A RegisterRowCorrection as persisted (Store.record_register_row_
    correction) - `id` is the register_row_correction table's own row id,
    carried for display/audit purposes only (unlike drift_finding, there
    is no later action that looks a correction up by id)."""

    id: int
    correction: RegisterRowCorrection


def sales_dn_amounts(rows: list[SalesDNRegisterRow]) -> dict[RegisterKey, Decimal]:
    amounts: dict[RegisterKey, Decimal] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.invoice_value
    return amounts


def cn_amounts(rows: list[CreditNoteRegisterRow]) -> dict[RegisterKey, Decimal]:
    amounts: dict[RegisterKey, Decimal] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.cn_amount
    return amounts


def rj_amounts(rows: list[ReceiptJournalRegisterRow]) -> dict[RegisterKey, Decimal]:
    amounts: dict[RegisterKey, Decimal] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.amount
    return amounts


def effective_corrections(
    corrections: list[RegisterRowCorrection],
) -> dict[_CorrectionKey, RegisterRowCorrection]:
    """Reduces a full append-only history to one effective correction per
    identity - the most recently recorded one wins, the same latest-wins
    reduction this app already applies elsewhere (e.g. Store.all_ytd_
    debtor_voucher_rows' own as_of scoping). A REINSTATE is kept as the
    effective entry rather than dropped, so `apply_corrections` can tell
    "corrected back to raw" apart from "never corrected" if it ever needs
    to - today both behave identically (no override applied).
    """
    latest: dict[_CorrectionKey, RegisterRowCorrection] = {}
    for correction in sorted(corrections, key=lambda c: c.corrected_at):
        key = (correction.branch_id, correction.voucher_type, correction.voucher_number, correction.party_id)
        latest[key] = correction
    return latest


def apply_corrections(
    amounts: dict[RegisterKey, Decimal],
    voucher_types: frozenset[str],
    effective: dict[_CorrectionKey, RegisterRowCorrection],
) -> dict[RegisterKey, Decimal]:
    """Overlays `effective` onto `amounts` for only the corrections whose
    own voucher_type is in `voucher_types` - callers pass the one exact
    label(s) that this particular amounts dict actually represents, so a
    correction recorded under a sibling type/register can never apply
    here by accident.
    """
    result = dict(amounts)
    for (branch_id, voucher_type, voucher_number, party_id), correction in effective.items():
        if voucher_type not in voucher_types:
            continue
        key = (branch_id, voucher_number, party_id)
        if correction.action == EXCLUDE:
            result.pop(key, None)
        elif correction.action == CORRECT_AMOUNT:
            result[key] = correction.corrected_amount
        # REINSTATE: no override - the raw figure already in `amounts`
        # (if any) stands unchanged.
    return result
