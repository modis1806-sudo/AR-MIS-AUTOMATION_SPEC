from datetime import date, datetime
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    LedgerEntry,
    NoteType,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
)
from ar_mis.rollforward import (
    aggregate_party_movements,
    aggregate_party_movements_from_registers,
    compute_party_closings_as_of,
)
from ar_mis.sign import flip_sign
from ar_mis.storage import Store


def test_flip_sign_debit_becomes_positive():
    # Tally: debit entries negative at source.
    assert flip_sign(Decimal("-125000.00")) == Decimal("125000.00")


def test_flip_sign_credit_becomes_negative():
    assert flip_sign(Decimal("50000.00")) == Decimal("-50000.00")


def _voucher(vtype, party, raw_amount, vnum="V1", vdate=date(2026, 4, 6)):
    return Voucher(
        voucher_type=vtype,
        voucher_date=vdate,
        voucher_number=vnum,
        branch_id="KOL",
        entries=[LedgerEntry(party_ledger_name=party, amount_as_extracted=Decimal(raw_amount))],
    )


def test_roll_forward_pure_addition_matches_spec_formula():
    party = "Acme Corp"
    vouchers = [
        _voucher(VoucherType.SALES, party, "-100000.00", "SB/1"),   # Dr, becomes +100000
        _voucher(VoucherType.CREDIT_NOTE, party, "10000.00", "CN/1"),  # Cr, becomes -10000
        _voucher(VoucherType.RECEIPT, party, "80000.00", "RCT/1"),  # Cr, becomes -80000
    ]
    movements = aggregate_party_movements(vouchers, {party}, {party: Decimal("50000.00")})
    m = movements[party]
    assert m.opening == Decimal("50000.00")
    assert m.sales == Decimal("100000.00")
    assert m.credit_notes == Decimal("-10000.00")
    assert m.receipts == Decimal("-80000.00")
    assert m.debit_notes == Decimal("0.00")
    assert m.journals == Decimal("0.00")
    # 50000 + 100000 - 10000 + 0 - 80000 + 0 = 60000
    assert m.closing_computed == Decimal("60000.00")


def test_journal_can_increase_debtor_balance_without_special_casing():
    party = "Acme Corp"
    # A journal that debits the party (raw negative at source) increases
    # the debtor balance - same pure-addition formula, no per-type sign.
    vouchers = [_voucher(VoucherType.JOURNAL, party, "-20000.00", "JV/1")]
    movements = aggregate_party_movements(vouchers, {party}, {party: Decimal("0.00")})
    assert movements[party].journals == Decimal("20000.00")
    assert movements[party].closing_computed == Decimal("20000.00")


def test_journal_can_decrease_debtor_balance_without_special_casing():
    party = "Acme Corp"
    # A journal that credits the party (raw positive at source) decreases it.
    vouchers = [_voucher(VoucherType.JOURNAL, party, "20000.00", "JV/2")]
    movements = aggregate_party_movements(vouchers, {party}, {party: Decimal("100000.00")})
    assert movements[party].journals == Decimal("-20000.00")
    assert movements[party].closing_computed == Decimal("80000.00")


def test_non_party_ledger_lines_are_excluded():
    # A Sales voucher's income-account leg (e.g. Freight Income) must never
    # be summed into a party's movement just because it's in the same
    # voucher - only lines whose ledger name is in party_ledger_names count.
    party = "Acme Corp"
    voucher = Voucher(
        voucher_type=VoucherType.SALES,
        voucher_date=date(2026, 4, 6),
        voucher_number="SB/1",
        branch_id="KOL",
        entries=[
            LedgerEntry(party_ledger_name=party, amount_as_extracted=Decimal("-100000.00")),
            LedgerEntry(party_ledger_name="Freight Income", amount_as_extracted=Decimal("100000.00")),
        ],
    )
    movements = aggregate_party_movements([voucher], {party}, {party: Decimal("0.00")})
    assert movements[party].sales == Decimal("100000.00")
    assert "Freight Income" not in movements


