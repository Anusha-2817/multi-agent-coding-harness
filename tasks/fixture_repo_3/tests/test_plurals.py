import pytest

from labels.plurals import pluralize


def test_a_regular_word_takes_s():
    assert pluralize("screw") == "screws"


def test_a_word_ending_in_a_consonant_and_y_takes_ies():
    assert pluralize("category") == "categories"


def test_a_word_ending_in_a_vowel_and_y_takes_s():
    assert pluralize("tray") == "trays"


def test_an_irregular_word_comes_from_the_table():
    assert pluralize("child") == "children"


def test_a_word_ending_in_a_sibilant_takes_es():
    # "box" ends in a sibilant, so the plural is "boxes", not "boxs". The same
    # rule covers every word ending in s, x, z, ch or sh.
    assert pluralize("box") == "boxes"


def test_an_empty_word_is_rejected():
    with pytest.raises(ValueError):
        pluralize("")
