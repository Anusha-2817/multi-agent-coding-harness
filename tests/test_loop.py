"""Tests for harness/loop.py -- the control loop.

Driven by scripted stubs and judged by the **real Tester**, which runs real pytest
against the real fixture. Nothing here fakes a test outcome: "fail twice, then
succeed" is three edit lists, and whether the suite is red is whatever pytest
says. That is what makes these tests about the loop's routing rather than about a
mock's cleverness.

Every scenario costs a `copytree` plus a pytest subprocess per attempt, so this is
the slowest module in the suite. The retry-cap and field-clearing tests cost five
attempts each. The alternative -- stubbing the Tester -- would remove the only
thing in the loop that is not scripted.

Two conventions worth knowing before reading the assertions:

- **The pinned numbers matter.** `attempt_count`, `len(previous_diffs)`, and the
  stubs' call counts are specified in CLAUDE.md, and several of them are off-by-one
  traps. A livelock halt is `attempt_count == 2` with **one** entry in
  `previous_diffs`, because the diff is appended only after the check passes.
- **A stub's `seen` is how ordering gets proved.** "The livelock check runs before
  Review" is not observable from the final state; it is observable from the
  Reviewer having been called once rather than twice.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest
from fixture_edits import (
    BUG,
    DISCOUNTS,
    FIX,
    MONEY,
    broken_edits,
    failing_edits,
    fixing_edits,
    out_of_scope_edits,
)

from harness.agents.stubs import StubApprover, StubImplementer, StubPlanner, StubReviewer
from harness.agents.tester import Tester
from harness.events import EventLog
from harness.loop import MAX_ATTEMPTS, render_diff, run_task
from harness.state import (
    FileEdit,
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    Status,
    TaskState,
    TesterFailure,
)
from harness.workspace import prepare_run_dir

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
TASK_ID = "fixture_repo_1_20260817T120000Z"

FAILING_NODEID = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"

PLAN = Plan(
    summary="Use an inclusive lower bound so a quantity on a tier boundary gets that tier.",
    steps=["Change > to >= in tier_for"],
    target_files=[DISCOUNTS],
    constraints=["Do not change the tier table"],
)
APPROVED = ReviewVerdict(approved=True, reason="minimal and faithful", violated_constraints=[])
REJECTED = ReviewVerdict(
    approved=False,
    reason="the change is broader than the plan calls for",
    violated_constraints=["minimality"],
)


@pytest.fixture
def event_log(tmp_path) -> EventLog:
    return EventLog(TASK_ID, log_dir=tmp_path / "logs")


@pytest.fixture
def runs_root(tmp_path) -> Path:
    return tmp_path / "runs"


def scenario(tmp_path_factory, name: str, **kwargs) -> "Run":
    """A run in its own temp directory, for use from a **class-scoped** fixture.

    Every attempt costs a `copytree` and a pytest subprocess, so a scenario that
    several tests assert against is run once and shared. `tmp_path_factory` rather
    than `tmp_path` because the latter is function-scoped and would drag the
    fixture back down with it.

    Safe to share only because nothing here mutates a `Run`: the tests read the
    final state, the stubs' `seen`, and the log. If a test ever needs to write
    into the run directory, it should take its own function-scoped scenario.
    """
    root = tmp_path_factory.mktemp(name)
    return run_scenario(
        EventLog(TASK_ID, log_dir=root / "logs"),
        root / "runs",
        **kwargs,
    )


@dataclass
class Run:
    """One finished run, plus the stubs that drove it.

    The stubs are the interesting half. `final` says how the run ended; the stubs
    say what the loop did on the way, which is where the routing assertions live.
    """

    final: TaskState
    planner: StubPlanner
    implementer: StubImplementer
    reviewer: StubReviewer
    approver: StubApprover
    log: EventLog

    @property
    def diffs_reviewed(self) -> list[str]:
        return [call["diff"] for call in self.reviewer.seen]

    @property
    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.path.read_text(encoding="utf-8").splitlines()]

    def names(self) -> list[str]:
        return [event["event"] for event in self.events]


def run_scenario(
    event_log: EventLog,
    runs_root: Path,
    *,
    edit_lists: Sequence[list[FileEdit]],
    verdicts: Sequence[ReviewVerdict] | None = None,
    answers: Sequence[bool] | None = None,
    plans: Sequence[Plan] | None = None,
) -> Run:
    """Drive one run to its terminal state.

    `verdicts` and `answers` default to one approving verdict and one `True` per
    edit list -- generous rather than exact, because a `Script` only raises when it
    is *called* too often. An attempt caught at the scope check simply does not
    consume its verdict, so a scenario need not restate the arithmetic of which
    attempts reach which step.
    """
    plans = [PLAN] if plans is None else plans
    verdicts = [APPROVED] * len(edit_lists) if verdicts is None else verdicts
    answers = [True] * len(edit_lists) if answers is None else answers

    planner = StubPlanner(event_log, plans)
    implementer = StubImplementer(event_log, edit_lists)
    reviewer = StubReviewer(event_log, verdicts)
    approver = StubApprover(answers)

    repo_path = prepare_run_dir(TASK_ID, FIXTURE, runs_root)
    state = TaskState(
        task_id=TASK_ID,
        repo_path=repo_path,
        task_description="An order at a tier boundary gets the wrong discount.",
        failure_input="1 failed, 19 passed",
    )

    final = run_task(
        state,
        fixture_path=FIXTURE,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
        tester=Tester(event_log),
        event_log=event_log,
        approve=approver,
        runs_root=runs_root,
    )
    return Run(final, planner, implementer, reviewer, approver, event_log)


def applied(run: Run) -> str:
    """The fixture's buggy file as it now stands in the run directory."""
    return (Path(run.final.repo_path) / DISCOUNTS).read_text(encoding="utf-8")


