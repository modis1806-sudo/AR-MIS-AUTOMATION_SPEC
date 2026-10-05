from datetime import date, datetime
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    LedgerEntry,
    NoteType,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
    YtdDebtorVoucherRow,
)
from ar_mis.register_corrections import CORRECT_AMOUNT, EXCLUDE, RegisterRowCorrection
from ar_mis.ytd_debtor_cross_check import (
    AMOUNT_MISMATCH,
    EXTRA_IN_REGISTERS,
    MATCH,
    MISSING_FROM_REGISTERS,
    compute_ytd_vs_register_comparison,
    drift_findings_from_missing_registers,
)


def _sales_dn_row(voucher_number: str, party_id: str, invoice_value: Decimal) -> SalesDNRegisterRow:
    return SalesDNRegisterRow(
        branch_id="B1", invoice_date=date(2025, 4, 1), note_type=NoteType.INVOICE,
        voucher_number=voucher_number, bill_allocation_reference=voucher_number, party_id=party_id,
        taxable_value=invoice_value, cgst=Decimal("0.00"), sgst=Decimal("0.00"), igst=Decimal("0.00"),
        invoice_value=invoice_value, due_date=date(2025, 5, 1),
    )


def _ytd_row(voucher_type: str, voucher_number: str, party_id: str, amount: Decimal) -> YtdDebtorVoucherRow:
    return YtdDebtorVoucherRow(
        branch_id="B1", as_of=date(2025, 4, 7), voucher_date=date(2025, 4, 1), voucher_type=voucher_type,
        raw_voucher_type_name=voucher_type, voucher_number=voucher_number, party_id=party_id, amount=amount,
    )


def test_matching_sales_voucher_shows_as_match():
    # YtdDebtorVoucherRow.amount is the party's own RAW entry (Tally's
    # at-source convention - a normal Sale's party leg is Dr negative),
    # while SalesDNRegisterRow.invoice_value is a derived, already-
    # positive gross total - opposite signs for the same real sale. The
    # comparison itself must flip one to match the other (see this
    # module's own docstring) - a real bug caught by this exact test
    # before the fix, when both sides were positive only by coincidence.
    ytd = [_ytd_row("Sales", "1", "ACME", Decimal("-1000.00"))]
    sales_dn = [_sales_dn_row("1", "ACME", Decimal("1000.00"))]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], [])
    assert len(rows) == 1
    assert rows[0].status == MATCH
    assert rows[0].ytd_amount == Decimal("1000.00")  # shown normalized, same convention as register_amount
    assert rows[0].register_amount == Decimal("1000.00")


def test_sales_note_type_label_normalizes_to_match_the_ytd_sides_own_label():
    # Real bug caught while building this: SalesDNRegisterRow.note_type's
    # own stored value is "Invoice" (NoteType.INVOICE), but the YTD pull's
    # own categorization (VoucherType.SALES) is "Sales" - without
    # normalizing one to the other, every genuinely matching Sales voucher
    # would wrongly show as "Extra in Registers" because the two sides'
    # type labels never compared equal.
    ytd = [_ytd_row("Sales", "1", "ACME", Decimal("-500.00"))]
    sales_dn = [_sales_dn_row("1", "ACME", Decimal("500.00"))]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], [])
    assert len(rows) == 1
    assert rows[0].status == MATCH


def test_voucher_in_ytd_pull_missing_from_registers():
    ytd = [_ytd_row("Receipt", "9", "KAY DEE", Decimal("5000.00"))]
    rows = compute_ytd_vs_register_comparison(ytd, [], [], [])
    assert len(rows) == 1
    assert rows[0].status == MISSING_FROM_REGISTERS
    assert rows[0].register_amount is None


def test_voucher_in_registers_but_not_in_ytd_pull_shows_as_extra():
    sales_dn = [_sales_dn_row("1", "ACME", Decimal("1000.00"))]
    rows = compute_ytd_vs_register_comparison([], sales_dn, [], [])
    assert len(rows) == 1
    assert rows[0].status == EXTRA_IN_REGISTERS
    assert rows[0].ytd_amount is None
    assert rows[0].register_amount == Decimal("1000.00")


