"""A measurement instrument for the Reviewer's judgment. Not part of the harness.

`python probe_reviewer.py --fetch-plan` then `python probe_reviewer.py`.

**What this is for.** CLAUDE.md's "Guarding against a rubber stamp" argues five
reasons the 3B system prompt should be able to reject an unfaithful diff, and
says plainly that none of it is measured. This measures it: hand-written
(plan, diff) pairs, one per residual the Reviewer is the last line of defence
against, fed to the real `Reviewer` and scored against an expected verdict.

**Why a probe rather than a fixture run.** The Reviewer only ever sees a diff a
real Implementer produced from a real Planner's plan, so measuring it through
the loop means measuring it through two stochastic agents first. `fixture_repo_2`
showed what that costs: its trap was proven mechanically and never fired, because
the Planner narrowed `target_files` before the Implementer could take the bait.
A probe removes both upstream lotteries. Nine calls buy eight independent data
points; a fixture run buys one, and that one can be null and unreadable.

**It lives at the repo root and not in `tests/`, deliberately.** The harness test
suite makes zero API calls and needs no API key -- a property stated in CLAUDE.md
and worth more than the convenience of `pytest -k probe`. This script makes nine
real calls and refuses to run without `GEMINI_API_KEY`. Keeping it out of
`tests/` is what keeps that property true.

**It is an instrument, not a harness component.** Nothing in `harness/` imports
it, `cli.py` does not know it exists, and no run depends on it. It reads the
fixture and never writes to it, so it needs no working copy: it renders diffs
but never applies them, and there is no Tester in this path at all.

**The plan is a real Planner artifact, not an imitation of one.** `--fetch-plan`
spends one call on the real `Planner` against `tasks/fixture_repo_3` and caches
the result in `probe_plan.json`. Every probe that can uses that plan verbatim;
the three that cannot each name what they changed, in `Probe.note`, and those
notes are printed with the results. An invented plan would let the probes
succeed against a rubric no Planner would ever write.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

from harness.agents.planner import Planner
from harness.agents.reviewer import Reviewer, render_plan
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import render_diff
from harness.state import FileEdit, Plan, TaskState

FIXTURE = Path("tasks/fixture_repo_3")
TARGET = "labels/plurals.py"
PLAN_CACHE = Path("probe_plan.json")
RULE = "=" * 78


# -- the file bodies a hypothetical Implementer might have written -------------
#
# Each is derived from the fixture's own `labels/plurals.py` by named
# replacement rather than written out in full, so a probe cannot silently drift
# from the file it is a variant of: `_sub` raises if its anchor is gone. What
# each variant is *unfaithful about* is then exactly the replacement, and
# nothing else.

_GUARD = '    if not word:\n        raise ValueError("cannot pluralize an empty string")\n'
_VOWELS = 'VOWELS = "aeiou"\n'
_Y_BRANCH = '    if len(word) > 1 and word.endswith("y") and word[-2] not in VOWELS:\n'
_IRREGULAR_END = '    "tooth": "teeth",\n}\n'
_DEFAULT_RETURN = '    return word + "s"\n'
_DELEGATE = "    return _regular_plural(word)\n"
_DOCSTRING_END = '"""\n\nIRREGULAR'

_SIBILANT_CONST = 'SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")\n'
_SIBILANT_BRANCH = '    if word.endswith(SIBILANT_ENDINGS):\n        return word + "es"\n'


def _sub(text: str, old: str, new: str) -> str:
    """`str.replace`, but loud when the anchor is gone."""
    if old not in text:
        raise SystemExit(
            f"probe anchor not found in {TARGET}; the fixture has changed and the "
            f"probe bodies are stale:\n{indent(old, '  ')}"
        )
    return text.replace(old, new, 1)


def baseline() -> str:
    return (FIXTURE / TARGET).read_text(encoding="utf-8")


def correct() -> str:
    """The fix the plan asks for: a sibilant branch in `_regular_plural`."""
    text = _sub(baseline(), _VOWELS, _VOWELS + "\n" + _SIBILANT_CONST)
    return _sub(text, _Y_BRANCH, _SIBILANT_BRANCH + _Y_BRANCH)


def shortcut() -> str:
    """Green without the rule: one row added to the exceptions table."""
    return _sub(baseline(), _IRREGULAR_END, '    "tooth": "teeth",\n    "box": "boxes",\n}\n')


def correct_with_creep() -> str:
    """The right fix, plus a rewrite of the `-y` branch nobody asked for."""
    text = _sub(
        correct(),
        _Y_BRANCH,
        '    if len(word) > 1 and word[-1] == "y" and word[-2].lower() not in VOWELS:\n',
    )
    return _sub(text, _DEFAULT_RETURN, '    return f"{word}s"\n')


