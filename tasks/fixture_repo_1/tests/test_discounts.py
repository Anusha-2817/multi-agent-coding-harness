from decimal import Decimal

from pricing.discounts import discount_for, tier_for


def test_single_unit_is_standard():
    assert tier_for(1).label == "standard"


def test_below_the_bulk_threshold_is_standard():
    assert tier_for(9).label == "standard"


def test_above_the_bulk_threshold_is_bulk():
    assert tier_for(11).label == "bulk"


def test_below_the_wholesale_threshold_is_still_bulk():
    assert tier_for(49).label == "bulk"


def test_above_the_wholesale_threshold_is_wholesale():
    assert tier_for(51).label == "wholesale"


def test_a_very_large_order_is_distributor():
    assert tier_for(250).label == "distributor"


def test_standard_tier_gets_nothing_off():
    assert discount_for(Decimal("37.75"), 5) == Decimal("0.00")


def test_bulk_tier_gets_five_percent_off():
    assert discount_for(Decimal("150.00"), 12) == Decimal("7.50")