# -- the happy path ----------------------------------------------------------


class TestHappyPath:
    @pytest.fixture(scope="class")
    @classmethod
    def happy(cls, tmp_path_factory) -> Run:
        return scenario(tmp_path_factory, "happy", edit_lists=[fixing_edits()])

    def test_one_correct_diff_succeeds(self, happy):
        assert happy.final.status is Status.SUCCEEDED
        assert happy.final.attempt_count == 1
        assert len(happy.final.previous_diffs) == 1
        assert happy.final.test_result.passed is True

    def test_a_clean_run_carries_no_evidence(self, happy):
        """Nothing failed, so there is nothing to have recovered from."""
        assert happy.final.evidence is None

    def test_the_fix_reached_disk(self, happy):
        assert FIX in applied(happy)
        assert BUG not in applied(happy)

    def test_each_agent_ran_once(self, happy):
        assert len(happy.planner.seen) == 1
        assert len(happy.implementer.seen) == 1
        assert len(happy.reviewer.seen) == 1
        assert len(happy.approver.seen) == 1


# -- retry on a test failure -------------------------------------------------


class TestRetryOnTestFailure:
    @pytest.fixture(scope="class")
    @classmethod
    def recovered(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "recovered", edit_lists=[failing_edits(1), fixing_edits()]
        )

    def test_a_red_attempt_routes_back_and_the_next_one_succeeds(self, recovered):
        assert recovered.final.status is Status.SUCCEEDED
        assert recovered.final.attempt_count == 2
        assert len(recovered.final.previous_diffs) == 2

    def test_the_failure_is_carried_into_the_second_attempt(self, recovered):
        """The point of the retry path. Seeing a second attempt happen is not the
        assertion -- seeing the failure carried into it is."""
        assert recovered.implementer.seen[0]["evidence"] is None

        carried = recovered.implementer.seen[1]["evidence"]
        assert isinstance(carried, TesterFailure)
        assert carried.kind == "tester_failure"
        assert carried.failed_tests == [FAILING_NODEID]
        assert "AssertionError" in carried.traceback
        assert carried.stdout_tail

    def test_evidence_survives_into_a_succeeded_state(self, recovered):
        """Deliberate, not a leak.

        A run ending `succeeded` with a `TesterFailure` in `evidence` is telling
        the truth: it succeeded on retry, and this is what it recovered from.
        Recovery is the point of the harness, so erasing it from the terminal
        state would lose the most interesting thing about the run.
        """
        assert recovered.final.status is Status.SUCCEEDED
        assert isinstance(recovered.final.evidence, TesterFailure)
        assert recovered.final.evidence.failed_tests == [FAILING_NODEID]

    def test_the_plan_is_carried_into_every_attempt(self, recovered):
        assert [call["plan"] for call in recovered.implementer.seen] == [PLAN, PLAN]


# -- the baseline reset ------------------------------------------------------


