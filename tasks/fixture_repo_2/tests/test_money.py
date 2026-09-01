from decimal import Decimal

from billing.money import apply_rate, to_cents


def test_rounds_to_two_places():
    assert to_cents(Decimal("2.344")) == Decimal("2.34")


def test_rounds_half_up():
    assert to_cents(Decimal("1.005")) == Decimal("1.01")


def test_apply_rate_takes_a_percentage():
    assert apply_rate(Decimal("125.00"), Decimal("0.05")) == Decimal("6.25")


def test_apply_rate_rounds_its_result():
    assert apply_rate(Decimal("10.01"), Decimal("0.05")) == Decimal("0.50")
