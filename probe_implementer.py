"""A measurement instrument for the Implementer's retry path. Not part of the harness.

`python probe_implementer.py --dry-run` costs nothing; `python probe_implementer.py`
costs one call.

**What this is for.** CLAUDE.md has said since 3A that the retry against a real
model is the one thing still unexercised: no model has ever received a
`TesterFailure` in its prompt, so `render_evidence` has never produced a byte a
model read, and `RESET_NOTICE` -- the sentence that stops a model patching a
baseline that never held its last fix -- is an argued claim. This measures it.

**Why a probe rather than a full run, which is the same reasoning as
`probe_reviewer.py`.** Reaching the retry path through the loop means passing
through the Planner first, and `fixture_repo_2` is what that costs: the trap was
proven mechanically, and the Planner fenced it off before the Implementer ever
saw it, so the run went green in three calls and measured nothing about the
retry. A full run also conflates the two agents -- a green attempt 2 could be the
Implementer reading evidence or the Planner having written a plan good enough
that evidence was never needed. This isolates the retry mechanism: the plan is
fixed, the failure is hand-constructed, and the only variable is what the
Implementer does with the evidence.

**The plan is a real Planner artifact and cost nothing.** `probe_plan_fixture_1.json`
is lifted verbatim from `logs/fixture_repo_1_20260823T064826Z.jsonl`, the last
real fixture-1 run. An invented plan would let the probe measure the Implementer
against a rubric no Planner would write; re-fetching one would spend a call on a
plan already saved.

**What this probe cannot settle, stated here because the output repeats it.**
The cached plan names the fix exactly -- "change `>` to `>=`". So an Implementer
that ignored the evidence entirely and re-derived the fix from the plan lands on
the same character as one that read every line of the traceback. Assertion 3
checks that the change addresses the boundaries the evidence named, and it is
worth checking, but on this plan a plan-follower satisfies it too. Separating the
two needs the `--no-evidence` control arm, which is a second call and is not run
by default. See "What the probe measures and what it cannot" in CLAUDE.md.

**Same category as `probe_reviewer.py`**: an instrument at the repo root, outside
`tests/`, so the harness suite keeps making zero API calls and needing no key.
Nothing in `harness/` imports it and `cli.py` does not know it exists. Unlike the
Reviewer probe it does write to disk -- it drives the real `prepare_run_dir`,
`apply_edits` and `reset_run_dir` against a real `runs/` directory, because the
thing under test is a retry and a retry is defined by the reset.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

from harness.agents.implementer import Implementer, render_evidence, render_user_message
from harness.agents.tester import Tester, tester_failure_from
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import render_diff
from harness.state import FileEdit, Plan, TaskState, TesterFailure
from harness.workspace import apply_edits, prepare_run_dir, reset_run_dir

FIXTURE = Path("tasks/fixture_repo_1")
TARGET = "pricing/discounts.py"
PLAN_CACHE = Path("probe_plan_fixture_1.json")
RULE = "=" * 78

# The hand-constructed failing attempt: the fixture's own `discounts.py` with one
# comparison replaced. Derived by named replacement rather than written out, so
# it cannot drift from the file it is a variant of.
_ORIGINAL = "        if quantity > tier.min_quantity:\n"
_WRONG = "        if quantity >= tier.min_quantity - 1:\n"

# What assertion 3 looks for: the failing variant's mechanism, which a correct
# fix must not retain.
WRONG_MECHANISM = "min_quantity - 1"


def baseline() -> str:
    return (FIXTURE / TARGET).read_text(encoding="utf-8")


def failing_content() -> str:
    """`>` overcorrected to `>= min_quantity - 1`: inclusive, and off by one.

    Chosen over a strawman for three properties, all verified before any call was
    spent:

    - **It is one character from correct.** The plan asked for an inclusive
      comparison and this is an inclusive comparison, so it is the shape of
      mistake a model makes when it is reasoning about the right thing rather
      than a mistake it makes when it has understood nothing.
    - **It fixes the bug it was asked to fix.** Every test in `test_orders.py`
      passes under it, including `test_an_order_at_the_bulk_threshold_is_discounted`,
      the fixture's one red test at baseline. A variant that left the original
      failure standing would hand the Implementer evidence it had already seen.
    - **It breaks two different boundaries.** `tier_for(9)` returns bulk and
      `tier_for(49)` returns wholesale, so the evidence names quantities and tier
      labels that appear nowhere in the plan and nowhere in the baseline files.
      That is the part of the prompt that is genuinely new information.
    """
    text = baseline()
    if _ORIGINAL not in text:
        raise SystemExit(f"probe anchor not found in {TARGET}; the fixture has changed")
    return text.replace(_ORIGINAL, _WRONG, 1)


def failing_edit() -> FileEdit:
    return FileEdit(path=TARGET, new_content=failing_content())


def load_plan() -> Plan:
    return Plan.model_validate_json(PLAN_CACHE.read_text(encoding="utf-8"))


def build_failure(repo: str, log: EventLog) -> tuple[TesterFailure, str]:
    """Apply the failing attempt, run the real Tester, package real evidence.

    Every step here is the loop's own: `apply_edits` writes it, `Tester` runs a
    real pytest subprocess against it, and `tester_failure_from` packages the
    result exactly as `loop.py` does. Nothing about the failure is written by
    hand -- the nodeids and the traceback are pytest's.
    """
    before_diff = render_diff(repo, [failing_edit()])
    apply_edits(repo, [failing_edit()])

    result = Tester(log).run(_state(repo, log.task_id)).test_result
    assert result is not None
    if result.passed:
        raise SystemExit("the failing attempt passed the suite; the probe is broken")

    return tester_failure_from(result), before_diff


def _state(repo: str, task_id: str, **extra: object) -> TaskState:
    """A state built from the fixture's real task file, not invented values."""
    task = json.loads((FIXTURE / "task.json").read_text(encoding="utf-8"))
    return TaskState(
        task_id=task_id,
        repo_path=repo,
        task_description=task["task_description"],
        failure_input=task["failure_input"],
        **extra,
    )


