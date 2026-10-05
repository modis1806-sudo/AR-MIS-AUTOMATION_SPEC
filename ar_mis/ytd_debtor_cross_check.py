"""Section 4.2's "TB vs Registers" cross-check (docs/registers_and_
reporting_design.md item 56) - Check 1 of the client's two-check redesign:
does every voucher Tally itself says touched a Sundry Debtor actually show
up in this app's own three master registers, and does a register ever
carry a voucher Tally's own independent list doesn't recognize.

This is deliberately VOUCHER-WISE, never bill-wise or invoice-level (the
client's own explicit correction): identity is (voucher_type category,
voucher_number, party_id) only. If a voucher is missing, every constituent
of it is missing - there is no partial credit for "some of this voucher's
lines matched."

The two inputs are each already an independently stored dataset in this
tool (the client's own standing principle for every reconciliation here):
`YtdDebtorVoucherRow` (from the live Sundry-Debtors-scoped Tally pull,
ar_mis.registers.build_ytd_debtor_voucher_rows) on one side, and whichever
of the three master registers actually owns that voucher type on the
other. Neither side is recomputed from raw vouchers here - this module
only compares what's already captured.

A voucher_number is NOT globally unique across voucher types (Tally scopes
numbering per voucher type, confirmed elsewhere in this codebase) - a
Sales voucher "1" and a Receipt voucher "1" for the same party are
unrelated. Matching is always scoped to the one register that actually
owns a given voucher_type category - Sales/Debit Note to the Sales & DN
Register, Receipt/Journal to the Receipt & Journal Register, Credit Note
to its own - the same routing ar_mis.pipeline._build_and_persist_registers
already uses when building these registers in the first place. Comparing
across registers by (voucher_number, party_id) alone, with no type
scoping, would silently blend two unrelated vouchers that merely share a
number - a real risk, not a hypothetical one, given Tally's per-type
numbering.

It's not globally unique across BRANCHES either - this app's registers
are combined across all branches (item 2), and different branches commonly
reuse the same voucher numbering independently in Tally (the exact
cross-branch collision item 49 already had to fix in
compute_invoice_position). The matching key here includes branch_id for
the identical reason.

Same sign-convention trap as aggregate_party_movements_from_registers
(rollforward.py), caught the same way - by a test failing, not assumed
safe because the fields are both called "amount": YtdDebtorVoucherRow.amount
is the party's own ledger entry's RAW amount_as_extracted (Tally's at-
source convention - a normal Sale's party leg is Dr negative), matching
CreditNoteRegisterRow.cn_amount and ReceiptJournalRegisterRow.amount
directly. But SalesDNRegisterRow.invoice_value is a DERIVED, already-
positive gross total (built from abs() of the voucher's OTHER entries),
the opposite sign of the same real sale's YTD-pull amount. Comparing them
raw would show every genuinely matching Sales/Debit Note voucher as a
false "Amount Mismatch" - flip_sign is applied to the YTD side for just
those two categories before comparison, nowhere else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    ManualYtdVoucherRow,
    NoteType,
    ReceiptJournalRegisterRow,
    SalesDNRegisterRow,
    Voucher,
    YtdDebtorVoucherRow,
)
from ar_mis.money import is_match
from ar_mis.reconciliation import DriftFinding, attribute_week
from ar_mis.register_corrections import (
    CREDIT_NOTE_TYPES as _CREDIT_NOTE_TYPES,
    RECEIPT_JOURNAL_TYPES as _RECEIPT_JOURNAL_TYPES,
    SALES_DN_TYPES as _SALES_DN_TYPES,
    RegisterRowCorrection,
    apply_corrections,
    effective_corrections,
)
from ar_mis.sign import flip_sign

MATCH = "Match"
AMOUNT_MISMATCH = "Amount Mismatch"
MISSING_FROM_REGISTERS = "Missing from Registers"
EXTRA_IN_REGISTERS = "Extra in Registers"

_Key = tuple[str, str, str]  # (branch_id, voucher_number, party_id) - always scoped to one register


@dataclass(frozen=True)
class YtdVsRegisterRow:
    branch_id: str
    voucher_type: str
    voucher_number: str
    party_id: str
    voucher_date: date | None
    ytd_amount: Decimal | None
    register_amount: Decimal | None
    status: str


def _sales_dn_amounts(
    rows: list[SalesDNRegisterRow],
) -> tuple[dict[_Key, Decimal], dict[_Key, date], dict[_Key, str]]:
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    labels: dict[_Key, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.invoice_value
        dates.setdefault(key, row.invoice_date)
        # NoteType.INVOICE.value is "Invoice", but the YTD side's own
        # categorization (VoucherType.SALES.value) is "Sales" - normalize
        # here, same reason the old EXTRA_IN_REGISTERS loop below did.
        labels.setdefault(key, "Sales" if row.note_type == NoteType.INVOICE else row.note_type.value)
    return amounts, dates, labels


def _cn_amounts(
    rows: list[CreditNoteRegisterRow],
) -> tuple[dict[_Key, Decimal], dict[_Key, date], dict[_Key, str]]:
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    labels: dict[_Key, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.cn_amount
        dates.setdefault(key, row.cn_date)
        labels.setdefault(key, "Credit Note")
    return amounts, dates, labels


def _rj_amounts(
    rows: list[ReceiptJournalRegisterRow],
) -> tuple[dict[_Key, Decimal], dict[_Key, date], dict[_Key, str]]:
    # Summed, not just taken - item 2's design keeps one Receipt & Journal
    # Register row per bill allocation, but this check is voucher-wise:
    # several bill-allocation rows for the same voucher+party collapse to
    # one net figure, the same way build_ytd_debtor_voucher_rows already
    # collapses the YTD pull's own side.
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    labels: dict[_Key, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.amount
        dates.setdefault(key, row.txn_date)
        labels.setdefault(key, row.voucher_type)
    return amounts, dates, labels


def compute_ytd_vs_register_comparison(
    ytd_rows: list[YtdDebtorVoucherRow],
    sales_dn_rows: list[SalesDNRegisterRow],
    cn_rows: list[CreditNoteRegisterRow],
    rj_rows: list[ReceiptJournalRegisterRow],
    corrections: list[RegisterRowCorrection] = (),
) -> list[YtdVsRegisterRow]:
    """Both directions of the client's own explicit ask: a voucher missing
    from the registers, AND a voucher sitting in a register that Tally's
    own independent Sundry-Debtors list doesn't recognize at all (item 1's
    "or do we have extra vouchers which do not relate to Sundry Debtors").
    Every row is one (voucher_type, voucher_number, party_id) identity -
    never split further.

    `ytd_rows` should already be scoped to one branch and one as_of pull
    (Store.all_ytd_debtor_voucher_rows) - this function does no filtering
    of its own. `sales_dn_rows`/`cn_rows`/`rj_rows` should be that same
    branch's full register history (cumulative from FY start, per item 2),
    since a register row from an earlier week is still a real voucher this
    YTD pull's own date range would also cover.

    `corrections` (Phase 6b's Delete/Modify overlay, ar_mis.register_
    corrections) is applied to the register side only, before either
    direction is computed - an EXCLUDE-d key drops out of the register
    amounts entirely (so it correctly re-surfaces as MISSING_FROM_
    REGISTERS if the YTD pull still has it, or simply disappears from
    this report if it doesn't - exactly mirroring what "this should never
    have been in the register" means), and a CORRECT_AMOUNT-ed key's
    register_amount becomes the Maker's own corrected figure, which the
    usual is_match comparison then judges on its own merits - a correction
    is never silently trusted as "now a match."
    """
    sales_dn_amounts_raw, sales_dn_dates, sales_dn_labels = _sales_dn_amounts(sales_dn_rows)
    cn_amounts_raw, cn_dates, cn_labels = _cn_amounts(cn_rows)
    rj_amounts_raw, rj_dates, rj_labels = _rj_amounts(rj_rows)

    effective = effective_corrections(list(corrections))
    sales_dn_amounts = apply_corrections(sales_dn_amounts_raw, _SALES_DN_TYPES, effective)
    cn_amounts = apply_corrections(cn_amounts_raw, _CREDIT_NOTE_TYPES, effective)
    rj_amounts = apply_corrections(rj_amounts_raw, _RECEIPT_JOURNAL_TYPES, effective)

    def _register_side(voucher_type: str) -> tuple[dict[_Key, Decimal], dict[_Key, date], str]:
        if voucher_type in _SALES_DN_TYPES:
            return sales_dn_amounts, sales_dn_dates, "Sales & DN Register"
        if voucher_type in _CREDIT_NOTE_TYPES:
            return cn_amounts, cn_dates, "Credit Note Register"
        if voucher_type in _RECEIPT_JOURNAL_TYPES:
            return rj_amounts, rj_dates, "Receipt & Journal Register"
        raise ValueError(f"No register owns voucher_type {voucher_type!r}")

    ytd_amounts_by_type: dict[str, dict[_Key, Decimal]] = {}
    ytd_row_by_type_key: dict[tuple[str, _Key], YtdDebtorVoucherRow] = {}
    for row in ytd_rows:
        by_key = ytd_amounts_by_type.setdefault(row.voucher_type, {})
        key = (row.branch_id, row.voucher_number, row.party_id)
        # Sales/Debit Note: flip to match invoice_value's own already-
        # positive convention (see this module's own docstring) - Credit
        # Note/Receipt/Journal already share the raw convention as-is.
        amount = flip_sign(row.amount) if row.voucher_type in _SALES_DN_TYPES else row.amount
        by_key[key] = by_key.get(key, Decimal("0.00")) + amount
        ytd_row_by_type_key.setdefault((row.voucher_type, key), row)

    rows: list[YtdVsRegisterRow] = []
    seen_type_keys: set[tuple[str, _Key]] = set()

    for voucher_type, by_key in ytd_amounts_by_type.items():
        register_amounts, _register_dates, _label = _register_side(voucher_type)
        for key, ytd_amount in by_key.items():
            seen_type_keys.add((voucher_type, key))
            register_amount = register_amounts.get(key)
            if register_amount is None:
                status = MISSING_FROM_REGISTERS
            elif is_match(ytd_amount, register_amount):
                status = MATCH
            else:
                status = AMOUNT_MISMATCH
            ytd_row = ytd_row_by_type_key[(voucher_type, key)]
            branch_id, voucher_number, party_id = key
            rows.append(
                YtdVsRegisterRow(
                    branch_id=branch_id,
                    voucher_type=voucher_type,
                    voucher_number=voucher_number,
                    party_id=party_id,
                    voucher_date=ytd_row.voucher_date,
                    ytd_amount=ytd_amount,
                    register_amount=register_amount,
                    status=status,
                )
            )

    # The other direction: a register row whose (voucher_number, party_id)
    # never appeared in the YTD pull for that SAME register's own voucher
    # type(s). branch_id/voucher_type come from the register side's own
    # labels dict, since the YTD side has nothing for this key by
    # definition. Driven by the CORRECTED amounts dicts' own keys, never
    # the raw rows directly - an EXCLUDE-d key has no entry left in
    # `sales_dn_amounts`/`cn_amounts`/`rj_amounts` (the register row
    # itself is untouched in storage, only dropped from this computation),
    # so it correctly never appears here once excluded.
    for key, register_amount in sales_dn_amounts.items():
        voucher_type = sales_dn_labels[key]
        if (voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((voucher_type, key))
        branch_id, voucher_number, party_id = key
        rows.append(
            YtdVsRegisterRow(
                branch_id=branch_id, voucher_type=voucher_type, voucher_number=voucher_number,
                party_id=party_id, voucher_date=sales_dn_dates[key], ytd_amount=None,
                register_amount=register_amount, status=EXTRA_IN_REGISTERS,
            )
        )
    for key, register_amount in cn_amounts.items():
        if ("Credit Note", key) in seen_type_keys:
            continue
        seen_type_keys.add(("Credit Note", key))
        branch_id, voucher_number, party_id = key
        rows.append(
            YtdVsRegisterRow(
                branch_id=branch_id, voucher_type="Credit Note", voucher_number=voucher_number,
                party_id=party_id, voucher_date=cn_dates[key], ytd_amount=None,
                register_amount=register_amount, status=EXTRA_IN_REGISTERS,
            )
        )
    for key, register_amount in rj_amounts.items():
        voucher_type = rj_labels[key]
        if (voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((voucher_type, key))
        branch_id, voucher_number, party_id = key
        rows.append(
            YtdVsRegisterRow(
                branch_id=branch_id, voucher_type=voucher_type, voucher_number=voucher_number,
                party_id=party_id, voucher_date=rj_dates[key], ytd_amount=None,
                register_amount=register_amount, status=EXTRA_IN_REGISTERS,
            )
        )

    return rows


def drift_findings_from_missing_registers(
    missing_rows: list[YtdVsRegisterRow],
    source_vouchers: list[Voucher],
    week_boundaries: list[date],
) -> list[DriftFinding]:
    """Turns Check 1's own "Missing from Registers" rows into the exact
    same DriftFinding shape the app's existing incorporation mechanism
    already knows how to fix (ar_mis.drift_correction.incorporate_drift_
    finding) - no separate "Add" mechanism was built, because this one was
    already proven and tested. This also replaces the old voucher_log-
    based `isolate_drift` as the detector: Check 1 is strictly more
    complete (it compares against what the Registers actually captured,
    not just whether something was ever logged at all), so there is no
    longer a reason to run both.

    `source_vouchers` must be the SAME vouchers Check 1 itself was built
    from (the live Sundry-Debtors pull, or - once Manual Upload gets its
    own Check 1 coverage - the uploaded equivalent) - this function does
    not re-fetch anything, it only looks up each missing row's own
    original voucher by identity, the same way isolate_drift's own
    findings always carried their real source voucher for later replay.

    Only `missing_rows` (status MISSING_FROM_REGISTERS) make sense here -
    an "Extra in Registers" or "Amount Mismatch" row has no missing
    voucher to replay; passing anything else is the caller's bug, not
    silently handled here.
    """
    # Deliberately a different triple than this module's own _Key
    # (branch, voucher_number, party) - here it's (branch, voucher_number,
    # voucher_type), since one original Voucher object is shared across
    # however many tracked parties it touches, never looked up per-party.
    vouchers_by_key: dict[tuple[str, str, str], Voucher] = {}
    for voucher in source_vouchers:
        key = (voucher.branch_id, voucher.voucher_number, voucher.voucher_type.value)
        vouchers_by_key[key] = voucher

    findings: list[DriftFinding] = []
    for row in missing_rows:
        if row.status != MISSING_FROM_REGISTERS:
            raise ValueError(f"drift_findings_from_missing_registers got a non-missing row: {row.status!r}")
        voucher = vouchers_by_key.get((row.branch_id, row.voucher_number, row.voucher_type))
        if voucher is None:
            continue
        entry = next((e for e in voucher.entries if e.party_ledger_name == row.party_id), None)
        if entry is None:
            continue
        findings.append(
            DriftFinding(
                party_ledger_name=row.party_id,
                voucher_type=row.voucher_type,
                voucher_number=row.voucher_number,
                voucher_date=voucher.voucher_date,
                # Same uniform AR-movement convention DriftFinding has
                # always used (flip_sign on the party's own raw entry) -
                # deliberately NOT the register-comparison-specific
                # adjustment this module applies to ytd_amount above,
                # which exists only to make Sales/DN comparable to
                # invoice_value and has nothing to do with this finding's
                # own, separate meaning.
                flipped_amount=flip_sign(entry.amount_as_extracted),
                attributed_week=attribute_week(voucher.voucher_date, week_boundaries),
                voucher=voucher,
            )
        )
    return findings


_BranchVoucherKey = tuple[str, str]  # (branch_id, voucher_number) - no party, see ManualYtdVsRegisterRow


@dataclass(frozen=True)
class ManualYtdVsRegisterRow:
    """Manual Upload's own Check 1 - see compute_manual_ytd_vs_register_
    comparison's own docstring for why this carries no party_id, unlike
    YtdVsRegisterRow."""

    branch_id: str
    voucher_type: str
    voucher_number: str
    voucher_date: date | None
    ytd_amount: Decimal | None
    register_amount: Decimal | None
    status: str


def _sales_dn_amounts_by_voucher(
    rows: list[SalesDNRegisterRow],
) -> tuple[dict[_BranchVoucherKey, Decimal], dict[_BranchVoucherKey, date], dict[_BranchVoucherKey, str]]:
    amounts: dict[_BranchVoucherKey, Decimal] = {}
    dates: dict[_BranchVoucherKey, date] = {}
    labels: dict[_BranchVoucherKey, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.invoice_value
        dates.setdefault(key, row.invoice_date)
        labels.setdefault(key, "Sales" if row.note_type == NoteType.INVOICE else row.note_type.value)
    return amounts, dates, labels


def _cn_amounts_by_voucher(
    rows: list[CreditNoteRegisterRow],
) -> tuple[dict[_BranchVoucherKey, Decimal], dict[_BranchVoucherKey, date], dict[_BranchVoucherKey, str]]:
    amounts: dict[_BranchVoucherKey, Decimal] = {}
    dates: dict[_BranchVoucherKey, date] = {}
    labels: dict[_BranchVoucherKey, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.cn_amount
        dates.setdefault(key, row.cn_date)
        labels.setdefault(key, "Credit Note")
    return amounts, dates, labels


def _rj_amounts_by_voucher(
    rows: list[ReceiptJournalRegisterRow],
) -> tuple[dict[_BranchVoucherKey, Decimal], dict[_BranchVoucherKey, date], dict[_BranchVoucherKey, str]]:
    amounts: dict[_BranchVoucherKey, Decimal] = {}
    dates: dict[_BranchVoucherKey, date] = {}
    labels: dict[_BranchVoucherKey, str] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.amount
        dates.setdefault(key, row.txn_date)
        labels.setdefault(key, row.voucher_type)
    return amounts, dates, labels


def compute_manual_ytd_vs_register_comparison(
    ytd_rows: list[ManualYtdVoucherRow],
    sales_dn_rows: list[SalesDNRegisterRow],
    cn_rows: list[CreditNoteRegisterRow],
    rj_rows: list[ReceiptJournalRegisterRow],
) -> list[ManualYtdVsRegisterRow]:
    """Manual Upload's own version of Check 1, for when there is no live
    Tally connection to pull the Sundry-Debtors-scoped voucher list from
    (ar_mis.tally_client.TallyClient.fetch_sundry_debtor_vouchers)- the
    one export a real operator's Tally UI CAN produce for this (Sundry
    Debtors -> Ctrl+H -> Voucher view, ar_mis.parsers.parse_manual_ytd_
    voucher_report) carries no party field at all, so identity here is
    (branch_id, voucher_type, voucher_number) only - never party_id.

    This is a deliberate, confirmed design choice, not a fallback made
    because the data happens to be missing it: the client's own prior
    Excel cross-check template filtered its SUMIFS formula by branch +
    voucher type + date range only, never by debtor name, and Tally
    itself already numbers a voucher uniquely per type per company
    (never per party) - so (branch_id, voucher_type, voucher_number) was
    already a complete, unambiguous identity on its own, same as the
    live Check 1's own key minus the one dimension this export can't
    supply.

    Same sign-convention handling as compute_ytd_vs_register_comparison:
    flip_sign applied to the YTD side only for Sales/Debit Note, to match
    SalesDNRegisterRow.invoice_value's own already-positive convention;
    Credit Note/Receipt/Journal compare raw-to-raw.

    Callers must pass only ONE branch's own rows on both sides (ytd_rows
    from parse_manual_ytd_voucher_report, already scoped to the branch_id
    it was parsed with; sales_dn_rows/cn_rows/rj_rows should be that same
    branch's own register history) - this function does no branch
    filtering of its own, unlike the live path, which relies on branch_id
    being part of its own richer key instead.
    """
    sales_dn_amounts_raw, sales_dn_dates, sales_dn_labels = _sales_dn_amounts_by_voucher(sales_dn_rows)
    cn_amounts, cn_dates, cn_labels = _cn_amounts_by_voucher(cn_rows)
    rj_amounts, rj_dates, rj_labels = _rj_amounts_by_voucher(rj_rows)

    def _register_side(voucher_type: str) -> tuple[dict[_BranchVoucherKey, Decimal], dict[_BranchVoucherKey, date]]:
        if voucher_type in _SALES_DN_TYPES:
            return sales_dn_amounts_raw, sales_dn_dates
        if voucher_type in _CREDIT_NOTE_TYPES:
            return cn_amounts, cn_dates
        if voucher_type in _RECEIPT_JOURNAL_TYPES:
            return rj_amounts, rj_dates
        raise ValueError(f"No register owns voucher_type {voucher_type!r}")

    ytd_amounts_by_type: dict[str, dict[_BranchVoucherKey, Decimal]] = {}
    ytd_row_by_type_key: dict[tuple[str, _BranchVoucherKey], ManualYtdVoucherRow] = {}
    for row in ytd_rows:
        by_key = ytd_amounts_by_type.setdefault(row.voucher_type, {})
        key = (row.branch_id, row.voucher_number)
        amount = flip_sign(row.amount) if row.voucher_type in _SALES_DN_TYPES else row.amount
        by_key[key] = by_key.get(key, Decimal("0.00")) + amount
        ytd_row_by_type_key.setdefault((row.voucher_type, key), row)

    rows: list[ManualYtdVsRegisterRow] = []
    seen_type_keys: set[tuple[str, _BranchVoucherKey]] = set()

    for voucher_type, by_key in ytd_amounts_by_type.items():
        register_amounts, _register_dates = _register_side(voucher_type)
        for key, ytd_amount in by_key.items():
            seen_type_keys.add((voucher_type, key))
            register_amount = register_amounts.get(key)
            if register_amount is None:
                status = MISSING_FROM_REGISTERS
            elif is_match(ytd_amount, register_amount):
                status = MATCH
            else:
                status = AMOUNT_MISMATCH
            ytd_row = ytd_row_by_type_key[(voucher_type, key)]
            branch_id, voucher_number = key
            rows.append(
                ManualYtdVsRegisterRow(
                    branch_id=branch_id,
                    voucher_type=voucher_type,
                    voucher_number=voucher_number,
                    voucher_date=ytd_row.voucher_date,
                    ytd_amount=ytd_amount,
                    register_amount=register_amount,
                    status=status,
                )
            )

    for key, register_amount in sales_dn_amounts_raw.items():
        voucher_type = sales_dn_labels[key]
        if (voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((voucher_type, key))
        branch_id, voucher_number = key
        rows.append(
            ManualYtdVsRegisterRow(
                branch_id=branch_id, voucher_type=voucher_type, voucher_number=voucher_number,
                voucher_date=sales_dn_dates[key], ytd_amount=None, register_amount=register_amount,
                status=EXTRA_IN_REGISTERS,
            )
        )
    for key, register_amount in cn_amounts.items():
        if ("Credit Note", key) in seen_type_keys:
            continue
        seen_type_keys.add(("Credit Note", key))
        branch_id, voucher_number = key
        rows.append(
            ManualYtdVsRegisterRow(
                branch_id=branch_id, voucher_type="Credit Note", voucher_number=voucher_number,
                voucher_date=cn_dates[key], ytd_amount=None, register_amount=register_amount,
                status=EXTRA_IN_REGISTERS,
            )
        )
    for key, register_amount in rj_amounts.items():
        voucher_type = rj_labels[key]
        if (voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((voucher_type, key))
        branch_id, voucher_number = key
        rows.append(
            ManualYtdVsRegisterRow(
                branch_id=branch_id, voucher_type=voucher_type, voucher_number=voucher_number,
                voucher_date=rj_dates[key], ytd_amount=None, register_amount=register_amount,
                status=EXTRA_IN_REGISTERS,
            )
        )

    return rows
