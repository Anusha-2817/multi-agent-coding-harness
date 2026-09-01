"""Sales tax.

One rate per jurisdiction, applied to the invoice subtotal. Jurisdictions are
static for now; when they move behind a real datastore, `rate_for` is the only
function that has to change.
"""

from decimal import Decimal

from billing.money import apply_rate


class UnknownJurisdiction(LookupError):
    """Raised when a jurisdiction code has no registered rate."""


_RATES = {
    "CA": Decimal("0.0725"),
    "NY": Decimal("0.04"),
    "TX": Decimal("0.0625"),
    "OR": Decimal("0.00"),
}


def rate_for(jurisdiction: str) -> Decimal:
    """The tax rate for a jurisdiction code, or raise `UnknownJurisdiction`."""
    try:
        return _RATES[jurisdiction]
    except KeyError:
        raise UnknownJurisdiction(jurisdiction) from None


def tax_for(subtotal: Decimal, rate: Decimal) -> Decimal:
    """The tax due on `subtotal` at `rate`."""
    return apply_rate(subtotal, rate)
