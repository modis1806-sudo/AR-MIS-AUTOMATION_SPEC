from datetime import date, datetime
from decimal import Decimal

import pytest

from ar_mis.models import CreditNoteRegisterRow, NoteType, ReceiptJournalRegisterRow, SalesDNRegisterRow
from ar_mis.register_corrections import (
    CORRECT_AMOUNT,
    CREDIT_NOTE_TYPES,
    EXCLUDE,
    RECEIPT_JOURNAL_TYPES,
    REINSTATE,
    SALES_DN_TYPES,
    RegisterRowCorrection,
    apply_corrections,
    cn_amounts,
    effective_corrections,
    rj_amounts,
    sales_dn_amounts,
)


def _correction(**overrides) -> RegisterRowCorrection:
    defaults = dict(
        branch_id="B1", voucher_type="Sales", voucher_number="SB/1", party_id="Acme",
        action=EXCLUDE, corrected_amount=None, reason="test", corrected_by="Maker",
        corrected_at=datetime(2026, 1, 1, 10, 0),
    )
    defaults.update(overrides)
    return RegisterRowCorrection(**defaults)


def test_correct_amount_requires_a_corrected_amount():
    with pytest.raises(ValueError):
        _correction(action=CORRECT_AMOUNT, corrected_amount=None)


def test_unknown_action_rejected():
    with pytest.raises(ValueError):
        _correction(action="Delete Forever")


def test_effective_corrections_latest_wins():
    older = _correction(action=EXCLUDE, corrected_at=datetime(2026, 1, 1))
    newer = _correction(action=REINSTATE, corrected_at=datetime(2026, 1, 5))
    effective = effective_corrections([older, newer])
    key = ("B1", "Sales", "SB/1", "Acme")
    assert effective[key] is newer


def test_effective_corrections_latest_wins_regardless_of_input_order():
    older = _correction(action=EXCLUDE, corrected_at=datetime(2026, 1, 1))
    newer = _correction(action=REINSTATE, corrected_at=datetime(2026, 1, 5))
    effective = effective_corrections([newer, older])
    key = ("B1", "Sales", "SB/1", "Acme")
    assert effective[key] is newer


def test_apply_corrections_exclude_drops_the_key():
    amounts = {("B1", "SB/1", "Acme"): Decimal("1000.00")}
    effective = effective_corrections([_correction(action=EXCLUDE)])
    result = apply_corrections(amounts, SALES_DN_TYPES, effective)
    assert result == {}


def test_apply_corrections_correct_amount_overrides_the_total():
    amounts = {("B1", "SB/1", "Acme"): Decimal("1000.00")}
    correction = _correction(action=CORRECT_AMOUNT, corrected_amount=Decimal("1200.00"))
    effective = effective_corrections([correction])
    result = apply_corrections(amounts, SALES_DN_TYPES, effective)
    assert result == {("B1", "SB/1", "Acme"): Decimal("1200.00")}


def test_apply_corrections_reinstate_is_a_no_op_on_the_raw_figure():
    amounts = {("B1", "SB/1", "Acme"): Decimal("1000.00")}
    effective = effective_corrections([_correction(action=REINSTATE)])
    result = apply_corrections(amounts, SALES_DN_TYPES, effective)
    assert result == {("B1", "SB/1", "Acme"): Decimal("1000.00")}


def test_apply_corrections_never_leaks_across_a_sibling_type():
    # A "Debit Note" correction must never touch a "Sales" row sharing the
    # exact same (branch, voucher_number, party) key - the whole point of
    # scoping corrections by exact voucher_type label, never a type-group.
    amounts = {("B1", "1", "Acme"): Decimal("1000.00")}
    debit_note_correction = _correction(voucher_type="Debit Note", action=EXCLUDE)
    effective = effective_corrections([debit_note_correction])
    result = apply_corrections(amounts, frozenset({"Sales"}), effective)
    assert result == {("B1", "1", "Acme"): Decimal("1000.00")}