def test_compute_party_closings_as_of_uses_pre_mis_outstanding_when_nothing_on_record(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    movements = compute_party_closings_as_of(store, "KOL", {"Acme"}, date(2026, 1, 5))
    assert movements["Acme"].closing_computed == Decimal("100000.00")
    store.close()


def test_compute_party_closings_as_of_sums_every_movement_type_on_or_before_as_of(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 1, 1), note_type=NoteType.INVOICE,
            voucher_number="SB/1", bill_allocation_reference="SB/1", party_id="Acme",
            taxable_value=Decimal("20000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("20000.00"), due_date=date(2026, 2, 1),
        )
    )
    store.append_receipt_journal_rows(
        [
            ReceiptJournalRegisterRow(
                branch_id="KOL", txn_date=date(2026, 1, 3), voucher_type="Receipt", voucher_number="RCT/1",
                party_id="Acme", amount=Decimal("15000.00"),
            )
        ]
    )
    # Pre-MIS 100000 + sales 20000 - receipt 15000 (raw Cr +15000, flipped
    # to -15000) = 105000 - no weekly_snapshot row involved anywhere.
    movements = compute_party_closings_as_of(store, "KOL", {"Acme"}, date(2026, 1, 6))
    assert movements["Acme"].closing_computed == Decimal("105000.00")
    store.close()


def test_compute_party_closings_as_of_excludes_rows_dated_after_as_of(tmp_path):
    # The CHARZE INDUSTRIES incident this whole redesign traces back to:
    # a backdated voucher's own real date can land earlier than register
    # rows already on record for later dates. There's no chain left to
    # confuse any more - this is a plain date filter, so a row dated
    # after `as_of` must never leak into an earlier date's own total.
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 1, 12), note_type=NoteType.INVOICE,
            voucher_number="SB/2", bill_allocation_reference="SB/2", party_id="Acme",
            taxable_value=Decimal("20000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("20000.00"), due_date=date(2026, 2, 12),
        )
    )
    movements = compute_party_closings_as_of(store, "KOL", {"Acme"}, date(2026, 1, 5))
    assert movements["Acme"].closing_computed == Decimal("100000.00")
    # The boundary itself is inclusive: a row dated exactly on `as_of` IS
    # counted - "as of" a date means through and including that date.
    movements_on_boundary = compute_party_closings_as_of(store, "KOL", {"Acme"}, date(2026, 1, 12))
    assert movements_on_boundary["Acme"].closing_computed == Decimal("120000.00")
    store.close()


def test_compute_party_closings_as_of_is_unaffected_by_insertion_order(tmp_path):
    # Structurally re-proves the CHARZE bug can't recur: computing a
    # party's position as of an earlier date is a pure sum-by-date over
    # whatever's on record, regardless of which register row was
    # appended first - a later-appended but earlier-dated (backdated)
    # row is included exactly like any other, and a later-dated row
    # already on record has no way to leak backwards into it.
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 1, 19), note_type=NoteType.INVOICE,
            voucher_number="SB/LATER", bill_allocation_reference="SB/LATER", party_id="Acme",
            taxable_value=Decimal("30000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("30000.00"), due_date=date(2026, 2, 19),
        )
    )
    store.append_sales_dn_row(
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 1, 1), note_type=NoteType.INVOICE,
            voucher_number="SB/BACKDATED", bill_allocation_reference="SB/BACKDATED", party_id="Acme",
            taxable_value=Decimal("20000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("20000.00"), due_date=date(2026, 2, 1),
        )
    )
    movements = compute_party_closings_as_of(store, "KOL", {"Acme"}, date(2026, 1, 6))
    assert movements["Acme"].closing_computed == Decimal("120000.00")
    store.close()


# ---- Section 4.2's "TB vs Registers" redesign: movements from Registers --


def test_from_registers_sales_dn_invoice_value_is_already_the_post_flip_amount():
    # Real bug caught by test while building this: invoice_value is a
    # DERIVED, already-positive gross total (built from abs() of the
    # voucher's other entries), not the party's own raw signed entry -
    # flip_sign-ing it on top would wrongly invert a real sale negative.
    party = "Acme Corp"
    sales_dn = [
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.INVOICE,
            voucher_number="SB/1", bill_allocation_reference="SB/1", party_id=party,
            taxable_value=Decimal("100000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("100000.00"), due_date=date(2026, 5, 6),
        )
    ]
    movements = aggregate_party_movements_from_registers(sales_dn, [], [], {party}, {party: Decimal("50000.00")})
    assert movements[party].sales == Decimal("100000.00")
    assert movements[party].closing_computed == Decimal("150000.00")


def test_from_registers_debit_note_also_increases_the_debtor_balance():
    party = "Acme Corp"
    sales_dn = [
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.DEBIT_NOTE,
            voucher_number="DN/1", bill_allocation_reference="DN/1", party_id=party,
            taxable_value=Decimal("5000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("5000.00"), due_date=date(2026, 5, 6),
        )
    ]
    movements = aggregate_party_movements_from_registers(sales_dn, [], [], {party}, {party: Decimal("0.00")})
    assert movements[party].debit_notes == Decimal("5000.00")
    assert movements[party].sales == Decimal("0.00")


