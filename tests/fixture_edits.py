"""Edit generators for `fixture_repo_1`. Test support, not a test module.

Loop tests need edits that make the fixture's suite genuinely red or genuinely
green, because the Tester is real and runs real pytest -- there is no flag to
set. They are built by reading the fixture's `discounts.py` and substituting into
it, never by inlining a module body here. An inlined copy would be a second
source of truth for the fixture and would rot the first time the fixture changed,
in a way no test would catch: the copy would still compile and still fail.

`FileEdit` is full file replacement, so each generator returns whole contents.

Successive failing variants must differ **byte-wise**, not just semantically.
Every attempt starts from an identical baseline, so two edits that both merely
leave the bug in place render byte-identical diffs -- which trips the livelock
check on attempt 2 and halts a run the test meant to send around the retry path.
The `# attempt N` marker is what keeps them distinct. Feeding the *same* marker
twice is therefore how a test asks for a livelock; there is no separate generator
for it.
"""

from __future__ import annotations

from pathlib import Path

from harness.state import FileEdit

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

#: The one file the fixture's bug lives in, relative and posix -- `FileEdit.path`
#: is compared against `Plan.target_files` by the scope check, so the spelling
#: has to be stable.
DISCOUNTS = "pricing/discounts.py"

# The injected bug: `>` where `>=` was meant, so a quantity landing exactly on a
# tier boundary gets the tier below.
BUG = "if quantity > tier.min_quantity:"
FIX = "if quantity >= tier.min_quantity:"


def baseline(fixture: Path = FIXTURE) -> str:
    """The fixture's `discounts.py` as written, bug included."""
    text = (fixture / DISCOUNTS).read_text(encoding="utf-8")
    if BUG not in text:
        raise AssertionError(
            f"{DISCOUNTS} no longer contains {BUG!r}. The fixture's bug moved or "
            f"was fixed; these generators need updating."
        )
    return text


def failing_edits(attempt: int, fixture: Path = FIXTURE) -> list[FileEdit]:
    """An edit that leaves the bug in place, marked so attempts differ byte-wise.

    A trailing comment is the cheapest thing that changes the file's bytes without
    changing its behaviour, which is exactly what a "wrong but distinct" attempt
    needs to be.
    """
    return [FileEdit(path=DISCOUNTS, new_content=f"{baseline(fixture)}\n# attempt {attempt}\n")]


def fixing_edits(fixture: Path = FIXTURE) -> list[FileEdit]:
    """The one-character fix the fixture is built to need."""
    fixed = baseline(fixture).replace(BUG, FIX)
    if BUG in fixed or FIX not in fixed:
        raise AssertionError(f"substituting {FIX!r} for {BUG!r} did not take")
    return [FileEdit(path=DISCOUNTS, new_content=fixed)]


def broken_edits(fixture: Path = FIXTURE) -> list[FileEdit]:
    """An edit that leaves `discounts.py` unparseable.

    Drives the branch the Implementer cannot otherwise reach: pytest fails to
    collect, so there is no per-test outcome set, `summary_parsed` is False, and
    the loop must escalate rather than spend another attempt aiming at a failing
    test that does not exist.
    """
    return [FileEdit(path=DISCOUNTS, new_content=f"{baseline(fixture)}\ndef (:\n")]


def write_edits(repo_path: str | Path, edits: list[FileEdit]) -> None:
    """Write `edits` into the run directory.

    Stands in for the harness's apply step, which is Phase 3. Test support only:
    no baseline capture, no diff, no approval gate. Do not grow this into the
    real one -- the real one lands behind the human-approval gate, and a helper
    that quietly applies edits is the shape of the thing that invariant exists to
    prevent.
    """
    for edit in edits:
        target = Path(repo_path) / edit.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit.new_content, encoding="utf-8", newline="\n")
