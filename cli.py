"""The entrypoint: `python cli.py --task tasks/fixture_repo_1/task.json`.

Everything the control loop needs is built here and nowhere else -- reading the
task file, generating the `task_id`, preparing the run directory, constructing
the initial `TaskState`, choosing the four agents, and supplying a human at the
approval gate. `run_task` was written to be driven by exactly this and by nothing
else.

**What this wires, at 3A.** A real Planner and a real Implementer, both backed by
the LLM client. The Tester is real and always has been. The Reviewer is still the
scripted stub, approving every diff, because the real one lands in 3B -- so every
diff here is gated by the *human*, not by a model's verdict. That is worth being
plain about: invariant 1 needs a Review verdict and a human approval, and until
3B one of those two is a stand-in.

**No `--stub` flag yet.** CLAUDE.md lists one and leaves the shape open. It has
nothing coherent to switch at 3A: the Reviewer is stubbed either way, and a
stubbed Planner and Implementer from the CLI would need scripts that only a test
can supply. It arrives with the real Reviewer in 3B, when `--stub` finally means
something -- "all three LLM-backed agents, or none".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.agents.implementer import Implementer
from harness.agents.planner import Planner
from harness.agents.stubs import StubReviewer
from harness.agents.tester import Tester
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import MAX_ATTEMPTS, run_task
from harness.state import ReviewVerdict, Status, TaskState, make_task_id
from harness.workspace import prepare_run_dir

RULE = "=" * 78

# What the stub Reviewer says on every attempt, until 3B replaces it with a model
# that actually reads the diff. One entry per possible attempt: `StubReviewer`
# raises `ScriptExhausted` when its script runs out, so a script shorter than
# `MAX_ATTEMPTS` would turn a legitimate fifth attempt into a crash.
_PLACEHOLDER_VERDICT = ReviewVerdict(
    approved=True,
    reason="placeholder verdict: the real Reviewer arrives in phase 3B",
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


def terminal_approval(diff: str) -> bool:
    """The human at the gate. Prints the diff, reads y/n, returns the answer.

    Receives the diff and nothing else, per the ownership table. CLAUDE.md notes
    that `cli.py` could show the plan and the verdict alongside -- but the plan
    does not exist when this callback is built, and widening to
    `approve(diff, review)` is a change to `run_task`'s signature that has no
    reason to happen before the Reviewer is real.

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
    args = parser.parse_args(argv)

    task_file: Path = args.task
    fixture_path = task_file.parent
    task = read_task_file(task_file)

    # Built before anything is copied, so a missing API key costs nothing. The
    # alternative is discovering it after a copytree and a Planner call.
    try:
        client = LLMClient()
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc

    task_id = make_task_id(fixture_path.name)
    repo_path = prepare_run_dir(task_id, fixture_path)
    event_log = EventLog(task_id)

    state = TaskState(
        task_id=task_id,
        repo_path=repo_path,
        task_description=task["task_description"],
        failure_input=task["failure_input"],
    )

    print(f"{RULE}\ntask     {task_file}\ntask id  {task_id}\n{RULE}")

    final = run_task(
        state,
        fixture_path=fixture_path,
        planner=Planner(event_log, client),
        implementer=Implementer(event_log, client),
        reviewer=StubReviewer(event_log, [_PLACEHOLDER_VERDICT] * MAX_ATTEMPTS),
        tester=Tester(event_log),
        event_log=event_log,
        approve=terminal_approval,
    )

    report(final)
    return 0 if final.status is Status.SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