def test_from_registers_credit_note_and_receipt_amounts_are_the_raw_signed_entry_and_do_get_flipped():
    # Unlike Sales/DN's invoice_value, CN's cn_amount and Receipt/Journal's
    # amount ARE the party's own raw amount_as_extracted (Tally's at-source
    # convention) - these DO need flip_sign, same as aggregate_party_
    # movements already does for the party's own entry.
    party = "Acme Corp"
    cn_rows = [
        CreditNoteRegisterRow(
            branch_id="KOL", cn_date=date(2026, 4, 6), voucher_number="CN/1", party_id=party,
            cn_amount=Decimal("10000.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    rj_rows = [
        ReceiptJournalRegisterRow(
            branch_id="KOL", txn_date=date(2026, 4, 6), voucher_type="Receipt", voucher_number="RCT/1",
            party_id=party, amount=Decimal("80000.00"),
        ),
        ReceiptJournalRegisterRow(
            branch_id="KOL", txn_date=date(2026, 4, 6), voucher_type="Journal", voucher_number="JV/1",
            party_id=party, amount=Decimal("-20000.00"),
        ),
    ]
    movements = aggregate_party_movements_from_registers([], cn_rows, rj_rows, {party}, {party: Decimal("50000.00")})
    m = movements[party]
    assert m.credit_notes == Decimal("-10000.00")
    assert m.receipts == Decimal("-80000.00")
    assert m.journals == Decimal("20000.00")
    # 50000 + 0 - 10000 + 0 - 80000 + 20000 = -20000
    assert m.closing_computed == Decimal("-20000.00")


def test_from_registers_matches_aggregate_party_movements_for_an_equivalent_voucher_set():
    # Same real scenario, same end result, via the two different bases -
    # confirms the register-based path isn't quietly a different formula,
    # just a different (more honest) source for the same numbers.
    party = "Acme Corp"
    vouchers = [
        _voucher(VoucherType.SALES, party, "-100000.00", "SB/1"),
        _voucher(VoucherType.CREDIT_NOTE, party, "10000.00", "CN/1"),
        _voucher(VoucherType.RECEIPT, party, "80000.00", "RCT/1"),
    ]
    from_vouchers = aggregate_party_movements(vouchers, {party}, {party: Decimal("50000.00")})

    sales_dn = [
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.INVOICE,
            voucher_number="SB/1", bill_allocation_reference="SB/1", party_id=party,
            taxable_value=Decimal("100000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("100000.00"), due_date=date(2026, 5, 6),
        )
    ]
    cn_rows = [
        CreditNoteRegisterRow(
            branch_id="KOL", cn_date=date(2026, 4, 6), voucher_number="CN/1", party_id=party,
            cn_amount=Decimal("10000.00"), classification=RegisterClassification.CURRENT,
        )
    ]
    rj_rows = [
        ReceiptJournalRegisterRow(
            branch_id="KOL", txn_date=date(2026, 4, 6), voucher_type="Receipt", voucher_number="RCT/1",
            party_id=party, amount=Decimal("80000.00"),
        )
    ]
    from_registers = aggregate_party_movements_from_registers(
        sales_dn, cn_rows, rj_rows, {party}, {party: Decimal("50000.00")}
    )
    assert from_registers[party] == from_vouchers[party]


def test_from_registers_a_voucher_missing_from_the_registers_is_simply_absent():
    # The whole point of this redesign: a register-build exclusion (a
    # voucher that never made it into any register, e.g. the Joy Ray/
    # Manoj Lal case before it was fixed) must now visibly starve this
    # total too - never silently counted the way a raw-voucher re-sum
    # would have counted it regardless of register-building success.
    party = "Acme Corp"
    movements = aggregate_party_movements_from_registers([], [], [], {party}, {party: Decimal("50000.00")})
    assert movements[party].sales == Decimal("0.00")
    assert movements[party].closing_computed == Decimal("50000.00")


def test_from_registers_only_counts_rows_for_tracked_parties():
    sales_dn = [
        SalesDNRegisterRow(
            branch_id="KOL", invoice_date=date(2026, 4, 6), note_type=NoteType.INVOICE,
            voucher_number="SB/1", bill_allocation_reference="SB/1", party_id="Not Tracked",
            taxable_value=Decimal("100000.00"), cgst=Decimal("0.00"), sgst=Decimal("0.00"),
            igst=Decimal("0.00"), invoice_value=Decimal("100000.00"), due_date=date(2026, 5, 6),
        )
    ]
    movements = aggregate_party_movements_from_registers(
        sales_dn, [], [], {"Acme Corp"}, {"Acme Corp": Decimal("0.00")}
    )
    assert movements["Acme Corp"].sales == Decimal("0.00")


