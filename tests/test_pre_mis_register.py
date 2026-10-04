from datetime import date
from decimal import Decimal

from ar_mis.models import CustomerMasterRecord, PreMisAdjustment
from ar_mis.pre_mis_register import compute_pre_mis_register


def test_original_seed_is_reconstructed_from_current_balance_minus_adjustments():
    customers = {
        ("ACME", "B1"): CustomerMasterRecord(
            party_id="ACME", party_name="Acme", branch_id="B1", pre_mis_outstanding=Decimal("3000.00")
        ),
    }
    adjustments = [
        PreMisAdjustment(party_id="ACME", branch_id="B1", amount=Decimal("-2000.00"),
                          reason="CN 1: pre-MIS", adjusted_by="maker", adjusted_at=date(2026, 5, 1)),
    ]
    rows = compute_pre_mis_register(customers, adjustments)
    assert len(rows) == 1
    assert rows[0].original_seed == Decimal("5000.00")  # 3000 current + 2000 reversed back out
    assert rows[0].total_adjustments == Decimal("-2000.00")
    assert rows[0].current_balance == Decimal("3000.00")


def test_a_party_fully_adjusted_away_to_zero_still_shows():
    customers = {
        ("ACME", "B1"): CustomerMasterRecord(
            party_id="ACME", party_name="Acme", branch_id="B1", pre_mis_outstanding=Decimal("0.00")
        ),
    }
    adjustments = [
        PreMisAdjustment(party_id="ACME", branch_id="B1", amount=Decimal("-5000.00"),
                          reason="CN 1: pre-MIS", adjusted_by="maker", adjusted_at=date(2026, 5, 1)),
    ]
    rows = compute_pre_mis_register(customers, adjustments)
    assert len(rows) == 1
    assert rows[0].original_seed == Decimal("5000.00")
    assert rows[0].current_balance == Decimal("0.00")


def test_a_party_with_no_pre_mis_history_at_all_is_excluded():
    customers = {
        ("NEWCO", "B1"): CustomerMasterRecord(
            party_id="NEWCO", party_name="New Co", branch_id="B1", pre_mis_outstanding=Decimal("0.00")
        ),
    }
    rows = compute_pre_mis_register(customers, [])
    assert rows == []


def test_multiple_adjustments_for_the_same_party_are_summed():
    customers = {
        ("ACME", "B1"): CustomerMasterRecord(
            party_id="ACME", party_name="Acme", branch_id="B1", pre_mis_outstanding=Decimal("1000.00")
        ),
    }
    adjustments = [
        PreMisAdjustment(party_id="ACME", branch_id="B1", amount=Decimal("-2000.00"),
                          reason="CN 1", adjusted_by="maker", adjusted_at=date(2026, 5, 1)),
        PreMisAdjustment(party_id="ACME", branch_id="B1", amount=Decimal("-1000.00"),
                          reason="CN 2", adjusted_by="maker", adjusted_at=date(2026, 6, 1)),
    ]
    rows = compute_pre_mis_register(customers, adjustments)
    assert rows[0].total_adjustments == Decimal("-3000.00")
    assert rows[0].original_seed == Decimal("4000.00")  # 1000 + 3000