class TestBaselineReset:
    @pytest.fixture(scope="class")
    @classmethod
    def two_markers(cls, tmp_path_factory) -> Run:
        """Ends at the approval gate rather than running a third attempt: the
        second diff only has to be *rendered* to be inspected, not applied."""
        return scenario(
            tmp_path_factory,
            "two_markers",
            edit_lists=[failing_edits(1), failing_edits(2)],
            answers=[True, False],
        )

    def test_attempt_twos_diff_contains_only_its_own_marker(self, two_markers):
        """The proof that every attempt starts from identical ground.

        If the run directory still held attempt 1's edit, attempt 2's diff would
        be rendered against *that* -- it would show `# attempt 1` being removed
        rather than not mentioning it at all. Instead each diff shows one marker
        being added to the pristine fixture.
        """
        first, second = two_markers.final.previous_diffs
        assert "# attempt 1" in first and "# attempt 2" not in first
        assert "# attempt 2" in second and "# attempt 1" not in second

    def test_every_attempt_opens_with_a_reset(self, two_markers):
        assert two_markers.names().count("baseline_reset") == 2

    def test_a_rejected_attempt_leaves_the_baseline_on_disk(self, two_markers):
        """Attempt 2 was rejected at the gate, so nothing from it was applied --
        and attempt 1's marker is gone too, because the reset removed it."""
        assert BUG in applied(two_markers)
        assert "# attempt" not in applied(two_markers)


# -- livelock ---------------------------------------------------------------


class TestLivelock:
    """Two attempts, the same marker, so the two diffs are byte-identical."""

    @pytest.fixture(scope="class")
    @classmethod
    def stuck(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "stuck", edit_lists=[failing_edits(1), failing_edits(1)]
        )

    def test_a_duplicate_diff_halts_the_run(self, stuck):
        assert stuck.final.status is Status.ESCALATED_LIVELOCK
        assert stuck.final.attempt_count == 2

    def test_previous_diffs_holds_one_entry_not_two(self, stuck):
        """The off-by-one the append-after-check ordering exists to avoid. The
        duplicate is never recorded, so the list holds only the diff it matched."""
        assert len(stuck.final.previous_diffs) == 1

    def test_the_reviewer_was_never_called_a_second_time(self, stuck):
        """This is the assertion that proves the check runs *before* Review. The
        final state cannot show it; the Reviewer's call count can."""
        assert len(stuck.reviewer.seen) == 1
        assert len(stuck.approver.seen) == 1

    def test_the_implementer_still_ran_twice(self, stuck):
        """Which is why the counter reads 2. The rule has no exceptions: the
        Implementer ran, so the attempt counted."""
        assert len(stuck.implementer.seen) == 2

    def test_the_halt_names_the_plan_as_the_likely_fault(self, stuck):
        [event] = [e for e in stuck.events if e["event"] == "run_finished"]

        assert "plan is the likely fault" in event["payload"]["reason"]

    def test_the_halt_is_logged_before_the_run_ends(self, stuck):
        names = stuck.names()

        assert names.index("livelock_detected") < names.index("run_finished")
        assert names[-1] == "run_finished"

    def test_the_terminal_state_carries_no_stale_review_or_result(self, stuck):
        """Attempt 1 set both. Attempt 2 halted upstream of either, so both must
        read `None` rather than attempt 1's approving verdict and red result."""
        assert stuck.final.review is None
        assert stuck.final.test_result is None
        # What attempt 2 did produce is still there.
        assert stuck.final.edits == failing_edits(1)
        assert stuck.final.diff is not None


# -- the retry cap ----------------------------------------------------------


