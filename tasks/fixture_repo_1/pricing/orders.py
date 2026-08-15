"""Orders and what they cost.

The discount is decided by the order's total unit count, not per line -- ten
units of one product and ten units spread over three products get the same tier.
"""

from dataclasses import dataclass
from decimal import Decimal

from pricing import catalog
from pricing.discounts import discount_for
from pricing.money import to_cents


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError(f"quantity must be at least 1, got {self.quantity}")


@dataclass(frozen=True)
class Order:
    reference: str
    lines: tuple[OrderLine, ...]


def line_subtotal(line: OrderLine) -> Decimal:
    """What one line costs before any discount."""
    return to_cents(catalog.lookup(line.sku).unit_price * line.quantity)


def subtotal(order: Order) -> Decimal:
    """The order's cost before any discount."""
    return to_cents(sum((line_subtotal(line) for line in order.lines), Decimal("0")))


def total_quantity(order: Order) -> int:
    """How many units the order is for, across all lines."""
    return sum(line.quantity for line in order.lines)


def total(order: Order) -> Decimal:
    """What the customer pays."""
    gross = subtotal(order)
    return to_cents(gross - discount_for(gross, total_quantity(order)))
