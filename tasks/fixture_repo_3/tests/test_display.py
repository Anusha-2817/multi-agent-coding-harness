from labels.display import label, quantity


def test_a_single_item_reads_singular():
    assert quantity(1, "case") == "1 case"


def test_several_items_read_plural():
    assert quantity(4, "case") == "4 cases"


def test_a_full_label():
    assert label(3, "case", "screws") == "3 cases of screws"


def test_a_label_with_an_irregular_unit():
    assert label(2, "foot", "cable") == "2 feet of cable"


def test_a_label_with_a_vowel_y_unit():
    assert label(5, "tray", "seedlings") == "5 trays of seedlings"


def test_one_reads_singular_in_a_label():
    assert label(1, "metre", "rope") == "1 metre of rope"