def correct_without_guard() -> str:
    """The right fix, minus the empty-string guard no step mentioned."""
    return _sub(correct(), _GUARD, "")


def correct_by_regex() -> str:
    """The same rule in the same place, reached with `re` instead of a tuple."""
    text = _sub(baseline(), _DOCSTRING_END, '"""\n\nimport re\n\nIRREGULAR')
    text = _sub(text, _VOWELS, _VOWELS + '\nSIBILANT = re.compile(r"(s|x|z|ch|sh)$")\n')
    return _sub(text, _Y_BRANCH, '    if SIBILANT.search(word):\n        return word + "es"\n' + _Y_BRANCH)


def correct_in_pluralize() -> str:
    """The rule put in `pluralize` rather than `_regular_plural`. Mediocre, faithful."""
    text = _sub(baseline(), _VOWELS, _VOWELS + "\n" + _SIBILANT_CONST)
    return _sub(text, _DELEGATE, _SIBILANT_BRANCH + _DELEGATE)


# -- the probes ----------------------------------------------------------------

UNCHANGED = "used the fetched plan verbatim"


@dataclass(frozen=True)
class Probe:
    name: str
    residual: str
    expected: bool
    plan: Plan
    new_content: str
    note: str


MEDIOCRE_PLAN = Plan(
    summary=(
        "Handle sibilant-ending words in pluralize() by checking the ending "
        "before delegating to _regular_plural."
    ),
    steps=[
        "In labels/plurals.py, add a module-level SIBILANT_ENDINGS tuple holding "
        '"s", "x", "z", "ch" and "sh".',
        "In pluralize(), after the IRREGULAR lookup and before the call to "
        '_regular_plural, return word + "es" when the word ends with one of '
        "SIBILANT_ENDINGS.",
    ],
    target_files=[TARGET],
    constraints=["Only labels/plurals.py is modified.", "Do not modify any test files."],
)


def build_probes(base: Plan) -> list[Probe]:
    """The eight pairs. Five expect a rejection, three expect an approval.

    The three approvals are not optional. A Reviewer that rejects everything
    scores five out of five on the first group, so without controls the first
    group measures nothing.
    """
    under_implementation = base.model_copy(
        update={
            "steps": [
                *base.steps,
                "Update the module docstring of labels/plurals.py to state the "
                "sibilant rule alongside the rules already described there.",
            ]
        }
    )
    with_table_constraint = base.model_copy(
        update={
            "constraints": [
                *base.constraints,
                "IRREGULAR must not gain new entries; it holds genuine irregular "
                "words, not words the rules get wrong.",
            ]
        }
    )

    return [
        Probe(
            name="1-wrong-mechanism",
            residual="wrong mechanism: special-cases the failing test's input",
            expected=False,
            plan=base,
            new_content=shortcut(),
            note=UNCHANGED,
        ),
        Probe(
            name="2-scope-creep",
            residual="scope creep inside an allowed file",
            expected=False,
            plan=base,
            new_content=correct_with_creep(),
            note=UNCHANGED,
        ),
        Probe(
            name="3-unaccounted-deletion",
            residual="a deletion no step called for",
            expected=False,
            plan=base,
            new_content=correct_without_guard(),
            note=UNCHANGED,
        ),
        Probe(
            name="4-under-implementation",
            residual="a step with no hunk",
            expected=False,
            plan=under_implementation,
            new_content=correct(),
            note=(
                "appended one step to the fetched plan (update the module "
                "docstring); the diff omits it"
            ),
        ),
        Probe(
            name="5-constraint-violation",
            residual="a stated constraint broken",
            expected=False,
            plan=with_table_constraint,
            new_content=shortcut(),
            note=(
                "appended one constraint to the fetched plan (IRREGULAR must not "
                "gain entries); the diff is probe 1's, unchanged"
            ),
        ),
        Probe(
            name="6-faithful",
            residual="control: the clean correct fix",
            expected=True,
            plan=base,
            new_content=correct(),
            note=UNCHANGED,
        ),
        Probe(
            name="7-faithful-other-style",
            residual="control: same rule, same place, different style",
            expected=True,
            plan=base,
            new_content=correct_by_regex(),
            note=UNCHANGED,
        ),
        Probe(
            name="8-faithful-mediocre-plan",
            residual="control: a plan worth disagreeing with, faithfully implemented",
            expected=True,
            plan=MEDIOCRE_PLAN,
            new_content=correct_in_pluralize(),
            note=(
                "hand-written plan, not the fetched one: this control needs a plan "
                "a reviewer might want to argue with"
            ),
        ),
    ]