def test_amount_differs_between_the_two_sides_is_its_own_status():
    ytd = [_ytd_row("Sales", "1", "ACME", Decimal("-1000.00"))]
    sales_dn = [_sales_dn_row("1", "ACME", Decimal("900.00"))]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], [])
    assert len(rows) == 1
    assert rows[0].status == AMOUNT_MISMATCH


def test_same_voucher_number_across_different_types_never_cross_contaminate():
    # Tally scopes voucher numbering per TYPE - a Sales "1" and a Receipt
    # "1" for the same party are unrelated. Comparing them as if they were
    # the same voucher would silently blend two real, separate things.
    ytd = [
        _ytd_row("Sales", "1", "ACME", Decimal("-1000.00")),
        _ytd_row("Receipt", "1", "ACME", Decimal("1000.00")),
    ]
    sales_dn = [_sales_dn_row("1", "ACME", Decimal("1000.00"))]
    rj = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2025, 4, 1), voucher_type="Receipt", voucher_number="1",
            party_id="ACME", amount=Decimal("1000.00"),
        )
    ]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], rj)
    assert len(rows) == 2
    assert {r.status for r in rows} == {MATCH}
    by_type = {r.voucher_type: r for r in rows}
    assert by_type["Sales"].register_amount == Decimal("1000.00")
    assert by_type["Receipt"].register_amount == Decimal("1000.00")


def test_receipt_journal_multiple_bill_allocation_rows_collapse_to_one_net_comparison():
    # Receipt & Journal Register deliberately keeps one row per bill
    # allocation (item 2) - this check is voucher-wise, so two allocation
    # rows for the same voucher+party must net to ONE comparison row, not
    # two, and not double-count the register's own total.
    ytd = [_ytd_row("Receipt", "9", "KAY DEE ELECTRIC CO.", Decimal("4835.00"))]
    rj = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2025, 4, 1), voucher_type="Receipt", voucher_number="9",
            party_id="KAY DEE ELECTRIC CO.", amount=Decimal("-7750.00"),
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2025, 4, 1), voucher_type="Receipt", voucher_number="9",
            party_id="KAY DEE ELECTRIC CO.", amount=Decimal("5750.00"),
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2025, 4, 1), voucher_type="Receipt", voucher_number="9",
            party_id="KAY DEE ELECTRIC CO.", amount=Decimal("6835.00"),
        ),
    ]
    rows = compute_ytd_vs_register_comparison(ytd, [], [], rj)
    assert len(rows) == 1
    assert rows[0].status == MATCH
    assert rows[0].register_amount == Decimal("4835.00")


def test_credit_note_extra_in_registers_uses_its_own_cn_date_and_amount():
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id="ACME",
            cn_amount=Decimal("250.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    rows = compute_ytd_vs_register_comparison([], [], cn, [])
    assert len(rows) == 1
    assert rows[0].status == EXTRA_IN_REGISTERS
    assert rows[0].voucher_type == "Credit Note"
    assert rows[0].voucher_date == date(2025, 4, 2)
    assert rows[0].register_amount == Decimal("250.00")


def test_same_voucher_number_across_different_branches_never_cross_contaminate():
    # The exact cross-branch collision class item 49 already had to fix in
    # compute_invoice_position - different branches commonly reuse the
    # same voucher numbering independently in Tally. Branch A's Sales "1"
    # for 1000 must never be treated as satisfying Branch B's own Sales
    # "1" for a different amount, or vice versa.
    ytd = [
        _ytd_row("Sales", "1", "ACME", Decimal("1000.00")),
    ]
    ytd[0] = YtdDebtorVoucherRow(
        branch_id="BRANCH-A", as_of=date(2025, 4, 7), voucher_date=date(2025, 4, 1), voucher_type="Sales",
        raw_voucher_type_name="Sales", voucher_number="1", party_id="ACME", amount=Decimal("1000.00"),
    )
    sales_dn = [
        SalesDNRegisterRow(
            branch_id="BRANCH-B", invoice_date=date(2025, 4, 1), note_type=NoteType.INVOICE,
            voucher_number="1", bill_allocation_reference="1", party_id="ACME",
            taxable_value=Decimal("500.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"), igst=Decimal("0.00"),
            invoice_value=Decimal("500.00"), due_date=date(2025, 5, 1),
        )
    ]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], [])
    assert len(rows) == 2
    by_branch = {r.branch_id: r for r in rows}
    assert by_branch["BRANCH-A"].status == MISSING_FROM_REGISTERS
    assert by_branch["BRANCH-B"].status == EXTRA_IN_REGISTERS