class TestRetryCap:
    """Five distinct failing variants. Identical ones would trip livelock first
    and never reach the cap, which is why the markers have to differ."""

    @pytest.fixture(scope="class")
    @classmethod
    def capped(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "capped", edit_lists=[failing_edits(n) for n in range(1, 6)]
        )

    def test_five_failures_escalate(self, capped):
        assert capped.final.status is Status.ESCALATED_RETRY_LIMIT
        assert capped.final.attempt_count == MAX_ATTEMPTS == 5

    def test_every_attempt_rendered_a_distinct_diff(self, capped):
        assert len(capped.final.previous_diffs) == 5
        assert len(set(capped.final.previous_diffs)) == 5

    def test_the_implementer_ran_exactly_five_times(self, capped):
        """Not six. The cap is checked at the top of the loop, so the sixth pass
        halts before calling anything -- which is why this fails as a status
        rather than as a `ScriptExhausted` from a five-entry script."""
        assert len(capped.implementer.seen) == 5

    def test_attempt_five_got_the_same_gate_as_attempt_one(self, capped):
        """Invariant 1 has no fast paths. Every one of the five diffs was reviewed
        and shown to the human."""
        assert len(capped.reviewer.seen) == 5
        assert len(capped.approver.seen) == 5

    def test_the_loop_never_replans(self, capped):
        """`StubPlanner` holds a one-entry script, so a second call would raise.
        Surviving a five-attempt run is the assertion."""
        assert len(capped.planner.seen) == 1

    def test_the_last_failure_is_the_terminal_evidence(self, capped):
        assert isinstance(capped.final.evidence, TesterFailure)
        assert capped.final.evidence.failed_tests == [FAILING_NODEID]


# -- the human approval gate ------------------------------------------------


class TestHumanRejection:
    @pytest.fixture(scope="class")
    @classmethod
    def aborted(cls, tmp_path_factory) -> Run:
        """A second edit list is deliberately available and deliberately unused:
        a human "no" halts, it does not route back."""
        return scenario(
            tmp_path_factory,
            "aborted",
            edit_lists=[fixing_edits(), fixing_edits()],
            answers=[False],
        )

    def test_a_human_no_halts_the_run(self, aborted):
        assert aborted.final.status is Status.ABORTED_BY_HUMAN
        assert aborted.final.attempt_count == 1

    def test_it_does_not_route_back(self, aborted):
        """The human is rejecting for reasons the harness cannot see, so there is
        no evidence to write and nothing to tell the Implementer."""
        assert aborted.final.evidence is None
        assert len(aborted.implementer.seen) == 1

    def test_the_run_never_reached_the_tester(self, aborted):
        assert aborted.final.test_result is None
        assert "edits_applied" not in aborted.names()

    def test_nothing_reached_disk(self, aborted):
        assert BUG in applied(aborted)

    def test_the_rejected_diff_was_still_recorded(self, aborted):
        """`previous_diffs` is appended at the livelock check, which is upstream of
        the gate. A rejected diff is in the list; it just never reached disk."""
        assert len(aborted.final.previous_diffs) == 1
        assert aborted.final.previous_diffs == [aborted.approver.seen[0]]

    def test_the_denial_is_logged(self, aborted):
        assert "approval_denied" in aborted.names()
        assert "approval_granted" not in aborted.names()


# -- a Reviewer rejection ---------------------------------------------------


class TestReviewRejection:
    @pytest.fixture(scope="class")
    @classmethod
    def rejected(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory,
            "rejected",
            edit_lists=[failing_edits(1), fixing_edits()],
            verdicts=[REJECTED, APPROVED],
        )

    def test_a_rejection_routes_back(self, rejected):
        assert rejected.final.status is Status.SUCCEEDED
        assert rejected.final.attempt_count == 2

    def test_the_verdict_becomes_the_evidence(self, rejected):
        carried = rejected.implementer.seen[1]["evidence"]

        assert isinstance(carried, ReviewerRejection)
        assert carried.kind == "reviewer_rejection"
        assert carried.reason == REJECTED.reason
        assert carried.violated_constraints == REJECTED.violated_constraints

    def test_the_human_was_never_shown_a_rejected_diff(self, rejected):
        """The gate sits downstream of Review, so a rejected diff never reaches
        it -- and never reaches disk either."""
        assert len(rejected.approver.seen) == 1
        assert rejected.approver.seen[0] == rejected.diffs_reviewed[1]

    def test_a_rejected_attempt_still_rendered_and_recorded_its_diff(self, rejected):
        """Unlike a scope failure: the render happens upstream of Review."""
        assert len(rejected.final.previous_diffs) == 2

    def test_the_event_names_the_judged_rejection(self, rejected):
        assert "review_rejected" in rejected.names()
        assert "scope_check_failed" not in rejected.names()


# -- the mechanical scope check ---------------------------------------------


