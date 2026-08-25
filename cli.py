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
from textwrap import indent

from harness.agents.base import Agent
from harness.agents.implementer import Implementer
from harness.agents.planner import Planner
from harness.agents.reviewer import Reviewer
from harness.agents.stubs import StubImplementer, StubPlanner, StubReviewer
from harness.agents.tester import Tester
from harness.events import EventLog
from harness.llm import LLMClient, LLMError
from harness.loop import MAX_ATTEMPTS, run_task
from harness.state import (
    Evidence,
    FileEdit,
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    Status,
    TaskState,
    make_task_id,
)
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


# -- the escalation output ---------------------------------------------------
#
# Phase 4. A run used to end by printing five lines; four of the five terminal
# statuses are failures with different causes, and `escalated_retry_limit` on its
# own tells a human nothing about which file to open.
#
# **This is the project's first reader of the event log**, and that is a decision
# rather than a drift. `EventLog` stays single-method: append-only constrains
# *mutation* -- no update, no delete, no rewriting history -- and says nothing
# about reading a finished file back. CLAUDE.md's own claim that "a run's log is
# self-contained and replayable on its own" is an invitation to read it. The
# reader lives here, in the presentation layer, rather than as `EventLog.read`,
# so the writer keeps the shape invariant 6 gave it.
#
# What has to come from the log is exactly one thing: **the per-attempt outcome
# history**. `TaskState.evidence` is the single most recent failure, never a
# history, and that is deliberate -- so a five-attempt escalation cannot say what
# the first four attempts died of without reading the events back. Everything
# else comes from the terminal state, which is intact at the halt because `_halt`
# fires before the next attempt's clear at step B.


#: The four branch events, one of which every continuing attempt writes exactly
#: once. A retry-limit halt therefore has exactly MAX_ATTEMPTS of them.
OUTCOME_EVENTS = (
    "no_edits_produced",
    "scope_check_failed",
    "review_rejected",
    "test_failed",
)

#: The only outcome that got past the apply step. Everything else was caught
#: upstream of it, so the run directory is still the pristine baseline.
APPLIED_OUTCOME = "test_failed"


def read_events(log_path: str | Path) -> list[dict]:
    """The run's events, oldest first. Unreadable lines are skipped, not raised.

    The mirror of `EventLog.append`'s own rule: a logging bug degrades one line
    and never kills a run. A *reporting* bug must not turn a completed run into a
    traceback either -- the run is over and its outcome is already on disk, so a
    half-readable log should cost detail, not the report.
    """
    events: list[dict] = []
    try:
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return events

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def _outcome_detail(event: str, payload: dict) -> str:
    """One line naming what this attempt actually died of.

    Short on purpose: the ledger is a shape you read at a glance -- five
    `test_failed` rows and five `scope_check_failed` rows send you to completely
    different files -- and the last failure is printed in full further down.
    """
    if event == "scope_check_failed":
        return ", ".join(payload.get("paths", []))
    if event == "review_rejected":
        return _first_line(payload.get("reason", ""))
    if event == "test_failed":
        failed = payload.get("failed_tests", [])
        if not failed:
            return f"exit code {payload.get('exit_code')}, no test named"
        more = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
        return f"{failed[0]}{more}"
    return ""  # no_edits_produced has no payload, and needs none


def attempt_ledger(events: list[dict]) -> list[tuple[int, str, str]]:
    """`(attempt, event, detail)` for every attempt that failed and routed back.

    The one thing in this module that the terminal state cannot answer. Ordered
    as the log is, which is the order the attempts happened.
    """
    return [
        (event["attempt"], event["event"], _outcome_detail(event["event"], event["payload"]))
        for event in events
        if event.get("event") in OUTCOME_EVENTS
    ]


