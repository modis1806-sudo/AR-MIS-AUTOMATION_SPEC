from decimal import Decimal

from ar_mis.money import difference, is_match, to_money


def test_to_money_rounds_to_paise():
    assert to_money("125000.505") == Decimal("125000.51")
    assert to_money(10) == Decimal("10.00")


def test_to_money_parses_from_string_not_float():
    # 0.1 + 0.2 != 0.3 in binary float; must not leak into money values.
    a = to_money("0.10")
    b = to_money("0.20")
    assert a + b == to_money("0.30")


def test_zero_tolerance_match():
    assert is_match(Decimal("1000.00"), Decimal("1000.00"))
    assert not is_match(Decimal("1000.00"), Decimal("1000.01"))


def test_difference():
    assert difference(Decimal("1000.00"), Decimal("999.99")) == Decimal("0.01")
