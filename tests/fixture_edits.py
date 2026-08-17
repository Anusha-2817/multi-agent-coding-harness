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
from harness.workspace import apply_edits

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

#: The one file the fixture's bug lives in, relative and posix -- `FileEdit.path`
#: is compared against `Plan.target_files` by the scope check, so the spelling
#: has to be stable.
DISCOUNTS = "pricing/discounts.py"

#: A real file in the fixture that no plan targets. Used to drive the scope check.
MONEY = "pricing/money.py"

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


def out_of_scope_edits(fixture: Path = FIXTURE) -> list[FileEdit]:
    """An edit to a real fixture file that no plan targets.

    Drives the mechanical scope check. Read from `pricing/money.py` rather than
    inlined for the reason at the top of this module -- an inlined copy would rot
    the first time the fixture changed, and nothing would catch it.

    Note what is *not* here: an `attempt` marker. The failing variants need one
    because they render diffs that must differ byte-wise, but a scope-violating
    attempt is caught before the render and contributes nothing to
    `previous_diffs`. Two identical out-of-scope edits therefore cannot livelock;
    they retry until the cap. That is a real property of the loop, and this
    generator being marker-free is the honest way to test it.
    """
    text = (fixture / MONEY).read_text(encoding="utf-8")
    return [FileEdit(path=MONEY, new_content=f"{text}\n# touched by the implementer\n")]


def broken_edits(fixture: Path = FIXTURE) -> list[FileEdit]:
    """An edit that leaves `discounts.py` unparseable.

    Drives the branch the Implementer cannot otherwise reach: pytest fails to
    collect, so there is no per-test outcome set, `summary_parsed` is False, and
    the loop must escalate rather than spend another attempt aiming at a failing
    test that does not exist.
    """
    return [FileEdit(path=DISCOUNTS, new_content=f"{baseline(fixture)}\ndef (:\n")]


def write_edits(repo_path: str | Path, edits: list[FileEdit]) -> None:
    """Apply `edits` with no gate in front of them. Test support only.

    Delegates to the real `workspace.apply_edits`, so there is exactly one
    function in the project that writes a file the Implementer produced. What
    this adds is a name for the bypass: the real apply is only ever called by the
    control loop, after a Review verdict and a human approval on that specific
    diff. Invariant 1 is a property of *that call site*, and every use of this
    helper is a test deliberately stepping around it.

    Which is why it stays in `tests/`. A helper in `harness/` that quietly
    applies edits is the shape of the thing invariant 1 exists to prevent.
    """
    apply_edits(repo_path, edits)
