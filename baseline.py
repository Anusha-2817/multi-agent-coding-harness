"""The single-agent baseline for the A/B/C benchmark. Not part of the harness.

`python baseline.py --repo <path>` runs one model, with no roles, against the
same target the harness would be pointed at. There is no Planner, no Reviewer,
no scope check and no livelock check: one prompt carrying the failing pytest
output and the repository, a full-file answer, the real Tester, and a retry with
the failure as evidence, up to the harness's own attempt cap.

**Everything except the agent structure is the harness's own**, so the arms
differ in exactly one thing. The model, thinking level and token ceiling are the
Implementer's (`LLMClient()`, `implementer.THINKING_LEVEL`,
`implementer.MAX_TOKENS`); the repository view is the Planner's `render_repo`;
the failure text comes from `cli.discover_failure`; reset, `.git` exclusion,
diff rendering, apply, the Tester, evidence rendering and `MAX_ATTEMPTS` are
imported, not copied. The log uses the loop's event names, so `cli.read_events`
and `cli.attempt_ledger` read it unchanged, and every call logs `llm_response`
with the same `usage` the Implementer logs, so cost sums the same way per arm.

**The approval gate is kept as a seam and auto-approved.** `run_baseline` takes
the same `approve(diff) -> bool` callable `run_task` does and calls it at the
same point, between render and apply; `main` passes one that always says yes.
The shipped CLI has no such path, and must not grow one -- this is an instrument,
like `probe_implementer.py`, and like it this is a deliberate second caller of
`apply_edits`. Invariant 1 governs the harness; this is the arm without it.

**Test edits are logged, not refused.** Nothing stops the model editing a test
file, and refusing would be a role this arm does not have. `edits_applied`
carries `test_paths` -- the edited paths `planner._is_test_file` would hide --
so the analysis can flag a green that was bought by touching the tests. Scoring
against held-out tests is what keeps such a run from counting.

**Two failure semantics match the harness exactly.** A suite that will not
collect halts with `escalated_broken_suite` rather than retrying. A truncated
response logs its `llm_response` (so its tokens are counted) and re-raises,
leaving no `run_finished` -- which is how an Implementer truncation ends a
harness run too.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli import DEFAULT_DESCRIPTION, discover_failure, repo_dir_name, resolve_repo
from harness.agents.implementer import (
    MAX_TOKENS,
    THINKING_LEVEL,
    ImplementerResponse,
    render_evidence,
)
from harness.agents.planner import _is_test_file, render_repo
from harness.agents.tester import PYTEST_TESTS_FAILED, Tester, tester_failure_from
from harness.events import EventLog
from harness.llm import LLMClient, LLMError, LLMTruncatedError
from harness.loop import MAX_ATTEMPTS, render_diff
from harness.state import Evidence, Status, TaskState, make_task_id
from harness.workspace import (
    RUNS_ROOT,
    apply_edits,
    normalize_path,
    prepare_run_dir,
    reset_run_dir,
)

AGENT = "baseline"

SYSTEM_PROMPT = """\
You are fixing a bug in a small Python repository. One or more of its tests are
failing. Change the code so that the failing tests pass and every other test
still passes.

You are given the failing pytest output and the repository's source files. Test
files are listed by path only; the failing assertions and tracebacks are in the
pytest output.

For every file you change, return its complete new contents: the entire file,
from its first line to its last, with your change in place. Not a diff, and never
an elision such as "# ... rest unchanged ...". Use the repo-relative path with
forward slashes, as shown. Leave out any file you do not change.

