from decimal import Decimal

import pytest

from pricing.catalog import UnknownSku
from pricing.orders import Order, OrderLine, line_subtotal, subtotal, total


def order(*lines: OrderLine) -> Order:
    return Order(reference="SO-1001", lines=lines)


def test_line_subtotal_multiplies_by_unit_price():
    assert line_subtotal(OrderLine("WID-1", 4)) == Decimal("50.00")


def test_unknown_sku_is_rejected():
    with pytest.raises(UnknownSku):
        line_subtotal(OrderLine("NOPE-9", 1))


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        OrderLine("WID-1", 0)


def test_subtotal_sums_every_line():
    assert subtotal(order(OrderLine("WID-1", 2), OrderLine("GAD-2", 3))) == Decimal("37.75")


def test_a_small_order_gets_no_discount():
    assert total(order(OrderLine("GAD-2", 3))) == Decimal("12.75")


def test_an_order_at_the_bulk_threshold_is_discounted():
    assert total(order(OrderLine("WID-1", 10))) == Decimal("118.75")


def test_an_order_above_the_bulk_threshold_is_discounted():
    assert total(order(OrderLine("WID-1", 12))) == Decimal("142.50")


def test_a_wholesale_order_gets_ten_percent_off():
    assert total(order(OrderLine("GAD-2", 60))) == Decimal("229.50")
