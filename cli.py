"""The entrypoint: `python cli.py --task tasks/fixture_repo_1/task.json [--stub]`.

Everything the control loop needs is built here and nowhere else -- reading the
task file, generating the `task_id`, preparing the run directory, constructing
the initial `TaskState`, choosing the four agents, and supplying a human at the
approval gate. `run_task` was written to be driven by exactly this and by nothing
else.

**What this wires, at 3B.** All three LLM-backed agents are real: Planner,
Implementer, and now Reviewer. The Tester has been real since Phase 1. So
invariant 1 is finally satisfied by two independent gates rather than by one and
a stand-in -- a model's verdict on the diff, and then a human's.

**`--stub` switches all three LLM-backed agents, or none.** That is the whole of
what it means. It does *not* stub the Tester, which is real from Phase 1, and it
does *not* stub the approval gate: `StubApprover` exists so tests can reach the
apply step, and a flag that let the shipped entrypoint skip the human would be
exactly the shape invariant 1 exists to prevent. A stub run still stops at a
terminal and asks.

**A stub run needs no API key, and that is most of the point.** `LLMClient` is
constructed only on the real path, so `--stub` runs when `GEMINI_API_KEY` is
unset or the day's quota is gone. What it is for is checking that the wiring
still works -- `loop.py`, `workspace.py`, the gate, the event log, the real
Tester -- without spending a request on it.

**A stub run cannot succeed, and says so.** The scripted agents decide nothing,
so they cannot fix a bug they were never told about; the stub Implementer appends
a marker comment and the suite stays red. The run walks every step of the loop --
reset, no-edits check, scope check, render, livelock, review, gate, apply, the
real Tester, evidence, retry -- and halts at `escalated_retry_limit`. The one
thing it never demonstrates is `succeeded`, which is the honest price of not
stubbing the Tester. Answering `n` at the first gate ends it sooner, having still
exercised everything up to the apply step.

The alternative was a per-fixture canned script that *does* fix the bug, and it
was rejected: it would put each fixture's fix in a second place, which is the
duplication `tests/fixture_edits.py` reads the fixture to avoid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.agents.base import Agent
from harness.agents.implementer import Implementer
from harness.agents.planner import Planner
from harness.agents.reviewer import Reviewer
from harness.agents.stubs import StubImplementer, StubPlanner, StubReviewer
from harness.agents.tester import Tester
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import MAX_ATTEMPTS, run_task
from harness.state import FileEdit, Plan, ReviewVerdict, Status, TaskState, make_task_id
from harness.workspace import list_repo_files, prepare_run_dir, read_repo_file

RULE = "=" * 78

# What the stub Reviewer says on every attempt. One entry per possible attempt is
# built from this: `StubReviewer` raises `ScriptExhausted` when its script runs
# out, so a script shorter than `MAX_ATTEMPTS` would turn a legitimate fifth
# attempt into a crash.
_STUB_VERDICT = ReviewVerdict(
    reason="stub verdict: no model read this diff",
    approved=True,
    violated_constraints=[],
)


def read_task_file(task_file: Path) -> dict[str, str]:
    """Load `task.json` and check it has exactly the two hand-authored fields.

    Checked here, at the boundary, rather than left to `TaskState` -- a typo'd
    key should name the file it is in, not surface three steps later as a
    contract violation inside an agent.
    """
    try:
        loaded = json.loads(task_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"no task file at {task_file}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{task_file} is not valid JSON: {exc}") from exc

    expected = {"task_description", "failure_input"}
    if not isinstance(loaded, dict) or set(loaded) != expected:
        found = sorted(loaded) if isinstance(loaded, dict) else type(loaded).__name__
        raise SystemExit(
            f"{task_file} must contain exactly {sorted(expected)}; found {found}"
        )

    return loaded


def stub_targets(repo_path: str) -> list[str]:
    """The files a stub run is willing to poke: non-test Python source, sorted.

    Deliberately a cruder rule than `planner._is_test_file`, and not shared with
    it, because it needs less. The Planner's version decides what a model is
    shown and what it may be tempted to edit; this one only has to pick a file
    that exists and that appending a comment to cannot break. Sharing would make
    a real decision answer to a placeholder's needs.
    """
    return [
        path
        for path in list_repo_files(repo_path)
        if path.endswith(".py")
        and not path.startswith("tests/")
        and not path.rsplit("/", 1)[-1].startswith("test_")
    ]


def stub_agents(event_log: EventLog, repo_path: str) -> tuple[Agent, Agent, Agent]:
    """Scripted Planner, Implementer, and Reviewer, built from the fixture itself.

    Fixture-agnostic on purpose: it reads whatever is in the run directory rather
    than carrying knowledge of any particular bug, so it works on fixture repos 2
    and 3 the day they are added.

    The edit lists follow `tests/fixture_edits.failing_edits`: read the real
    baseline, append an attempt marker, leave everything else alone. The marker
    is load-bearing for the same reason it is there -- every attempt starts from
    an identical baseline, so two edits that both merely leave the bug in place
    would render byte-identical diffs and halt the run on the livelock check at
    attempt 2, short of the retry path this is meant to exercise.

    Reading the baseline once at construction is correct because of invariant 4:
    the reset at the top of every attempt puts the same bytes back.
    """
    targets = stub_targets(repo_path)
    if not targets:
        raise SystemExit(
            f"--stub found no non-test Python file in {repo_path} to work with"
        )

    poked = targets[0]
    baseline = read_repo_file(repo_path, poked)

    plan = Plan(
        summary=(
            "Stub plan: no model was consulted. It exists so that a --stub run "
            "has a plan-shaped object to carry through the loop."
        ),
        steps=[f"Append a marker comment to {poked}. This fixes nothing."],
        # Exactly the one file the stub Implementer edits, not every source file.
        # A plan naming everything would make the scope check vacuous, and the
        # point of a stub run is that every step does its real work.
        target_files=[poked],
        constraints=[],
    )

    edit_lists = [
        [FileEdit(path=poked, new_content=f"{baseline}\n# stub attempt {n}\n")]
        for n in range(1, MAX_ATTEMPTS + 1)
    ]

    return (
        # One entry: v1 plans exactly once, and a one-entry script makes that an
        # assertion rather than a hope.
        StubPlanner(event_log, [plan]),
        StubImplementer(event_log, edit_lists),
        StubReviewer(event_log, [_STUB_VERDICT] * MAX_ATTEMPTS),
    )


def terminal_approval(diff: str) -> bool:
    """The human at the gate. Prints the diff, reads y/n, returns the answer.

    Receives the diff and nothing else, per the ownership table. CLAUDE.md notes
    that `cli.py` could show the plan and the verdict alongside -- but the plan
    does not exist when this callback is built, and widening to
    `approve(diff, review)` is a change to `run_task`'s signature that has no
    reason to happen before something asks for it.

    An unreadable stdin -- a pipe, a CI job, `< /dev/null` -- is treated as "no".
    A gate whose failure mode is "approve" is not a gate.
    """
    print(f"\n{RULE}\nProposed diff\n{RULE}")
    print(diff if diff.strip() else "(empty)")
    print(RULE)

    while True:
        try:
            answer = input("Apply this diff? [y/n] ").strip().lower()
        except EOFError:
            print("\nno input available; treating that as a refusal.")
            return False

        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("please answer y or n.")


def report(state: TaskState) -> None:
    """What a finished run says on the way out.

    Deliberately plain. Four of the five terminal statuses are failures with
    different causes, and packaging them properly is Phase 4's job -- anything
    more here would be a second implementation to throw away.
    """
    print(f"\n{RULE}")
    print(f"status        {state.status.value}")
    print(f"attempts      {state.attempt_count} of {MAX_ATTEMPTS}")
    print(f"run directory {state.repo_path}")
    print(f"event log     logs/{state.task_id}.jsonl")

    if state.evidence is not None:
        # Present even on a success, and that is not a leak: it is what the run
        # recovered from. See CLAUDE.md, "evidence survives; that is the point".
        print(f"last failure  {state.evidence.kind}")
    print(RULE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Fix one failing test in a fixture repo, with a human at the gate.",
    )
    parser.add_argument(
        "--task",
        required=True,
        type=Path,
        help="path to a fixture's task.json, e.g. tasks/fixture_repo_1/task.json",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help=(
            "run the loop with scripted stand-ins for the Planner, Implementer, "
            "and Reviewer. No API key needed, no requests spent. The Tester and "
            "the approval gate stay real, and the run cannot succeed."
        ),
    )
    args = parser.parse_args(argv)

    task_file: Path = args.task
    fixture_path = task_file.parent
    task = read_task_file(task_file)

    # Built before anything is copied, so a missing API key costs nothing. The
    # alternative is discovering it after a copytree and a Planner call. Under
    # --stub no agent holds a client, so none is built -- constructing one would
    # buy nothing and would cost the flag its main use, which is running when
    # there is no key or no quota left.
    client = None
    if not args.stub:
        try:
            client = LLMClient()
        except LLMError as exc:
            raise SystemExit(str(exc)) from exc

    task_id = make_task_id(fixture_path.name)
    repo_path = prepare_run_dir(task_id, fixture_path)
    event_log = EventLog(task_id)

    if args.stub:
        # Built from the prepared run directory, which is why this happens here
        # and not up beside the client.
        planner, implementer, reviewer = stub_agents(event_log, repo_path)
    else:
        planner = Planner(event_log, client)
        implementer = Implementer(event_log, client)
        reviewer = Reviewer(event_log, client)

    state = TaskState(
        task_id=task_id,
        repo_path=repo_path,
        task_description=task["task_description"],
        failure_input=task["failure_input"],
    )

    # Which agents ran is already unambiguous in the log -- the stubs write
    # `stub_scripted` and the real agents write `llm_request` -- so this is for
    # the person at the terminal, and `run_started` keeps the payload it had.
    print(
        f"{RULE}\ntask     {task_file}\ntask id  {task_id}\n"
        f"agents   {'stubbed (no API calls)' if args.stub else 'real'}\n{RULE}"
    )

    final = run_task(
        state,
        fixture_path=fixture_path,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
        tester=Tester(event_log),
        event_log=event_log,
        approve=terminal_approval,
    )

    report(final)
    return 0 if final.status is Status.SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