def test_empty_inputs_produce_no_rows():
    assert compute_ytd_vs_register_comparison([], [], [], []) == []


# ---- drift_findings_from_missing_registers --------------------------------


def test_drift_findings_from_missing_registers_builds_a_replayable_finding():
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE, voucher_date=date(2025, 4, 1), voucher_number="CN/24",
        branch_id="B1", party_ledger_name="Joy Ray",
        entries=[
            LedgerEntry(party_ledger_name="Joy Ray", amount_as_extracted=Decimal("-2160.00")),
            LedgerEntry(party_ledger_name="Manoj Lal", amount_as_extracted=Decimal("2160.00")),
        ],
    )
    missing_rows = compute_ytd_vs_register_comparison(
        [
            _ytd_row("Credit Note", "CN/24", "Joy Ray", Decimal("-2160.00")),
            _ytd_row("Credit Note", "CN/24", "Manoj Lal", Decimal("2160.00")),
        ],
        [], [], [],
    )
    assert {r.status for r in missing_rows} == {MISSING_FROM_REGISTERS}

    findings = drift_findings_from_missing_registers(missing_rows, [voucher], week_boundaries=[date(2026, 4, 7)])
    assert len(findings) == 2
    by_party = {f.party_ledger_name: f for f in findings}
    # flip_sign negates the party's own raw entry (Tally's at-source
    # convention) - Joy Ray's raw -2160.00 (a Dr decrease at source)
    # becomes +2160.00 in the uniform AR-movement convention, and vice
    # versa for Manoj Lal.
    assert by_party["Joy Ray"].flipped_amount == Decimal("2160.00")
    assert by_party["Manoj Lal"].flipped_amount == Decimal("-2160.00")
    assert by_party["Joy Ray"].voucher is voucher  # the real original voucher, ready to replay
    assert by_party["Joy Ray"].attributed_week == date(2026, 4, 7)


def test_drift_findings_from_missing_registers_skips_a_voucher_it_cant_find(): 
    missing_rows = compute_ytd_vs_register_comparison(
        [_ytd_row("Sales", "SB/1", "ACME", Decimal("-1000.00"))], [], [], []
    )
    # No source voucher supplied - nothing to replay, so nothing is built,
    # never a crash or a half-built finding.
    assert drift_findings_from_missing_registers(missing_rows, [], week_boundaries=[]) == []


def test_drift_findings_from_missing_registers_rejects_a_non_missing_row():
    import pytest

    matched_rows = compute_ytd_vs_register_comparison(
        [_ytd_row("Sales", "1", "ACME", Decimal("-1000.00"))],
        [_sales_dn_row("1", "ACME", Decimal("1000.00"))], [], [],
    )
    assert matched_rows[0].status == MATCH
    with pytest.raises(ValueError):
        drift_findings_from_missing_registers(matched_rows, [], week_boundaries=[])


def _correction(**overrides) -> RegisterRowCorrection:
    defaults = dict(
        branch_id="B1", voucher_type="Credit Note", voucher_number="CN1", party_id="ACME",
        action=EXCLUDE, corrected_amount=None, reason="test", corrected_by="Maker",
        corrected_at=datetime(2025, 4, 10, 9, 0),
    )
    defaults.update(overrides)
    return RegisterRowCorrection(**defaults)


