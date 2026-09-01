"""Invoices and what they come to.

An invoice carries a subtotal and the tax rate of the jurisdiction it was raised
in. The agent's payout is a commission on the subtotal, never on the tax -- tax
is collected on behalf of the jurisdiction and is not revenue.
"""

from dataclasses import dataclass
from decimal import Decimal

from billing.commission import commission_for
from billing.money import to_cents
from billing.tax import tax_for


@dataclass(frozen=True)
class Invoice:
    reference: str
    subtotal: Decimal
    tax_rate: Decimal

    def __post_init__(self) -> None:
        if self.subtotal < 0:
            raise ValueError(f"subtotal must not be negative, got {self.subtotal}")


def tax(invoice: Invoice) -> Decimal:
    """The tax due on an invoice."""
    return tax_for(invoice.subtotal, invoice.tax_rate)


def total(invoice: Invoice) -> Decimal:
    """What the customer pays: subtotal plus tax."""
    return to_cents(invoice.subtotal + tax(invoice))


def payout(invoice: Invoice, rate: Decimal) -> Decimal:
    """What the agent earns on an invoice, at their commission rate."""
    return commission_for(invoice.subtotal, rate)
