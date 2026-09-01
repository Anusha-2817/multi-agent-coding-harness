from decimal import Decimal

from billing.commission import commission_for


def test_commission_is_a_percentage_of_the_sale():
    assert commission_for(Decimal("200.00"), Decimal("0.10")) == Decimal("20.00")


def test_a_partial_cent_is_not_paid_out():
    # 123.45 at 7.5% is 9.258750 exactly. Commission is money leaving the
    # business, so the fraction is dropped rather than rounded up.
    assert commission_for(Decimal("123.45"), Decimal("0.075")) == Decimal("9.25")


def test_a_commission_landing_on_a_whole_cent_is_exact():
    assert commission_for(Decimal("100.00"), Decimal("0.075")) == Decimal("7.50")


def test_a_zero_rate_earns_nothing():
    assert commission_for(Decimal("500.00"), Decimal("0.00")) == Decimal("0.00")


def test_the_rate_applies_to_the_whole_sale():
    assert commission_for(Decimal("1000.00"), Decimal("0.025")) == Decimal("25.00")
