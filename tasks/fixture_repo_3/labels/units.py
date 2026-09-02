"""Units of sale.

A product is sold by some unit -- a box, a metre, a case. The unit is stored in
the singular; how it reads on a label depends on the quantity.
"""

from labels.plurals import pluralize

ABBREVIATIONS = {
    "metre": "m",
    "kilogram": "kg",
    "litre": "l",
}


def unit_name(unit: str, count: int) -> str:
    """`unit` as it should read for a quantity of `count`."""
    if count == 1:
        return unit
    return pluralize(unit)


def abbreviate(unit: str) -> str:
    """The short form of a unit, or the unit itself if it has none."""
    return ABBREVIATIONS.get(unit, unit)