def rendered_attempt(events: list[dict], diff: str) -> int | None:
    """Which attempt first rendered `diff`, from the `diff_rendered` events.

    **Not** `previous_diffs.index(diff) + 1`, and the difference is not
    theoretical -- it was confirmed against a real three-attempt run before this
    was written. An attempt caught at the no-edits or scope check never reaches
    the render, so it adds no entry to `previous_diffs`; if such an attempt comes
    *before* the diff that is later duplicated, the index sits below the attempt
    number that produced it. A scope failure on attempt 1 followed by identical
    diffs on attempts 2 and 3 gives `index + 1 == 1` for a diff attempt 2 wrote.

    The log does not drift, because `diff_rendered` carries its own `attempt`.
    """
    for event in events:
        if event.get("event") == "diff_rendered" and event["payload"].get("diff") == diff:
            return event["attempt"]
    return None


def _run_finished_reason(events: list[dict]) -> str:
    """The halt reason `_halt` wrote. It lives in the log and nowhere else.

    `run_finished` appears exactly once per run, because every exit goes through
    `_halt`. Read from the end anyway -- the cost is nothing and it does not
    depend on that staying true.
    """
    for event in reversed(events):
        if event.get("event") == "run_finished":
            return event["payload"].get("reason", "")
    return ""


def _heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _block(text: str) -> None:
    """Print an indented block. Uncapped, deliberately.

    A traceback truncated above its assertion line is worse than a long one, and
    this is the output a human reads *instead of* opening the log.

    The `lambda` overrides `indent`'s default of skipping whitespace-only lines,
    and that is load-bearing for a diff: a blank context line is a single space,
    so the default would leave it two columns left of the lines around it and put
    a visible kink in the one artifact the human is being asked to judge.
    """
    print(indent(text.rstrip("\n"), "  ", lambda _: True))


def _print_plan(plan: Plan | None) -> None:
    """The plan, when the plan is the suspect.

    Printed for the retry limit and for livelock, and for neither of the other
    two: a broken suite is the Implementer emitting something unparseable, and a
    human refusal is not about the plan at all.
    """
    if plan is None:
        return
    _heading("Plan under suspicion")
    print(f"  summary  {plan.summary}")
    print("  targets  " + ", ".join(plan.target_files))


def _print_evidence(evidence: Evidence) -> None:
    """The last failure in full, rendered by kind -- not just `evidence.kind`.

    Deliberately not shared with `implementer.render_evidence`. That one is a
    prompt: it speaks to the model in the second person and carries the reset
    notice, both of which would be nonsense here. Same data, different reader.
    """
    _heading(f"Last failure ({evidence.kind})")

    if isinstance(evidence, ReviewerRejection):
        _block(evidence.reason)
        if evidence.violated_constraints:
            print("\n  violated:")
            for item in evidence.violated_constraints:
                print(f"    - {item}")
        return

    for nodeid in evidence.failed_tests:
        print(f"  - {nodeid}")
    if evidence.traceback:
        print()
        _block(evidence.traceback)


def _print_retry_limit(state: TaskState, events: list[dict]) -> None:
    """Five diffs, none green. The ledger is the whole point of this branch."""
    ledger = attempt_ledger(events)

    if ledger:
        _heading("Attempt ledger")
        width = max(len(event) for _, event, _ in ledger)
        for attempt, event, detail in ledger:
            print(f"  {attempt}  {event:<{width}}  {detail}".rstrip())

        # What is actually sitting in the run directory, derived from the last
        # row. The reset happens at the *top* of an attempt, so the final
        # attempt's work is still on disk if it got as far as apply -- and is not
        # there at all if it was caught upstream. Worth stating rather than
        # leaving a human to guess which of the two they are looking at.
        attempt, last, _ = ledger[-1]
        if last == APPLIED_OUTCOME:
            print(
                f"\n  On disk: attempt {attempt}'s edits are applied in the run "
                "directory -- the suite ran against them and failed."
            )
        else:
            print(
                f"\n  On disk: nothing. Attempt {attempt} was caught before the "
                "apply step, so the run directory is the untouched baseline."
            )

    _print_plan(state.plan)
    if state.evidence is not None:
        _print_evidence(state.evidence)


