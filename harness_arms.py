"""Runner for benchmark arms B and C. Not part of the harness.

`python harness_arms.py --arm B --repo <path>` runs the harness without the
Reviewer; `--arm C` runs the full harness. Both drive the real `run_task` with an
always-approve callable, which is the one reason this script exists: `cli.py`
always stops at a human gate and must not grow a flag that skips it.

**B and C differ in exactly one object: the reviewer handed to `run_task`.** C
gets the real `Reviewer`; B gets `PassThroughReviewer`, which approves every
diff without a model call. Everything else -- the client, the Planner and
Implementer and their prompts, the Tester, the loop, the scope and livelock
checks, `MAX_ATTEMPTS`, the reset, the gate -- is built by the same lines for
both. The Implementer's prompt still mentions a reviewer in arm B; that is
deliberate, so B vs C measures whether the Reviewer *runs*, with the prompt
held fixed.

The failure input comes from `cli.discover_failure` and `cli.resolve_repo`, the
same calls `baseline.py` makes, so all three arms are handed the same bytes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from cli import DEFAULT_DESCRIPTION, discover_failure, repo_dir_name, resolve_repo
from harness.agents.base import Agent
from harness.agents.implementer import EDIT_FORMATS, Implementer
from harness.agents.planner import Planner
from harness.agents.reviewer import Reviewer
from harness.agents.tester import PYTEST_TESTS_FAILED, Tester
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import run_task
from harness.state import Plan, ReviewVerdict, Status, TaskState, make_task_id
from harness.workspace import RUNS_ROOT, prepare_run_dir

ARMS = ("B", "C")

BYPASS_REASON = "arm B: no review was performed; every diff is approved."


class PassThroughReviewer(Agent):
    """Arm B's reviewer: approves every diff, calls no model.

    A real `Agent` under the name `reviewer`, so `CONTRACTS["reviewer"]` is
    checked and `Agent.run` writes the same `agent_produced` event C's Reviewer
    does. Where C logs `llm_request`/`llm_response`, this logs `review_bypassed`,
    so the arm is readable from the log alone.
    """

    name = "reviewer"

    def _run(self, *, plan: Plan, diff: str) -> ReviewVerdict:
        self.log(event="review_bypassed", payload={"arm": "B"})
        return ReviewVerdict(reason=BYPASS_REASON, approved=True, violated_constraints=[])


class ArmEventLog(EventLog):
    """An `EventLog` that stamps `arm` into `run_started`, and changes nothing else.

    `run_started` is written inside `run_task`, whose payload is fixed, so this
    is the only way to put the arm there without editing `harness/`. Every other
    event passes through untouched.

    `edit_format` is stamped the same way, and only when it is not `full`: a
    full-mode log keeps the shape every earlier log has, and a missing key reads
    as full, so no old log needs migrating.
    """

    def __init__(
        self, task_id: str, arm: str, log_dir: str | Path = "logs", edit_format: str = "full"
    ) -> None:
        super().__init__(task_id, log_dir)
        self.arm = arm
        self.edit_format = edit_format

    def append(self, *, attempt: int, agent: str, event: str, payload: dict[str, Any]) -> None:
        if event == "run_started":
            stamp = {"arm": self.arm}
            if self.edit_format != "full":
                stamp["edit_format"] = self.edit_format
            payload = {**stamp, **payload}
        super().append(attempt=attempt, agent=agent, event=event, payload=payload)


def always_approve(diff: str) -> bool:
    return True


def run_arm(
    arm: str,
    state: TaskState,
    *,
    fixture_path: str | Path,
    client: Any,
    event_log: EventLog,
    runs_root: str | Path = RUNS_ROOT,
    edit_format: str = "full",
) -> TaskState:
    """Run one arm to a terminal state. The reviewer is the only arm-dependent line.

    `edit_format` reaches the Implementer and nothing else, and is the same for B
    and C, so B vs C still varies only the reviewer.
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")

    reviewer = Reviewer(event_log, client) if arm == "C" else PassThroughReviewer(event_log)

    return run_task(
        state,
        fixture_path=fixture_path,
        planner=Planner(event_log, client),
        implementer=Implementer(event_log, client, edit_format),
        reviewer=reviewer,
        tester=Tester(event_log),
        event_log=event_log,
        approve=always_approve,
        runs_root=runs_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run benchmark arm B or C.")
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    parser.add_argument("--edit-format", choices=EDIT_FORMATS, default="full")
    args = parser.parse_args(argv)

    repo_dir = resolve_repo(args.repo)
    try:
        client = LLMClient()
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc

    task_id = f"arm{args.arm}_{make_task_id(repo_dir_name(repo_dir))}"
    event_log = ArmEventLog(task_id, args.arm, edit_format=args.edit_format)

    discovered = discover_failure(repo_dir, event_log)
    if discovered.exit_code != PYTEST_TESTS_FAILED:
        raise SystemExit(f"pytest exited {discovered.exit_code} in {repo_dir}; no failing test to fix.")

    state = TaskState(
        task_id=task_id,
        repo_path=prepare_run_dir(task_id, repo_dir),
        task_description=args.description,
        failure_input=discovered.stdout,
    )
    final = run_arm(args.arm, state, fixture_path=repo_dir, client=client, event_log=event_log,
                    edit_format=args.edit_format)
    print(f"arm {args.arm}: {final.status} after {final.attempt_count} attempt(s). Log: {event_log.path}")
    return 0 if final.status is Status.SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
