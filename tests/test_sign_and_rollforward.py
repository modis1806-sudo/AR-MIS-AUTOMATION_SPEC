from datetime import date
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, LedgerEntry, Voucher, VoucherType, WeeklySnapshotRow
from ar_mis.rollforward import aggregate_party_movements, resolve_opening_balances
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


def test_resolve_opening_balances_uses_pre_mis_outstanding_for_first_week(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    openings = resolve_opening_balances(store, {"Acme"}, "KOL")
    assert openings == {"Acme": Decimal("100000.00")}
    store.close()


def test_resolve_opening_balances_uses_prior_week_closing_after_first_week(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_customer_master(CustomerMasterRecord("Acme", "Acme", "KOL", Decimal("100000.00")))
    store.append_weekly_snapshot(
        WeeklySnapshotRow(
            party_id="Acme",
            branch_id="KOL",
            week_ending=date(2026, 1, 5),
            opening=Decimal("100000.00"),
            sales=Decimal("20000.00"),
            credit_notes=Decimal("0.00"),
            debit_notes=Decimal("0.00"),
            receipts=Decimal("-15000.00"),
            journals=Decimal("0.00"),
            closing_computed=Decimal("105000.00"),
            closing_extracted=Decimal("105000.00"),
            reconciled=True,
            difference=Decimal("0.00"),
        )
    )
    openings = resolve_opening_balances(store, {"Acme"}, "KOL")
    # Must use last week's closing (105000), not re-seed from the
    # Pre-MIS Outstanding baseline (100000) now that a prior week exists.
    assert openings == {"Acme": Decimal("105000.00")}
    store.close()