def _print_livelock(state: TaskState, events: list[dict]) -> None:
    """Two attempts, one diff. The diff is the entire evidence."""
    first = rendered_attempt(events, state.diff or "")
    if first is not None:
        print(
            f"\nAttempt {state.attempt_count} rendered a diff byte-identical to "
            f"attempt {first}'s."
        )

    _print_plan(state.plan)

    if state.diff:
        _heading("The repeated diff")
        _block(state.diff)

    print(
        "\n  v1 does not replan, so the loop cannot route this back to the "
        "Planner. The plan above is the thing to change."
    )


def _print_broken_suite(state: TaskState) -> None:
    """pytest could not collect. No evidence is read here, and that is checked.

    The loop writes no `evidence` on this path -- `test_no_evidence_is_written`
    asserts it -- so a reporter that printed `state.evidence` unconditionally
    would show an *earlier* attempt's failure as though it caused this halt.
    """
    result = state.test_result
    if result is not None:
        _heading("What pytest said")
        print(f"  exit code  {result.exit_code}")
        if result.traceback:
            print()
            _block(result.traceback)

    if state.diff:
        _heading("The diff that broke it")
        _block(state.diff)

    print(
        f"\n  On disk: these edits are applied in {state.repo_path}, which is "
        "where the unparseable file can be opened."
    )


def _print_aborted(state: TaskState) -> None:
    """A human said no. Nothing reached disk.

    `state.review` is printed here, and it closes an open question rather than
    reopening one: CLAUDE.md notes that an approving verdict's `reason` otherwise
    reaches the event log and nothing else. Showing it to the person who just
    refused the diff gives them the model's case for it -- which is what widening
    the gate to `approve(diff, review)` would have bought, without widening
    anything. The gate still takes the diff alone; the report reads the verdict
    afterwards, off a state field that is still populated because `_halt` fires
    before the next attempt's clear.
    """
    if state.review is not None:
        _heading("What the Reviewer had said about it")
        _block(state.review.reason)

    if state.diff:
        _heading("The diff you refused")
        _block(state.diff)

    print(
        f"\n  On disk: nothing. The apply step never ran, so {state.repo_path} is "
        "the untouched baseline."
    )


def _print_succeeded(state: TaskState) -> None:
    """Green. The only interesting extra fact is whether it took a retry.

    `evidence` surviving into a `succeeded` state is not a leak -- see CLAUDE.md,
    "evidence survives; that is the point". Every attempt that routes back writes
    evidence before it does, so the surviving one is always the immediately
    preceding attempt's, which is why the attempt number can be named rather than
    looked up.
    """
    if state.evidence is not None:
        print(
            f"\nRecovered from a {state.evidence.kind} on attempt "
            f"{state.attempt_count - 1}."
        )


def report(state: TaskState, log_path: str | Path) -> None:
    """What a finished run says on the way out.

    Takes the log path rather than deriving `logs/<task_id>.jsonl`, so a caller
    that put its log somewhere else -- every test does -- reads the log it wrote.

    The header is common to all six statuses; below it, each terminal status gets
    the one thing a human needs next. The exit code does not vary: every
    non-success is 1, including `aborted_by_human`, which is the gate working
    rather than a distinct kind of outcome worth encoding in a shell.
    """
    events = read_events(log_path)

    print(f"\n{RULE}")
    print(f"status        {state.status.value}")
    print(f"attempts      {state.attempt_count} of {MAX_ATTEMPTS}")
    print(f"run directory {state.repo_path}")
    print(f"event log     {log_path}")
    reason = _run_finished_reason(events)
    if reason:
        print(f"reason        {reason}")
    print(RULE)

    if state.status is Status.ESCALATED_RETRY_LIMIT:
        _print_retry_limit(state, events)
    elif state.status is Status.ESCALATED_LIVELOCK:
        _print_livelock(state, events)
    elif state.status is Status.ESCALATED_BROKEN_SUITE:
        _print_broken_suite(state)
    elif state.status is Status.ABORTED_BY_HUMAN:
        _print_aborted(state)
    elif state.status is Status.SUCCEEDED:
        _print_succeeded(state)

    print(f"\n{RULE}")


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

    report(final, event_log.path)
    return 0 if final.status is Status.SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
