from labels.units import abbreviate, unit_name


def test_one_of_something_reads_singular():
    assert unit_name("box", 1) == "box"


def test_more_than_one_reads_plural():
    assert unit_name("case", 3) == "cases"


def test_zero_reads_plural():
    assert unit_name("case", 0) == "cases"


def test_an_irregular_unit_reads_from_the_table():
    assert unit_name("foot", 3) == "feet"


def test_a_known_unit_abbreviates():
    assert abbreviate("kilogram") == "kg"


def test_an_unknown_unit_is_left_alone():
    assert abbreviate("case") == "case"