class TestScopeCheck:
    @pytest.fixture(scope="class")
    @classmethod
    def violated(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "violated", edit_lists=[out_of_scope_edits(), fixing_edits()]
        )

    def test_an_out_of_scope_edit_routes_back(self, violated):
        assert violated.final.status is Status.SUCCEEDED
        assert violated.final.attempt_count == 2

    def test_the_offending_path_is_the_evidence(self, violated):
        carried = violated.implementer.seen[1]["evidence"]

        assert isinstance(carried, ReviewerRejection)
        assert carried.violated_constraints == [MONEY]
        assert MONEY in carried.reason

    def test_the_reviewer_was_never_called(self, violated):
        """The scope check is upstream of Review, so a violation costs no review.
        The Reviewer's one call is attempt 2's."""
        assert len(violated.reviewer.seen) == 1
        assert len(violated.approver.seen) == 1

    def test_a_scope_failed_attempt_renders_no_diff(self, violated):
        """It is caught before the render, so it contributes nothing to
        `previous_diffs` -- which is also why two identical out-of-scope edits
        cannot livelock."""
        assert len(violated.final.previous_diffs) == 1

    def test_the_out_of_scope_file_never_reached_disk(self, violated):
        assert "# touched by the implementer" not in (
            Path(violated.final.repo_path) / MONEY
        ).read_text(encoding="utf-8")

    def test_the_event_names_the_mechanical_catch(self, violated):
        """`ReviewerRejection` deliberately cannot tell you which kind of
        rejection this was. The log can."""
        assert "scope_check_failed" in violated.names()
        assert "review_rejected" not in violated.names()


# -- per-attempt field clearing ---------------------------------------------


class TestPerAttemptFieldsAreCleared:
    """A scope failure is the sharpest case, because it reaches neither the
    render, nor Review, nor the Tester -- so every one of those fields must read
    `None` no matter what earlier attempts put there.

    Four red attempts set `review` and `test_result` over and over, then attempt
    5 is caught at the scope check and the cap ends the run. The terminal state is
    the one that attempt left behind. (`TestLivelock` makes the cheaper version of
    the same assertion on a two-attempt run.)
    """

    @pytest.fixture(scope="class")
    @classmethod
    def stale(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory,
            "stale",
            edit_lists=[failing_edits(n) for n in range(1, 5)] + [out_of_scope_edits()],
        )

    def test_the_run_ends_on_a_scope_failed_attempt(self, stale):
        assert stale.final.status is Status.ESCALATED_RETRY_LIMIT
        assert stale.final.attempt_count == 5
        assert stale.names()[-2] == "scope_check_failed"

    def test_no_stale_review_or_test_result_survives(self, stale):
        assert stale.final.review is None
        assert stale.final.test_result is None

    def test_no_stale_diff_survives(self, stale):
        """Attempt 5 never reached the render, so `diff` is `None` even though the
        four attempts before it each set one."""
        assert stale.final.diff is None
        assert len(stale.final.previous_diffs) == 4

    def test_the_evidence_is_attempt_fives_own(self, stale):
        """Not the `TesterFailure` from attempt 4. `evidence` is overwritten, never
        appended, and attempt 5 wrote its own."""
        assert isinstance(stale.final.evidence, ReviewerRejection)
        assert stale.final.evidence.violated_constraints == [MONEY]


# -- a suite that will not collect ------------------------------------------


class TestBrokenSuite:
    @pytest.fixture(scope="class")
    @classmethod
    def broken(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "broken", edit_lists=[broken_edits(), fixing_edits()]
        )

    def test_an_uncollectable_suite_escalates(self, broken):
        assert broken.final.status is Status.ESCALATED_BROKEN_SUITE
        assert broken.final.attempt_count == 1

    def test_it_escalates_rather_than_retrying(self, broken):
        """There is no failing test to aim a retry at, so another diff would burn
        an attempt producing a change nobody can evaluate. The second edit list
        is deliberately available and deliberately unused."""
        assert len(broken.implementer.seen) == 1

    def test_no_evidence_is_written(self, broken):
        """An escalation ends the run; it does not hand anything to an Implementer
        that will not be called again."""
        assert broken.final.evidence is None

    def test_the_result_that_triggered_it_is_still_in_state(self, broken):
        assert broken.final.test_result.summary_parsed is False
        assert broken.final.test_result.failed_tests == []
        assert broken.final.test_result.exit_code not in (0, 1)

    def test_the_diagnostic_is_logged(self, broken):
        assert "suite_not_collectable" in broken.names()
        assert "test_failed" not in broken.names()


