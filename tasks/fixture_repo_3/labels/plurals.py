"""Turning a singular noun into its plural.

Most words follow a rule. A few do not, and those live in `IRREGULAR`.
"""

IRREGULAR = {
    "child": "children",
    "foot": "feet",
    "person": "people",
    "tooth": "teeth",
}

VOWELS = "aeiou"


def _regular_plural(word: str) -> str:
    """The plural of a word the rules cover."""
    if len(word) > 1 and word.endswith("y") and word[-2] not in VOWELS:
        return word[:-1] + "ies"
    return word + "s"


def pluralize(word: str) -> str:
    """The plural of `word`."""
    if not word:
        raise ValueError("cannot pluralize an empty string")
    if word in IRREGULAR:
        return IRREGULAR[word]
    return _regular_plural(word)
