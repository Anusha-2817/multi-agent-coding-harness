from decimal import Decimal

import pytest

from billing.invoices import Invoice, payout, tax, total


def an_invoice(subtotal="100.00", tax_rate="0.0725") -> Invoice:
    return Invoice("INV-2001", Decimal(subtotal), Decimal(tax_rate))


def test_tax_is_the_subtotal_at_the_invoices_rate():
    assert tax(an_invoice()) == Decimal("7.25")


def test_the_total_is_subtotal_plus_tax():
    assert total(an_invoice()) == Decimal("107.25")


def test_a_tax_free_invoice_totals_its_subtotal():
    assert total(an_invoice(tax_rate="0.00")) == Decimal("100.00")


def test_the_payout_is_a_commission_on_the_subtotal():
    assert payout(an_invoice(subtotal="200.00"), Decimal("0.10")) == Decimal("20.00")


def test_the_payout_ignores_the_tax():
    # Tax is collected for the jurisdiction and is not revenue, so a change of
    # tax rate must not change what the agent earns.
    taxed = payout(an_invoice(subtotal="200.00", tax_rate="0.0725"), Decimal("0.10"))
    untaxed = payout(an_invoice(subtotal="200.00", tax_rate="0.00"), Decimal("0.10"))
    assert taxed == untaxed


def test_a_negative_subtotal_is_rejected():
    with pytest.raises(ValueError):
        Invoice("INV-2002", Decimal("-1.00"), Decimal("0.00"))
