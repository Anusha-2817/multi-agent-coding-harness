"""Volume discounts.

One tier applies to the whole order, chosen by the total number of units. The
tiers are inclusive lower bounds: an order of exactly `min_quantity` units
qualifies for that tier.
"""

from dataclasses import dataclass
from decimal import Decimal

from pricing.money import apply_rate


@dataclass(frozen=True)
class Tier:
    label: str
    min_quantity: int
    rate: Decimal


TIERS = (
    Tier("standard", 1, Decimal("0.00")),
    Tier("bulk", 10, Decimal("0.05")),
    Tier("wholesale", 50, Decimal("0.10")),
    Tier("distributor", 100, Decimal("0.15")),
)


def tier_for(quantity: int) -> Tier:
    """The best tier `quantity` units qualify for.

    Tiers are ordered cheapest first, so the last one that matches wins.
    """
    selected = TIERS[0]
    for tier in TIERS:
        if quantity > tier.min_quantity:
            selected = tier
    return selected


def discount_for(subtotal: Decimal, quantity: int) -> Decimal:
    """Money off `subtotal` for ordering `quantity` units in total."""
    return apply_rate(subtotal, tier_for(quantity).rate)