def test_apply_corrections_respects_the_type_group_membership():
    amounts = {("B1", "SB/1", "Acme"): Decimal("1000.00")}
    correction = _correction(voucher_type="Debit Note", action=EXCLUDE)
    effective = effective_corrections([correction])
    # Debit Note is in SALES_DN_TYPES, so this one DOES apply to a Sales &
    # DN Register amounts dict even though the row itself was a Sales row -
    # scoping is by the correction's own type label, not by what physically
    # built the dict.
    result = apply_corrections(amounts, SALES_DN_TYPES, effective)
    assert result == {}


def test_sales_dn_amounts_sums_by_branch_voucher_party():
    rows = [
        SalesDNRegisterRow(
            branch_id="B1", invoice_date=date(2026, 1, 1), note_type=NoteType.INVOICE, voucher_number="SB/1",
            bill_allocation_reference="SB/1", party_id="Acme", taxable_value=Decimal("1000.00"),
            cgst=Decimal("90.00"), sgst=Decimal("90.00"), igst=Decimal("0.00"), invoice_value=Decimal("1180.00"),
            due_date=date(2026, 2, 1),
        ),
    ]
    assert sales_dn_amounts(rows) == {("B1", "SB/1", "Acme"): Decimal("1180.00")}


def test_cn_amounts_sums_by_branch_voucher_party():
    rows = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2026, 1, 1), voucher_number="CN/1", party_id="Acme",
            cn_amount=Decimal("500.00"),
        ),
    ]
    assert cn_amounts(rows) == {("B1", "CN/1", "Acme"): Decimal("500.00")}


def test_rj_amounts_collapses_multiple_bill_allocation_lines_for_one_voucher():
    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 1), voucher_type="Receipt", voucher_number="RCT/1",
            party_id="Acme", amount=Decimal("300.00"), target_doc_no="SB/1",
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 1), voucher_type="Receipt", voucher_number="RCT/1",
            party_id="Acme", amount=Decimal("700.00"), target_doc_no="SB/2",
        ),
    ]
    assert rj_amounts(rows) == {("B1", "RCT/1", "Acme"): Decimal("1000.00")}


def test_credit_note_types_and_receipt_journal_types_are_disjoint_from_sales_dn():
    assert SALES_DN_TYPES.isdisjoint(CREDIT_NOTE_TYPES)
    assert SALES_DN_TYPES.isdisjoint(RECEIPT_JOURNAL_TYPES)
    assert CREDIT_NOTE_TYPES.isdisjoint(RECEIPT_JOURNAL_TYPES)


def test_same_correction_is_consistent_across_check_1_and_check_2():
    # The whole point of Phase 6b: a Delete/Modify correction must change
    # what BOTH checks report, never just one - since both ultimately
    # depend on the same Registers (the client's own standing point).
    from ar_mis.rollforward import aggregate_party_movements_from_registers
    from ar_mis.ytd_debtor_cross_check import compute_ytd_vs_register_comparison
    from ar_mis.models import YtdDebtorVoucherRow

    party = "Acme Corp"
    cn_rows = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id=party,
            cn_amount=Decimal("250.00"),
        )
    ]
    correction = _correction(
        branch_id="B1", voucher_type="Credit Note", voucher_number="CN1", party_id=party,
        action=CORRECT_AMOUNT, corrected_amount=Decimal("300.00"),
    )

    # Check 2: the party's own movement total picks up the corrected
    # figure, flip_sign-ed exactly as an uncorrected cn_amount would be.
    movements = aggregate_party_movements_from_registers(
        [], cn_rows, [], {party}, {party: Decimal("0.00")}, corrections=[correction],
    )
    assert movements[party].credit_notes == Decimal("-300.00")

    # Check 1: the same correction, same figure, shows up as the
    # register_amount Tally's own pull is now compared against.
    ytd_rows = [
        YtdDebtorVoucherRow(
            branch_id="B1", as_of=date(2025, 4, 7), voucher_date=date(2025, 4, 2), voucher_type="Credit Note",
            raw_voucher_type_name="Credit Note", voucher_number="CN1", party_id=party, amount=Decimal("300.00"),
        )
    ]
    comparison = compute_ytd_vs_register_comparison(ytd_rows, [], cn_rows, [], corrections=[correction])
    assert len(comparison) == 1
    assert comparison[0].status == "Match"
    assert comparison[0].register_amount == Decimal("300.00")