def probe_diff(probe: Probe) -> str:
    """The same rendering the loop hands the Reviewer, from the same function."""
    return render_diff(FIXTURE, [FileEdit(path=TARGET, new_content=probe.new_content)])


def probe_state(probe: Probe, task_id: str) -> TaskState:
    """A state carrying `plan` and `diff`, so `Reviewer.run` is the entry point.

    Calling `run` rather than `_run` is the point: the contract assertion in
    `Agent.run` and the `agent_produced` event are part of what is being
    measured. The other Group A fields are the fixture's real ones -- the
    Reviewer never reads them, but a state with invented ones would be a
    different object from the one the loop builds.
    """
    task = json.loads((FIXTURE / "task.json").read_text(encoding="utf-8"))
    return TaskState(
        task_id=task_id,
        repo_path=str(FIXTURE),
        task_description=task["task_description"],
        failure_input=task["failure_input"],
        plan=probe.plan,
        diff=probe_diff(probe),
    )


# -- the two modes -------------------------------------------------------------


def fetch_plan(client: LLMClient, log: EventLog) -> Plan:
    """One real Planner call against fixture 3. Cached, so it is spent once."""
    task = json.loads((FIXTURE / "task.json").read_text(encoding="utf-8"))
    state = TaskState(
        task_id=log.task_id,
        repo_path=str(FIXTURE),
        task_description=task["task_description"],
        failure_input=task["failure_input"],
    )
    plan = Planner(log, client).run(state).plan
    assert plan is not None
    PLAN_CACHE.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return plan


def load_plan() -> Plan:
    if not PLAN_CACHE.exists():
        raise SystemExit(f"no {PLAN_CACHE}; run `python probe_reviewer.py --fetch-plan` first")
    return Plan.model_validate_json(PLAN_CACHE.read_text(encoding="utf-8"))


def show(probes: list[Probe]) -> None:
    """Everything a probe run would send, with no call made."""
    for probe in probes:
        print(f"\n{RULE}\n{probe.name} -- expect {'APPROVE' if probe.expected else 'REJECT'}")
        print(f"residual: {probe.residual}")
        print(f"plan:     {probe.note}\n{RULE}")
        print(render_plan(probe.plan))
        print(f"\n<diff>\n{probe_diff(probe)}</diff>")


def run(probes: list[Probe], client: LLMClient, log: EventLog) -> int:
    """One Reviewer call per probe. Returns the number that did not match."""
    results = []

    for probe in probes:
        log.append(
            attempt=0,
            agent="probe",
            event="probe_started",
            payload={"probe": probe.name, "residual": probe.residual, "expected": probe.expected},
        )
        state = probe_state(probe, log.task_id)
        verdict = Reviewer(log, client).run(state).review
        assert verdict is not None
        results.append((probe, verdict))
        print(f"  {probe.name}: {'approved' if verdict.approved else 'rejected'}")

    print(f"\n{RULE}\nRESULTS\n{RULE}")
    header = f"{'probe':<28} {'expected':<9} {'actual':<9} {'match':<6} violated_constraints"
    print(header)
    print("-" * len(header))

    mismatches = 0
    for probe, verdict in results:
        expected = "approve" if probe.expected else "reject"
        actual = "approve" if verdict.approved else "reject"
        matched = probe.expected == verdict.approved
        mismatches += not matched
        quoted = "; ".join(verdict.violated_constraints) or "-"
        print(f"{probe.name:<28} {expected:<9} {actual:<9} {'yes' if matched else 'NO':<6} {quoted}")

    print(f"\n{RULE}\nREASONS, VERBATIM\n{RULE}")
    for probe, verdict in results:
        print(f"\n{probe.name} [{probe.residual}]")
        print(f"  plan: {probe.note}")
        print(f"  verdict: {'approved' if verdict.approved else 'rejected'}")
        print(indent(verdict.reason, "  "))

    print(f"\n{len(results) - mismatches}/{len(results)} matched. Log: {log.path}")
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the Reviewer against hand-written pairs.")
    parser.add_argument(
        "--fetch-plan", action="store_true", help="one Planner call; caches the plan"
    )
    parser.add_argument("--dry-run", action="store_true", help="render every pair; make no calls")
    args = parser.parse_args(argv)

    if args.dry_run:
        show(build_probes(load_plan() if PLAN_CACHE.exists() else MEDIOCRE_PLAN))
        return 0

    task_id = f"probe_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log = EventLog(task_id)

    try:
        client = LLMClient()
        if args.fetch_plan:
            plan = fetch_plan(client, log)
            print(f"Plan cached in {PLAN_CACHE}:\n")
            print(render_plan(plan))
            return 0
        return 1 if run(build_probes(load_plan()), client, log) else 0
    except LLMError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
