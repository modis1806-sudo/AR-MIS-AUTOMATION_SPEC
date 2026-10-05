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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    NoteType,
    ReceiptJournalRegisterRow,
    SalesDNRegisterRow,
    YtdDebtorVoucherRow,
)
from ar_mis.money import is_match

MATCH = "Match"
AMOUNT_MISMATCH = "Amount Mismatch"
MISSING_FROM_REGISTERS = "Missing from Registers"
EXTRA_IN_REGISTERS = "Extra in Registers"

_SALES_DN_TYPES = {"Sales", "Debit Note"}
_RECEIPT_JOURNAL_TYPES = {"Receipt", "Journal"}
_CREDIT_NOTE_TYPES = {"Credit Note"}

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


def _sales_dn_amounts(rows: list[SalesDNRegisterRow]) -> tuple[dict[_Key, Decimal], dict[_Key, date]]:
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.invoice_value
        dates.setdefault(key, row.invoice_date)
    return amounts, dates


def _cn_amounts(rows: list[CreditNoteRegisterRow]) -> tuple[dict[_Key, Decimal], dict[_Key, date]]:
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.cn_amount
        dates.setdefault(key, row.cn_date)
    return amounts, dates


def _rj_amounts(rows: list[ReceiptJournalRegisterRow]) -> tuple[dict[_Key, Decimal], dict[_Key, date]]:
    # Summed, not just taken - item 2's design keeps one Receipt & Journal
    # Register row per bill allocation, but this check is voucher-wise:
    # several bill-allocation rows for the same voucher+party collapse to
    # one net figure, the same way build_ytd_debtor_voucher_rows already
    # collapses the YTD pull's own side.
    amounts: dict[_Key, Decimal] = {}
    dates: dict[_Key, date] = {}
    for row in rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        amounts[key] = amounts.get(key, Decimal("0.00")) + row.amount
        dates.setdefault(key, row.txn_date)
    return amounts, dates


def compute_ytd_vs_register_comparison(
    ytd_rows: list[YtdDebtorVoucherRow],
    sales_dn_rows: list[SalesDNRegisterRow],
    cn_rows: list[CreditNoteRegisterRow],
    rj_rows: list[ReceiptJournalRegisterRow],
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
    """
    sales_dn_amounts, sales_dn_dates = _sales_dn_amounts(sales_dn_rows)
    cn_amounts, cn_dates = _cn_amounts(cn_rows)
    rj_amounts, rj_dates = _rj_amounts(rj_rows)

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
        by_key[key] = by_key.get(key, Decimal("0.00")) + row.amount
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
    # type(s). branch_id/voucher_type come from the register row itself,
    # since the YTD side has nothing for this key by definition.
    for row in sales_dn_rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        # NoteType.INVOICE.value is "Invoice", but the YTD side's own
        # categorization (ar_mis.models.VoucherType.SALES.value) is
        # "Sales" - normalize here so the two sides' labels for the same
        # real voucher actually compare equal, or every Sales row would
        # wrongly show as "extra" despite genuinely matching.
        voucher_type = "Sales" if row.note_type == NoteType.INVOICE else row.note_type.value
        if (voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((voucher_type, key))
        rows.append(
            YtdVsRegisterRow(
                branch_id=row.branch_id, voucher_type=voucher_type, voucher_number=row.voucher_number,
                party_id=row.party_id, voucher_date=row.invoice_date, ytd_amount=None,
                register_amount=sales_dn_amounts[key], status=EXTRA_IN_REGISTERS,
            )
        )
    for row in cn_rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        if ("Credit Note", key) in seen_type_keys:
            continue
        seen_type_keys.add(("Credit Note", key))
        rows.append(
            YtdVsRegisterRow(
                branch_id=row.branch_id, voucher_type="Credit Note", voucher_number=row.voucher_number,
                party_id=row.party_id, voucher_date=row.cn_date, ytd_amount=None,
                register_amount=cn_amounts[key], status=EXTRA_IN_REGISTERS,
            )
        )
    for row in rj_rows:
        key = (row.branch_id, row.voucher_number, row.party_id)
        if (row.voucher_type, key) in seen_type_keys:
            continue
        seen_type_keys.add((row.voucher_type, key))
        rows.append(
            YtdVsRegisterRow(
                branch_id=row.branch_id, voucher_type=row.voucher_type, voucher_number=row.voucher_number,
                party_id=row.party_id, voucher_date=row.txn_date, ytd_amount=None,
                register_amount=rj_amounts[key], status=EXTRA_IN_REGISTERS,
            )
        )

    return rows