Copy everything you are not changing exactly as it is.\
"""


def render_user_message(
    *,
    repo_path: str,
    task_description: str,
    failure_input: str,
    evidence: Evidence | None,
) -> str:
    """Task, repository, failure, then evidence.

    Stable prefix first and the one volatile section last, for the same caching
    reason the Implementer's prompt is ordered that way.
    """
    sections = [
        f"<task>\n{task_description}\n</task>",
        f"<repository>\n{render_repo(repo_path)}\n</repository>",
        f"<failing_test_output>\n{failure_input}\n</failing_test_output>",
    ]
    if evidence is not None:
        sections.append(render_evidence(evidence))
    return "\n\n".join(sections)


def run_baseline(
    state: TaskState,
    *,
    fixture_path: str | Path,
    client: Any,
    event_log: EventLog,
    approve: Callable[[str], bool],
    runs_root: str | Path = RUNS_ROOT,
) -> TaskState:
    """Run the baseline to a terminal status and return the final state."""

    def log(event: str, agent: str = "harness", **payload: Any) -> None:
        event_log.append(attempt=state.attempt_count, agent=agent, event=event, payload=payload)

    def halt(status: Status, reason: str) -> TaskState:
        nonlocal state
        state = _advance(state, status=status)
        log("run_finished", status=status, attempt_count=state.attempt_count, reason=reason)
        return state

    log(
        "run_started",
        arm=AGENT,
        task_id=state.task_id,
        fixture_path=str(fixture_path),
        repo_path=state.repo_path,
        max_attempts=MAX_ATTEMPTS,
    )

    while True:
        if state.attempt_count >= MAX_ATTEMPTS:
            return halt(
                Status.ESCALATED_RETRY_LIMIT,
                f"{MAX_ATTEMPTS} attempts produced no passing diff.",
            )

        state = _advance(
            state, attempt_count=state.attempt_count + 1, edits=None, diff=None, test_result=None
        )
        reset_run_dir(state.task_id, fixture_path, runs_root)
        log("baseline_reset", repo_path=state.repo_path)

        user = render_user_message(
            repo_path=state.repo_path,
            task_description=state.task_description,
            failure_input=state.failure_input,
            evidence=state.evidence,
        )
        log(
            "llm_request",
            agent=AGENT,
            system_chars=len(SYSTEM_PROMPT),
            user_chars=len(user),
            evidence_kind=state.evidence.kind if state.evidence is not None else None,
        )
        try:
            result = client.complete_structured(
                system=SYSTEM_PROMPT,
                user=user,
                schema=ImplementerResponse,
                max_tokens=MAX_TOKENS,
                thinking_level=THINKING_LEVEL,
            )
        except LLMTruncatedError as exc:
            # Same as `Agent.run`: record what the call cost, then let it end the run.
            if exc.call_payload is not None:
                log("llm_response", agent=AGENT, **exc.call_payload)
            raise
        log("llm_response", agent=AGENT, **result.log_payload())

        edits = result.value.edits
        diff = render_diff(state.repo_path, edits)
        state = _advance(state, edits=edits, diff=diff)
        log("diff_rendered", diff=diff)

        if not approve(diff):
            log("approval_denied")
            return halt(Status.ABORTED_BY_HUMAN, "the diff was rejected at the approval gate.")
        log("approval_granted")

        written = apply_edits(state.repo_path, edits)
        log(
            "edits_applied",
            paths=written,
            test_paths=[path for path in written if _is_test_file(normalize_path(path))],
        )

        state = Tester(event_log).run(state)
        test_result = state.test_result

        if not test_result.summary_parsed:
            log(
                "suite_not_collectable",
                exit_code=test_result.exit_code,
                traceback=test_result.traceback,
            )
            return halt(
                Status.ESCALATED_BROKEN_SUITE,
                "pytest could not collect the suite, so no per-test outcome exists "
                "to aim a retry at.",
            )

        if test_result.passed:
            return halt(Status.SUCCEEDED, "the suite is green.")

        state = _advance(state, evidence=tester_failure_from(test_result))
        log("test_failed", failed_tests=test_result.failed_tests, exit_code=test_result.exit_code)


def _advance(state: TaskState, **fields: Any) -> TaskState:
    """A new, revalidated state with `fields` replaced -- the loop's own rule."""
    return TaskState.model_validate(state.model_dump() | fields)


def _always_approve(diff: str) -> bool:
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single-agent baseline for the benchmark.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    args = parser.parse_args(argv)

    repo_dir = resolve_repo(args.repo)
    try:
        client = LLMClient()
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc

    task_id = f"{AGENT}_{make_task_id(repo_dir_name(repo_dir))}"
    event_log = EventLog(task_id)

    discovered = discover_failure(repo_dir, event_log)
    if discovered.exit_code != PYTEST_TESTS_FAILED:
        raise SystemExit(f"pytest exited {discovered.exit_code} in {repo_dir}; no failing test to fix.")

    state = TaskState(
        task_id=task_id,
        repo_path=prepare_run_dir(task_id, repo_dir),
        task_description=args.description,
        failure_input=discovered.stdout,
    )
    final = run_baseline(
        state,
        fixture_path=repo_dir,
        client=client,
        event_log=event_log,
        approve=_always_approve,
    )
    print(f"{final.status} after {final.attempt_count} attempt(s). Log: {event_log.path}")
    return 0 if final.status is Status.SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
