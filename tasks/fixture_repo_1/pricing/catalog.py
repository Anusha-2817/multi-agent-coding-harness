"""The product catalogue.

Static for now. When this moves behind a real datastore, `lookup` is the only
function that has to change.
"""

from dataclasses import dataclass
from decimal import Decimal


class UnknownSku(LookupError):
    """Raised when a SKU is not in the catalogue."""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price: Decimal


_PRODUCTS = (
    Product("WID-1", "Widget", Decimal("12.50")),
    Product("GAD-2", "Gadget", Decimal("4.25")),
    Product("DOO-3", "Doohickey", Decimal("99.00")),
)

_BY_SKU = {product.sku: product for product in _PRODUCTS}


def lookup(sku: str) -> Product:
    """Find a product by SKU, or raise `UnknownSku`."""
    try:
        return _BY_SKU[sku]
    except KeyError:
        raise UnknownSku(sku) from None


def all_products() -> tuple[Product, ...]:
    return _PRODUCTS