# -- an empty edit list -----------------------------------------------------


class TestNoEdits:
    @pytest.fixture(scope="class")
    @classmethod
    def empty(cls, tmp_path_factory) -> Run:
        return scenario(tmp_path_factory, "empty", edit_lists=[[], fixing_edits()])

    def test_an_empty_edit_list_routes_back(self, empty):
        """`[]` is not a contract violation -- only `None` is -- so the loop has to
        judge it."""
        assert empty.final.status is Status.SUCCEEDED
        assert empty.final.attempt_count == 2

    def test_the_evidence_says_no_edits(self, empty):
        carried = empty.implementer.seen[1]["evidence"]

        assert isinstance(carried, ReviewerRejection)
        assert "no edits" in carried.reason
        assert carried.violated_constraints == []

    def test_nothing_downstream_was_called(self, empty):
        """Caught before the scope check, so no diff, no review, no gate."""
        assert len(empty.final.previous_diffs) == 1
        assert len(empty.reviewer.seen) == 1
        assert len(empty.approver.seen) == 1

    def test_the_branch_is_named_in_the_log(self, empty):
        assert "no_edits_produced" in empty.names()
        assert empty.names().count("diff_rendered") == 1


# -- path normalization -----------------------------------------------------


def windows_edits() -> list[FileEdit]:
    """The fix, with the path spelled the way a Windows Implementer would."""
    return [FileEdit(path="pricing\\discounts.py", new_content=fixing_edits()[0].new_content)]


class TestPathNormalization:
    @pytest.fixture(scope="class")
    @classmethod
    def backslashed(cls, tmp_path_factory) -> Run:
        return scenario(tmp_path_factory, "backslashed", edit_lists=[windows_edits()])

    def test_a_backslash_path_is_not_a_scope_violation(self, backslashed):
        """A Windows Implementer will emit `pricing\\discounts.py`. That is the
        same file the plan named, and reporting it as a scope violation would
        spend a retry on a path-separator bug while the log claimed the model had
        gone outside its plan."""
        assert backslashed.final.status is Status.SUCCEEDED
        assert "scope_check_failed" not in backslashed.names()

    def test_the_edit_landed_on_the_file_the_plan_named(self, backslashed):
        """The check and the write share one normalization, so an accepted
        spelling cannot resolve somewhere else."""
        assert FIX in applied(backslashed)

    def test_the_diff_labels_use_the_normalized_path(self, backslashed):
        assert f"--- a/{DISCOUNTS}" in backslashed.final.diff
        assert "\\" not in backslashed.final.diff.splitlines()[0]

    def test_a_dot_slash_target_file_matches_a_plain_path(self, tmp_path_factory):
        """Normalization applies to the plan's side too -- an LLM Planner may well
        write `./pricing/discounts.py`."""
        plan = PLAN.model_copy(update={"target_files": [f"./{DISCOUNTS}"]})

        run = scenario(
            tmp_path_factory, "dotslash", edit_lists=[fixing_edits()], plans=[plan]
        )

        assert run.final.status is Status.SUCCEEDED


# -- the rendered diff ------------------------------------------------------


