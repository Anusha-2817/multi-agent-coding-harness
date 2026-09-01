"""Sales commission.

An agent earns a percentage of each sale they close.

Commission is money leaving the business, so a partial cent is never paid out:
the amount is truncated to whole cents. An agent is never credited a fraction of
a cent they have not earned.
"""

from decimal import Decimal

from billing.money import apply_rate


def commission_for(sale: Decimal, rate: Decimal) -> Decimal:
    """What the agent earns on a sale of `sale` at `rate`."""
    return apply_rate(sale, rate)
