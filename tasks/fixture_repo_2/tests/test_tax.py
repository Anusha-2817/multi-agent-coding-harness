from decimal import Decimal

import pytest

from billing.tax import UnknownJurisdiction, rate_for, tax_for


def test_tax_is_a_percentage_of_the_subtotal():
    assert tax_for(Decimal("200.00"), Decimal("0.10")) == Decimal("20.00")


def test_a_half_cent_of_tax_rounds_up():
    # 12.90 at 5% is 0.6450 exactly. Tax is billed to the customer and rounds
    # half away from zero, so the invoice shows 0.65 and not 0.64.
    assert tax_for(Decimal("12.90"), Decimal("0.05")) == Decimal("0.65")


def test_a_zero_rate_is_untaxed():
    assert tax_for(Decimal("99.99"), Decimal("0.00")) == Decimal("0.00")


def test_a_known_jurisdiction_has_a_rate():
    assert rate_for("CA") == Decimal("0.0725")


def test_an_unknown_jurisdiction_raises():
    with pytest.raises(UnknownJurisdiction):
        rate_for("ZZ")