class TestTheRenderedDiff:
    @pytest.fixture(scope="class")
    @classmethod
    def rendered(cls, tmp_path_factory) -> Run:
        return scenario(tmp_path_factory, "rendered", edit_lists=[fixing_edits()])

    def test_the_reviewer_sees_a_real_unified_diff(self, rendered):
        """The Reviewer reviews a genuine unified diff; it just isn't the model
        that produced it. Minimality is a property of the change, and the diff is
        the only view that shows it."""
        [shown] = rendered.diffs_reviewed
        lines = shown.splitlines()

        assert lines[0] == f"--- a/{DISCOUNTS}"
        assert lines[1] == f"+++ b/{DISCOUNTS}"
        assert any(line.startswith("@@ ") and line.endswith(" @@") for line in lines)
        assert any(line.startswith("-") and BUG in line for line in lines)
        assert any(line.startswith("+") and FIX in line for line in lines)

    def test_the_diff_the_reviewer_saw_is_the_one_in_state(self, rendered):
        assert rendered.diffs_reviewed == [rendered.final.diff]

    def test_the_diff_is_minimal_for_a_one_character_fix(self, rendered):
        """One line removed, one added. If the render were against something other
        than the baseline this would balloon."""
        lines = rendered.final.diff.splitlines()

        removed = [line for line in lines if line.startswith("-") and not line.startswith("---")]
        added = [line for line in lines if line.startswith("+") and not line.startswith("+++")]
        assert len(removed) == 1
        assert len(added) == 1

    def test_the_headers_carry_no_timestamp(self, rendered):
        """A timestamp would make every diff unique and livelock unreachable.
        `difflib` appends one only if given a date, so the assertion is that the
        header line ends at the path."""
        header = rendered.final.diff.splitlines()[0]

        assert header == f"--- a/{DISCOUNTS}"
        assert "\t" not in header

    def test_the_diff_names_no_absolute_path(self, rendered):
        """An absolute path would embed `task_id`, and with it a timestamp."""
        assert TASK_ID not in rendered.final.diff
        assert rendered.final.repo_path not in rendered.final.diff

    def test_rendering_is_reproducible(self, rendered, tmp_path):
        """The property livelock rests on: the same baseline plus the same edits
        gives the same bytes. Rendered here against a directory the run never
        touched."""
        fresh = prepare_run_dir("reference", FIXTURE, tmp_path)

        assert render_diff(fresh, fixing_edits()) == rendered.final.diff

    def test_a_multi_file_diff_renders_in_path_order(self, tmp_path_factory):
        """Deterministic ordering, so a multi-file change cannot render in two
        orders across attempts and read as progress."""
        plan = PLAN.model_copy(update={"target_files": [DISCOUNTS, MONEY]})
        both = fixing_edits() + out_of_scope_edits()

        run = scenario(tmp_path_factory, "multifile", edit_lists=[both], plans=[plan])

        assert run.final.status is Status.SUCCEEDED
        assert run.final.diff.index(f"--- a/{DISCOUNTS}") < run.final.diff.index(f"--- a/{MONEY}")


# -- invariant 1: on that specific diff -------------------------------------


class TestInvariantOne:
    """No diff reaches disk without a Review verdict AND a human approval **on
    that specific diff**. Per diff, never per task -- so the assertion is about
    byte identity across the three, not about all three having happened."""

    @pytest.fixture(scope="class")
    @classmethod
    def three_attempts(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory,
            "gated",
            edit_lists=[failing_edits(1), failing_edits(2), fixing_edits()],
        )

    def test_the_gate_sees_exactly_what_the_reviewer_saw_on_every_attempt(self, three_attempts):
        assert three_attempts.final.status is Status.SUCCEEDED
        assert len(three_attempts.approver.seen) == 3
        assert three_attempts.approver.seen == three_attempts.diffs_reviewed

    def test_each_attempt_was_gated_on_its_own_diff(self, three_attempts):
        """Not one approval carried across three attempts -- three distinct diffs,
        each shown once."""
        assert len(set(three_attempts.approver.seen)) == 3
        assert three_attempts.final.previous_diffs == three_attempts.approver.seen

    def test_the_approved_diff_describes_what_reached_disk(self, three_attempts, tmp_path):
        """Re-rendered against a pristine copy: the bytes the human approved are
        exactly the change that was applied, not merely a change."""
        fresh = prepare_run_dir("reference", FIXTURE, tmp_path)

        assert render_diff(fresh, three_attempts.final.edits) == three_attempts.approver.seen[-1]
        assert FIX in applied(three_attempts)

    def test_the_gate_precedes_the_apply_in_the_log(self, three_attempts):
        names = three_attempts.names()

        assert names.index("approval_granted") < names.index("edits_applied")
        assert names.index("edits_applied") < names.index("test_run_started")


# -- the event log ----------------------------------------------------------


