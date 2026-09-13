from decimal import Decimal

from ar_mis.webapp.formatting import format_inr


def test_format_inr_groups_lakh_and_crore_correctly():
    assert format_inr(Decimal("100000.00")) == "1,00,000.00"
    assert format_inr(Decimal("10000000.00")) == "1,00,00,000.00"
    assert format_inr(Decimal("123456789.50")) == "12,34,56,789.50"


def test_format_inr_leaves_small_numbers_ungrouped():
    assert format_inr(Decimal("100.00")) == "100.00"
    assert format_inr(Decimal("0.00")) == "0.00"


def test_format_inr_groups_four_and_five_digit_numbers_like_western():
    # Indian and Western grouping only diverge from the second comma
    # onward - a single group of 4 or 5 digits looks the same either way.
    assert format_inr(Decimal("1234.56")) == "1,234.56"
    assert format_inr(Decimal("43840.00")) == "43,840.00"


def test_format_inr_handles_negative_amounts():
    assert format_inr(Decimal("-43840.00")) == "-43,840.00"
    assert format_inr(Decimal("-100000.00")) == "-1,00,000.00"


def test_format_inr_returns_empty_string_for_none():
    assert format_inr(None) == ""


def test_format_inr_rounds_to_requested_decimals():
    # Decimal's default rounding (ROUND_HALF_EVEN) applies, same as
    # every other Decimal quantize already used in this codebase.
    assert format_inr(Decimal("100000.005"), decimals=2) == "1,00,000.00"
    assert format_inr(Decimal("100000"), decimals=0) == "1,00,000"


def test_format_inr_accepts_plain_int_and_float():
    assert format_inr(100000) == "1,00,000.00"
    assert format_inr(2770, decimals=0) == "2,770"