def test_exclude_correction_removes_an_extra_in_registers_row_entirely():
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id="ACME",
            cn_amount=Decimal("250.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    corrections = [_correction(action=EXCLUDE)]
    rows = compute_ytd_vs_register_comparison([], [], cn, [], corrections=corrections)
    assert rows == []


def test_exclude_correction_turns_a_match_into_missing_from_registers():
    # Excluding a register row that WAS genuinely matching the YTD pull
    # must surface as a fresh, honest mismatch - never silently vanish on
    # both sides, since Tally's own independent list still has it.
    ytd = [_ytd_row("Credit Note", "CN1", "ACME", Decimal("250.00"))]
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id="ACME",
            cn_amount=Decimal("250.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    corrections = [_correction(action=EXCLUDE)]
    rows = compute_ytd_vs_register_comparison(ytd, [], cn, [], corrections=corrections)
    assert len(rows) == 1
    assert rows[0].status == MISSING_FROM_REGISTERS


def test_correct_amount_turns_amount_mismatch_into_match():
    ytd = [_ytd_row("Credit Note", "CN1", "ACME", Decimal("300.00"))]
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id="ACME",
            cn_amount=Decimal("250.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    rows_before = compute_ytd_vs_register_comparison(ytd, [], cn, [])
    assert rows_before[0].status == AMOUNT_MISMATCH

    corrections = [_correction(action=CORRECT_AMOUNT, corrected_amount=Decimal("300.00"))]
    rows_after = compute_ytd_vs_register_comparison(ytd, [], cn, [], corrections=corrections)
    assert len(rows_after) == 1
    assert rows_after[0].status == MATCH
    assert rows_after[0].register_amount == Decimal("300.00")


def test_correct_amount_with_the_wrong_figure_still_shows_a_mismatch():
    # A correction is never silently trusted - if the Maker's own typed-in
    # figure still disagrees with the YTD pull, it stays AMOUNT_MISMATCH,
    # just against the corrected number instead of the register's own.
    ytd = [_ytd_row("Credit Note", "CN1", "ACME", Decimal("300.00"))]
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 2), voucher_number="CN1", party_id="ACME",
            cn_amount=Decimal("250.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    corrections = [_correction(action=CORRECT_AMOUNT, corrected_amount=Decimal("275.00"))]
    rows = compute_ytd_vs_register_comparison(ytd, [], cn, [], corrections=corrections)
    assert rows[0].status == AMOUNT_MISMATCH
    assert rows[0].register_amount == Decimal("275.00")


def test_correct_amount_on_a_sales_voucher_is_not_flip_signed_in_the_comparison():
    # Same sign-convention trap caught three times already - a Modify on
    # a Sales/DN row must land in invoice_value's own already-positive
    # convention, with no flip_sign applied on the register side here
    # (that adjustment only ever applies to the YTD side, per this
    # module's own docstring).
    ytd = [_ytd_row("Sales", "SB/1", "ACME", Decimal("-1000.00"))]
    sales_dn = [_sales_dn_row("SB/1", "ACME", Decimal("900.00"))]
    corrections = [
        _correction(voucher_type="Sales", voucher_number="SB/1", action=CORRECT_AMOUNT, corrected_amount=Decimal("1000.00"))
    ]
    rows = compute_ytd_vs_register_comparison(ytd, sales_dn, [], [], corrections=corrections)
    assert len(rows) == 1
    assert rows[0].status == MATCH
    assert rows[0].register_amount == Decimal("1000.00")


def test_correction_never_leaks_across_a_different_register_sharing_the_same_key():
    # A "Receipt" exclude must never touch a "Credit Note" row that
    # happens to share the same (branch, voucher_number, party) - Check 1
    # always scopes a correction to the one register that actually owns
    # its own voucher_type (Sales/Debit Note are deliberately treated as
    # one combined "Sales & DN Register" scope - see this module's own
    # docstring - but Credit Note and Receipt/Journal are always kept
    # fully separate registers, and a correction must respect that).
    ytd = [_ytd_row("Credit Note", "1", "ACME", Decimal("1000.00"))]
    cn = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2025, 4, 1), voucher_number="1", party_id="ACME",
            cn_amount=Decimal("1000.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    corrections = [_correction(voucher_type="Receipt", voucher_number="1", action=EXCLUDE)]
    rows = compute_ytd_vs_register_comparison(ytd, [], cn, [], corrections=corrections)
    assert len(rows) == 1
    assert rows[0].status == MATCH