class TestTheEventLog:
    @pytest.fixture(scope="class")
    @classmethod
    def two_attempts(cls, tmp_path_factory) -> Run:
        return scenario(
            tmp_path_factory, "logged", edit_lists=[failing_edits(1), fixing_edits()]
        )

    def test_a_fail_then_fix_run_logs_the_expected_sequence(self, two_attempts):
        """`stub_scripted` is a stub's own event, so this exact sequence is the
        stub-driven one. Everything else here is the loop's."""
        assert [(e["agent"], e["event"]) for e in two_attempts.events] == [
            ("harness", "run_started"),
            ("planner", "stub_scripted"),
            ("planner", "agent_produced"),
            ("harness", "baseline_reset"),
            ("implementer", "stub_scripted"),
            ("implementer", "agent_produced"),
            ("harness", "diff_rendered"),
            ("reviewer", "stub_scripted"),
            ("reviewer", "agent_produced"),
            ("harness", "approval_granted"),
            ("harness", "edits_applied"),
            ("tester", "test_run_started"),
            ("tester", "agent_produced"),
            ("harness", "test_failed"),
            ("harness", "baseline_reset"),
            ("implementer", "stub_scripted"),
            ("implementer", "agent_produced"),
            ("harness", "diff_rendered"),
            ("reviewer", "stub_scripted"),
            ("reviewer", "agent_produced"),
            ("harness", "approval_granted"),
            ("harness", "edits_applied"),
            ("tester", "test_run_started"),
            ("tester", "agent_produced"),
            ("harness", "run_finished"),
        ]

    def test_the_planner_is_stamped_attempt_zero(self, two_attempts):
        """The counter increments on the Implementer, so nothing before it has an
        attempt to belong to."""
        planner_events = [e for e in two_attempts.events if e["agent"] == "planner"]

        assert planner_events
        assert {e["attempt"] for e in planner_events} == {0}
        assert two_attempts.events[0]["event"] == "run_started"
        assert two_attempts.events[0]["attempt"] == 0

    def test_attempt_numbers_are_correct_throughout(self, two_attempts):
        """Non-decreasing, starting at 0, ending at 2, and each attempt's block
        opens with its own reset."""
        attempts = [e["attempt"] for e in two_attempts.events]

        assert attempts == sorted(attempts)
        assert attempts[0] == 0
        assert attempts[-1] == 2
        assert set(attempts) == {0, 1, 2}

        resets = [e["attempt"] for e in two_attempts.events if e["event"] == "baseline_reset"]
        assert resets == [1, 2]

    def test_every_agent_step_is_stamped_with_its_own_attempt(self, two_attempts):
        """A step's number is the attempt it belongs to, not the one before it.
        The Tester runs last in an attempt and must still read the same number as
        the Implementer that opened it."""
        by_attempt = {}
        for event in two_attempts.events:
            by_attempt.setdefault(event["attempt"], []).append(event["agent"])

        assert set(by_attempt[1]) == {"harness", "implementer", "reviewer", "tester"}
        assert set(by_attempt[2]) == {"harness", "implementer", "reviewer", "tester"}

    def test_run_finished_appears_exactly_once_and_last(self, two_attempts):
        names = two_attempts.names()

        assert names.count("run_finished") == 1
        assert names[-1] == "run_finished"

    def test_run_finished_carries_the_terminal_status(self, two_attempts):
        final = two_attempts.events[-1]

        assert final["payload"]["status"] == Status.SUCCEEDED.value
        assert final["payload"]["attempt_count"] == 2
        assert final["payload"]["reason"]

    def test_the_full_diff_is_inlined(self, two_attempts):
        """A run's log is self-contained and replayable on its own -- no references
        to external blobs."""
        rendered = [e for e in two_attempts.events if e["event"] == "diff_rendered"]

        assert len(rendered) == 2
        assert [e["payload"]["diff"] for e in rendered] == two_attempts.final.previous_diffs

    def test_the_loops_own_events_are_attributed_to_harness(self, two_attempts):
        loop_events = {
            "run_started",
            "baseline_reset",
            "diff_rendered",
            "approval_granted",
            "edits_applied",
            "test_failed",
            "run_finished",
        }
        agents = {e["agent"] for e in two_attempts.events if e["event"] in loop_events}

        assert agents == {"harness"}

    @pytest.mark.parametrize(
        ("status", "kwargs"),
        [
            (Status.SUCCEEDED, {"edit_lists": [fixing_edits()]}),
            (Status.ABORTED_BY_HUMAN, {"edit_lists": [fixing_edits()], "answers": [False]}),
            (Status.ESCALATED_LIVELOCK, {"edit_lists": [failing_edits(1), failing_edits(1)]}),
            (Status.ESCALATED_BROKEN_SUITE, {"edit_lists": [broken_edits()]}),
        ],
    )
    def test_every_exit_path_logs_one_run_finished(self, event_log, runs_root, status, kwargs):
        run = run_scenario(event_log, runs_root, **kwargs)

        assert run.final.status is status
        assert run.names().count("run_finished") == 1
        assert run.events[-1]["payload"]["status"] == status.value
