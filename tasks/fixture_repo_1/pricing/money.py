"""Rounding helpers.

Every amount that reaches a customer is rounded to whole cents here, so the
rest of the package can multiply freely and round once at the boundary.
"""

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def to_cents(amount: Decimal) -> Decimal:
    """Round to whole cents, half away from zero -- what an invoice does."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def apply_rate(amount: Decimal, rate: Decimal) -> Decimal:
    """The portion of `amount` at `rate`, rounded to cents."""
    return to_cents(amount * rate)
