"""Assembling the label a customer sees.

One line per catalogue entry: a quantity, its unit, and the product.
"""

from labels.units import unit_name


def quantity(count: int, unit: str) -> str:
    """A count and its unit: "1 box", "3 boxes"."""
    return f"{count} {unit_name(unit, count)}"


def label(count: int, unit: str, product: str) -> str:
    """A full catalogue line: "3 cases of screws"."""
    return f"{quantity(count, unit)} of {product}"
