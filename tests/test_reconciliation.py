from datetime import date
from decimal import Decimal

from ar_mis.models import LedgerEntry, Voucher, VoucherType
from ar_mis.reconciliation import (
    attribute_week,
    isolate_drift,
    reconcile_all_parties,
    reconcile_party,
    ytd_cross_check_party,
)
from ar_mis.rollforward import PartyMovement


def _movement(party, opening, sales=0, cn=0, dn=0, receipts=0, journals=0):
    return PartyMovement(
        party_ledger_name=party,
        opening=Decimal(str(opening)),
        sales=Decimal(str(sales)),
        credit_notes=Decimal(str(cn)),
        debit_notes=Decimal(str(dn)),
        receipts=Decimal(str(receipts)),
        journals=Decimal(str(journals)),
    )


def test_reconcile_party_passes_on_exact_match():
    m = _movement("Acme", opening=50000, sales=100000, receipts=-80000)
    result = reconcile_party("Acme", "KOL", m, closing_extracted=Decimal("70000.00"))
    assert result.reconciled is True
    assert result.difference == Decimal("0.00")


def test_reconcile_party_fails_on_one_paisa_mismatch_zero_tolerance():
    m = _movement("Acme", opening=50000, sales=100000, receipts=-80000)
    result = reconcile_party("Acme", "KOL", m, closing_extracted=Decimal("70000.01"))
    assert result.reconciled is False
    assert result.difference == Decimal("-0.01")


def test_reconcile_all_parties_flags_party_missing_from_ytd_extract():
    movements = {"Acme": _movement("Acme", opening=0, sales=100000)}
    results = reconcile_all_parties("KOL", movements, closing_extracted_by_party={})
    assert len(results) == 1
    assert results[0].reconciled is False


def test_reconcile_all_parties_flags_party_missing_from_appended_build():
    # A party with a Tally closing balance but no movement rows at all -
    # e.g. a party that existed before this pipeline went live and has had
    # zero activity, or a genuine gap in extraction. Either way, silent
    # omission is worse than a loud fail.
    results = reconcile_all_parties(
        "KOL", movements={}, closing_extracted_by_party={"Ghost Ltd": Decimal("15000.00")}
    )
    assert len(results) == 1
    assert results[0].reconciled is False


def test_ytd_cross_check_agrees():
    assert ytd_cross_check_party("Acme", Decimal("70000.00"), Decimal("70000.00")) is True


def test_ytd_cross_check_disagrees():
    assert ytd_cross_check_party("Acme", Decimal("70000.00"), Decimal("83000.00")) is False


def _voucher(vtype, party, raw_amount, vnum, vdate):
    return Voucher(
        voucher_type=vtype,
        voucher_date=vdate,
        voucher_number=vnum,
        branch_id="KOL",
        entries=[LedgerEntry(party_ledger_name=party, amount_as_extracted=Decimal(raw_amount))],
    )


def test_attribute_week_finds_the_containing_week():
    boundaries = [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]
    assert attribute_week(date(2026, 1, 8), boundaries) == date(2026, 1, 12)
    assert attribute_week(date(2026, 1, 5), boundaries) == date(2026, 1, 5)


def test_attribute_week_returns_none_for_voucher_beyond_known_weeks():
    boundaries = [date(2026, 1, 5)]
    assert attribute_week(date(2026, 1, 20), boundaries) is None


def test_isolate_drift_finds_backdated_voucher_and_its_week():
    party = "Acme"
    # Week of Jan 5 was already run and logged with these two vouchers.
    logged = {
        ("Sales", "SB/1", date(2026, 1, 3).isoformat(), "5000.00"),
        ("Receipt", "RCT/1", date(2026, 1, 4).isoformat(), "-2000.00"),
    }
    # Fresh YTD pull reveals a third Sales voucher dated Jan 4 (inside the
    # already-closed Jan 5 week) that the original run never saw - the
    # canonical backdated-entry scenario Section 4.2 exists to catch.
    fresh_vouchers = [
        _voucher(VoucherType.SALES, party, "-5000.00", "SB/1", date(2026, 1, 3)),
        _voucher(VoucherType.RECEIPT, party, "2000.00", "RCT/1", date(2026, 1, 4)),
        _voucher(VoucherType.SALES, party, "-13000000.00", "SB/999-BACKDATED", date(2026, 1, 4)),
    ]
    findings = isolate_drift(
        branch_id="KOL",
        fresh_ytd_vouchers=fresh_vouchers,
        party_ledger_names={party},
        logged_keys_by_party={party: logged},
        week_boundaries=[date(2026, 1, 5), date(2026, 1, 12)],
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.voucher_number == "SB/999-BACKDATED"
    assert finding.attributed_week == date(2026, 1, 5)
    assert finding.flipped_amount == Decimal("13000000.00")


def test_isolate_drift_ignores_non_party_ledger_lines():
    fresh_vouchers = [_voucher(VoucherType.SALES, "Freight Income", "5000.00", "SB/2", date(2026, 1, 4))]
    findings = isolate_drift(
        branch_id="KOL",
        fresh_ytd_vouchers=fresh_vouchers,
        party_ledger_names={"Acme"},
        logged_keys_by_party={},
        week_boundaries=[date(2026, 1, 5)],
    )
    assert findings == []