def check(failure: TesterFailure, edits: list[FileEdit], passed: bool, failed_now: list[str]):
    """The three assertions, plus what each does and does not prove.

    Assertion 3 is the one the user asked for and the one that needs its caveat
    carried with it: a passing diff alone does not show the Implementer read the
    evidence, because it could have re-derived the fix from a plan that names it.
    Each row therefore carries whether a plan-follower ignoring the evidence
    entirely would also satisfy it.
    """
    content = next((edit.new_content for edit in edits if edit.path.endswith("discounts.py")), "")
    named = set(failure.failed_tests)

    return [
        (
            "1. edits differ from the failing attempt",
            content.strip() != failing_content().strip(),
            "no -- a repeat would have halted on livelock",
        ),
        (
            "2. the real Tester goes green",
            passed,
            "yes -- the plan names the fix",
        ),
        (
            "3a. every nodeid the evidence named now passes",
            bool(named) and not (named & set(failed_now)),
            "yes -- implied by a green suite",
        ),
        (
            "3b. the failing attempt's mechanism is gone",
            WRONG_MECHANISM not in content,
            "yes -- it was never on the baseline it was shown",
        ),
        (
            "3c. the change is the comparison in tier_for",
            ">= tier.min_quantity" in content and "def discount_for" in content,
            "yes -- the plan names the line",
        ),
    ]


def run(*, spend: bool, use_evidence: bool = True) -> int:
    plan = load_plan()
    task_id = f"probe_impl_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log = EventLog(task_id)
    repo = prepare_run_dir(task_id, FIXTURE)

    failure, before_diff = build_failure(repo, log)

    # The reset is the whole point of the probe: the Implementer is about to be
    # shown baseline files that never held the change the evidence describes.
    reset_run_dir(task_id, FIXTURE)

    print(f"{RULE}\nHAND-CONSTRUCTED FAILING ATTEMPT (diff against baseline)\n{RULE}")
    print(before_diff)
    print(f"{RULE}\nREAL EVIDENCE, AS RENDERED INTO THE PROMPT\n{RULE}")
    print(render_evidence(failure))

    if not spend:
        print(f"\n{RULE}\nFULL USER MESSAGE THAT WOULD BE SENT\n{RULE}")
        print(render_user_message(repo_path=repo, plan=plan, evidence=failure))
        print(f"\nDry run: no call made. Run dir left at {repo}")
        return 0

    evidence = failure if use_evidence else None
    state = _state(repo, task_id, plan=plan, evidence=evidence)
    edits = Implementer(log, LLMClient()).run(state).edits
    assert edits is not None

    after_diff = render_diff(repo, edits)
    apply_edits(repo, edits)
    result = Tester(log).run(_state(repo, task_id)).test_result
    assert result is not None

    print(f"{RULE}\nWHAT THE IMPLEMENTER PRODUCED (diff against baseline)\n{RULE}")
    print(after_diff)

    print(f"{RULE}\nASSERTIONS\n{RULE}")
    rows = check(failure, edits, result.passed, result.failed_tests)
    width = max(len(name) for name, _, _ in rows)
    print(f"{'check':<{width}}  {'result':<7}  would a plan-follower also pass it?")
    print("-" * (width + 45))
    for name, ok, confound in rows:
        print(f"{name:<{width}}  {'PASS' if ok else 'FAIL':<7}  {confound}")

    failures = [name for name, ok, _ in rows if not ok]
    print(f"\n{len(rows) - len(failures)}/{len(rows)} passed."
          f"{' Failed: ' + ', '.join(failures) if failures else ''}")
    print(f"Evidence sent: {'yes' if use_evidence else 'NO (control arm)'}. Log: {log.path}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the Implementer's retry path.")
    parser.add_argument("--dry-run", action="store_true", help="render everything; make no call")
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="control arm: same plan and baseline, evidence withheld. A second call.",
    )
    args = parser.parse_args(argv)

    try:
        return run(spend=not args.dry_run, use_evidence=not args.no_evidence)
    except LLMError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
