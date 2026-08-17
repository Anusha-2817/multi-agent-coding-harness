"""The control loop: routing, retry with evidence, livelock, escalation.

This is the part of the project that matters. The four agents are role-pure and
know nothing about each other; everything that decides what happens next lives
here, in one function you can read top to bottom.

Three things shape it.

**The loop owns every state transition.** Agents return new states and never
mutate; so does this module. `_advance` is the single place a `TaskState` is
rebuilt, and it revalidates for the same reason `Agent.run` does -- a field set
by a typo'd keyword should fail here, not four steps later.

**Harness steps sit between the agents, not inside them.** The scope check, the
render, the livelock check and the approval gate are all loop code. None of them
is an agent, none of them has a contract, and each is cheap enough to run before
the expensive thing it protects: scope before render, render before review,
review before the human, the human before anything reaches disk.

**The agents arrive as parameters.** That is not the injectable seam the scope
fence talks about -- they are the loop's operands, and `cli.py` is what chooses
stubs or real ones. The loop never learns which it got. The one genuine seam is
`approve`, because a human at a terminal is otherwise untestable.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.agents.base import Agent
from harness.agents.tester import tester_failure_from
from harness.events import EventLog
from harness.state import FileEdit, Plan, ReviewerRejection, Status, TaskState
from harness.workspace import RUNS_ROOT, apply_edits, normalize_path, reset_run_dir

# Five Implementer runs. A module constant rather than a `TaskState` field: it is
# a property of the harness, not of the task, and putting it in state would let a
# task file raise its own ceiling.
MAX_ATTEMPTS = 5

#: What the run is told when two attempts render the same diff. The Implementer
#: is doing as it was told; being told the same thing twice is the plan's fault.
LIVELOCK_REASON = (
    "two attempts produced a byte-identical diff. The plan is the likely fault, "
    "not the implementation."
)


def render_diff(repo_path: str | Path, edits: list[FileEdit]) -> str:
    """Render `edits` against the files currently in `repo_path`.

    Valid only because the run directory was reset immediately before the
    Implementer ran, so what is on disk here *is* the baseline. That is the whole
    reason two identical fixes render identical text.

    Three details exist to keep that byte-equality honest, because the livelock
    check compares this output literally:

    - **No timestamps.** `unified_diff`'s date arguments are left empty. Filling
      them would make every diff unique and livelock unreachable.
    - **Stable labels.** `a/<path>`, `b/<path>`, built from the normalised edit
      path -- never an absolute path, which would embed `task_id` and with it a
      timestamp.
    - **Deterministic order.** Edits are sorted by path, so a multi-file change
      cannot render in two orders and read as progress.

    Line endings pass through as the Implementer wrote them. A model emitting
    CRLF gets a diff touching every line, which is honest: it really did change
    every line ending. Silently rewriting model output would hide that.
    """
    chunks: list[str] = []

    for edit in sorted(edits, key=lambda e: normalize_path(e.path)):
        relative = normalize_path(edit.path)
        target = Path(repo_path) / relative

        before = (
            target.read_text(encoding="utf-8").splitlines(keepends=True)
            if target.exists()
            else []  # a plan may name a file that does not exist yet
        )
        after = edit.new_content.splitlines(keepends=True)

        text = "".join(
            difflib.unified_diff(before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}")
        )
        # A file whose last line has no trailing newline would otherwise run into
        # the next file's `--- a/...` header. difflib emits no "\ No newline at
        # end of file" marker of its own.
        if text and not text.endswith("\n"):
            text += "\n"
        chunks.append(text)

    return "".join(chunks)


def run_task(
    state: TaskState,
    *,
    fixture_path: str | Path,
    planner: Agent,
    implementer: Agent,
    reviewer: Agent,
    tester: Agent,
    event_log: EventLog,
    approve: Callable[[str], bool],
    runs_root: str | Path = RUNS_ROOT,
) -> TaskState:
    """Run one task to a terminal status and return the final state.

    `fixture_path` is a parameter rather than a `TaskState` field because reset
    needs it and no agent does -- it is harness plumbing, and the ownership table
    has no row for it.

    `approve` is handed the rendered diff and returns whether it may reach disk.
    The diff alone, because the ownership table says the gate reads `diff`.

    The Planner runs once, outside the retry loop: v1 does not replan. Routing
    rejections back to the Planner is a v2 item behind the scope fence, and a
    one-entry `StubPlanner` script turns that into an assertion.
    """
    _log(
        event_log,
        state,
        "run_started",
        task_id=state.task_id,
        fixture_path=str(fixture_path),
        repo_path=state.repo_path,
        max_attempts=MAX_ATTEMPTS,
    )

    # attempt_count is still 0 here, so Planner events are stamped attempt=0.
    state = planner.run(state)

    while True:
        # -- cap check ------------------------------------------------------
        # Before the reset, so the escalating pass does not copy a tree it will
        # not use. Before the Implementer too, which is the load-bearing part: a
        # runaway loop halts here with a status rather than by exhausting a
        # stub's script, so the test asserts an outcome instead of an exception.
        if state.attempt_count >= MAX_ATTEMPTS:
            return _halt(
                event_log,
                state,
                Status.ESCALATED_RETRY_LIMIT,
                f"{MAX_ATTEMPTS} attempts produced no passing diff.",
            )

        # -- new attempt ----------------------------------------------------
        # Clear and increment together. The per-attempt fields go back to None so
        # `None` keeps meaning "not yet produced this attempt" -- without this, a
        # run halting at the scope check on attempt 2 would return attempt 1's
        # approving verdict and its passing-shaped test_result. `evidence` is the
        # deliberate exception: carrying the last failure forward is its job.
        #
        # The counter moves before the physical reset so `baseline_reset` is
        # stamped with the attempt it prepares for. The reset is the first act of
        # the new attempt, not the last act of the old one.
        state = _advance(
            state,
            attempt_count=state.attempt_count + 1,
            edits=None,
            diff=None,
            review=None,
            test_result=None,
        )

        # -- reset to baseline ----------------------------------------------
        # Unconditional, at the top of every attempt including the first. One
        # call site means no failure path can forget one, which is what makes
        # invariant 4 structural rather than an agreement between five branches.
        # The cost is one redundant copy on attempt 1, over a directory `cli.py`
        # has just prepared.
        reset_run_dir(state.task_id, fixture_path, runs_root)
        _log(event_log, state, "baseline_reset", repo_path=state.repo_path)

        # -- implement ------------------------------------------------------
        state = implementer.run(state)
        plan: Plan = state.plan  # non-None: the Implementer's contract asserted it
        edits: list[FileEdit] = state.edits  # non-None: producing None is a violation

        # -- no edits -------------------------------------------------------
        # `[]` is not a contract violation -- only `None` is -- so the loop has to
        # judge it. Left implicit it would apply nothing, test red, and read in
        # the log as a failed fix rather than as no fix at all.
        if not edits:
            state = _advance(
                state,
                evidence=ReviewerRejection(
                    reason="the Implementer produced no edits",
                    violated_constraints=[],
                ),
            )
            _log(event_log, state, "no_edits_produced")
            continue

        # -- scope check ----------------------------------------------------
        # First of the harness steps because it is the cheapest: it compares
        # paths and needs no rendered diff. A violation is written as a
        # ReviewerRejection, not a third evidence type -- the corrective action is
        # identical, and the log is where the provenance lives.
        out_of_scope = _out_of_scope(edits, plan)
        if out_of_scope:
            state = _advance(
                state,
                evidence=ReviewerRejection(
                    reason=(
                        f"edited {len(out_of_scope)} file(s) outside the plan's "
                        f"target_files: {', '.join(out_of_scope)}"
                    ),
                    violated_constraints=out_of_scope,
                ),
            )
            _log(
                event_log,
                state,
                "scope_check_failed",
                paths=out_of_scope,
                target_files=plan.target_files,
            )
            continue

        # -- render ---------------------------------------------------------
        diff = render_diff(state.repo_path, edits)
        state = _advance(state, diff=diff)
        _log(event_log, state, "diff_rendered", diff=diff)

        # -- livelock check -------------------------------------------------
        # Before Review, so a duplicate never costs a review. The assertion that
        # proves the ordering is that the Reviewer was never called a second time.
        if diff in state.previous_diffs:
            _log(
                event_log,
                state,
                "livelock_detected",
                diff=diff,
                previous_diffs=len(state.previous_diffs),
            )
            return _halt(event_log, state, Status.ESCALATED_LIVELOCK, LIVELOCK_REASON)

        # Appended only once the check has passed. Appending first would leave the
        # duplicate in the list and put every count off by one.
        state = _advance(state, previous_diffs=[*state.previous_diffs, diff])

        # -- review ---------------------------------------------------------
        state = reviewer.run(state)
        verdict = state.review
        if not verdict.approved:
            state = _advance(
                state,
                evidence=ReviewerRejection(
                    reason=verdict.reason,
                    violated_constraints=verdict.violated_constraints,
                ),
            )
            _log(
                event_log,
                state,
                "review_rejected",
                reason=verdict.reason,
                violated_constraints=verdict.violated_constraints,
            )
            continue

        # -- human approval -------------------------------------------------
        # Per diff, never per task. Attempt 5 gets this gate exactly as attempt 1
        # does, and a rejected diff has still touched nothing.
        if not approve(diff):
            _log(event_log, state, "approval_denied")
            # No evidence, no route back. The human is rejecting for reasons the
            # harness cannot see; asking the Implementer to guess at an objection
            # it was never told is wasted work.
            return _halt(
                event_log,
                state,
                Status.ABORTED_BY_HUMAN,
                "the human rejected the diff at the approval gate.",
            )
        _log(event_log, state, "approval_granted")

        # -- apply ----------------------------------------------------------
        written = apply_edits(state.repo_path, edits)
        _log(event_log, state, "edits_applied", paths=written)

        # -- test -----------------------------------------------------------
        state = tester.run(state)
        result = state.test_result

        # A suite that will not collect is not fixable by another diff: there is
        # no failing test to aim at. Checked before `passed` because it is the
        # stronger statement -- though pytest cannot report both, since exit 0
        # always carries a readable outcome set.
        if not result.summary_parsed:
            _log(
                event_log,
                state,
                "suite_not_collectable",
                exit_code=result.exit_code,
                traceback=result.traceback,
            )
            return _halt(
                event_log,
                state,
                Status.ESCALATED_BROKEN_SUITE,
                "pytest could not collect the suite, so no per-test outcome exists "
                "to aim a retry at.",
            )

        if result.passed:
            return _halt(event_log, state, Status.SUCCEEDED, "the suite is green.")

        # Red, and legible. Package the failure and go round again.
        state = _advance(state, evidence=tester_failure_from(result))
        _log(
            event_log,
            state,
            "test_failed",
            failed_tests=result.failed_tests,
            exit_code=result.exit_code,
        )


# -- harness steps -----------------------------------------------------------


def _out_of_scope(edits: list[FileEdit], plan: Plan) -> list[str]:
    """The edit paths that fall outside `plan.target_files`.

    Both sides are normalised before comparison, so a separator or a leading
    `./` is not mistaken for a scope violation. The paths are returned **as the
    Implementer wrote them**: the evidence should show the model what it actually
    emitted, not a cleaned-up version it will not recognise.
    """
    allowed = {normalize_path(path) for path in plan.target_files}
    return [edit.path for edit in edits if normalize_path(edit.path) not in allowed]


# -- state and logging -------------------------------------------------------


def _advance(state: TaskState, **fields: Any) -> TaskState:
    """Return a new state with `fields` replaced. Never mutates.

    `model_validate` rather than `model_copy(update=...)`, for the reason
    `Agent.run` gives: `model_copy` does not validate, so a wrong type would land
    in state unchecked and surface much later with nothing pointing back.
    """
    return TaskState.model_validate(state.model_dump() | fields)


def _log(event_log: EventLog, state: TaskState, event: str, **payload: Any) -> None:
    """Append a harness event, stamped with the current attempt.

    `agent="harness"` is what separates these from agent events in the log. The
    harness names things the evidence types deliberately do not -- a mechanical
    `scope_check_failed` and a judged `review_rejected` produce identical
    evidence, and only the log can tell you which one happened.
    """
    event_log.append(attempt=state.attempt_count, agent="harness", event=event, payload=payload)


def _halt(event_log: EventLog, state: TaskState, status: Status, reason: str) -> TaskState:
    """Set the terminal status, log it, and return the final state.

    Every exit goes through here, so `run_finished` appears exactly once per run
    and always carries the status and the attempt count. The diagnostic events
    that precede it -- `livelock_detected`, `approval_denied`, `test_failed`,
    `suite_not_collectable` -- carry the payloads this one cannot.
    """
    state = _advance(state, status=status)
    _log(
        event_log,
        state,
        "run_finished",
        status=status,
        attempt_count=state.attempt_count,
        reason=reason,
    )
    return state
