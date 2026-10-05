from datetime import date
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    NoteType,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    YtdDebtorVoucherRow,
)
from ar_mis.ytd_debtor_cross_check import (
    AMOUNT_MISMATCH,
    EXTRA_IN_REGISTERS,
    MATCH,
    MISSING_FROM_REGISTERS,
    compute_ytd_vs_register_comparison,
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
