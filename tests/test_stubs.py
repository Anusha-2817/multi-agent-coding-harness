"""Tests for harness/agents/stubs.py and tests/fixture_edits.py.

Two halves. The first is the stubs themselves: they return the script in order,
record what they were handed, and raise when the script runs out.

The second is the part that would otherwise be assumed. The failing-variant
generator is only useful if a failing variant genuinely produces a red suite and
the fixing variant a green one, and the only way to know that is to run them
through the real Tester against the real fixture. A generator that quietly
produced two green suites would make every "fail twice, then succeed" test in
Phase 2.3 pass for the wrong reason.
"""

from pathlib import Path

import pytest
from fixture_edits import (
    BUG,
    DISCOUNTS,
    FIX,
    broken_edits,
    failing_edits,
    fixing_edits,
    write_edits,
)

from harness.agents.stubs import (
    ScriptExhausted,
    StubImplementer,
    StubPlanner,
    StubReviewer,
)
from harness.agents.tester import Tester
from harness.events import EventLog
from harness.state import FileEdit, Plan, ReviewerRejection, ReviewVerdict, TaskState
from harness.workspace import prepare_run_dir

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

FAILING_NODEID = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"

PLAN = Plan(
    summary="Use an inclusive lower bound.",
    steps=["Change > to >= in tier_for"],
    target_files=[DISCOUNTS],
    constraints=["Do not change the tier table"],
)
APPROVED = ReviewVerdict(approved=True, reason="minimal and faithful", violated_constraints=[])
REJECTED = ReviewVerdict(approved=False, reason="too broad", violated_constraints=["minimality"])


@pytest.fixture
def event_log(tmp_path) -> EventLog:
    return EventLog("fixture_repo_1_20260815T120000Z", log_dir=tmp_path / "logs")


@pytest.fixture
def runs_root(tmp_path) -> Path:
    return tmp_path / "runs"


def state(**overrides) -> TaskState:
    base = {
        "task_id": "fixture_repo_1_20260815T120000Z",
        "repo_path": "runs/fixture_repo_1_20260815T120000Z",
        "task_description": "An order at a tier boundary gets the wrong discount.",
        "failure_input": "1 failed, 19 passed",
    }
    return TaskState(**(base | overrides))


# -- the stubs ---------------------------------------------------------------


class TestScriptOrder:
    def test_the_planner_returns_its_plan(self, event_log):
        after = StubPlanner(event_log, [PLAN]).run(state())

        assert after.plan == PLAN

    def test_the_implementer_returns_each_entry_in_order(self, event_log):
        script = [failing_edits(1), failing_edits(2), fixing_edits()]
        stub = StubImplementer(event_log, script)

        produced = [stub.run(state(plan=PLAN)).edits for _ in range(3)]

        assert produced == script

    def test_the_reviewer_returns_each_verdict_in_order(self, event_log):
        stub = StubReviewer(event_log, [REJECTED, APPROVED])

        first = stub.run(state(plan=PLAN, diff="--- a\n")).review
        second = stub.run(state(plan=PLAN, diff="--- b\n")).review

        assert [first, second] == [REJECTED, APPROVED]


class TestScriptExhaustion:
    def test_the_implementer_raises_when_the_script_runs_out(self, event_log):
        stub = StubImplementer(event_log, [fixing_edits()])
        stub.run(state(plan=PLAN))

        with pytest.raises(ScriptExhausted) as caught:
            stub.run(state(plan=PLAN))

        assert "implementer" in str(caught.value)
        assert "call 2" in str(caught.value)

    def test_the_planner_raises_if_the_loop_ever_plans_twice(self, event_log):
        """v1 does not replan -- routing rejections back to the Planner is a v2
        item. A one-entry script makes that an assertion rather than a hope."""
        stub = StubPlanner(event_log, [PLAN])
        stub.run(state())

        with pytest.raises(ScriptExhausted):
            stub.run(state())

    def test_the_reviewer_raises_when_the_script_runs_out(self, event_log):
        stub = StubReviewer(event_log, [APPROVED])
        stub.run(state(plan=PLAN, diff="--- a\n"))

        with pytest.raises(ScriptExhausted):
            stub.run(state(plan=PLAN, diff="--- a\n"))

    def test_an_empty_script_raises_on_the_first_call(self, event_log):
        with pytest.raises(ScriptExhausted) as caught:
            StubReviewer(event_log, []).run(state(plan=PLAN, diff="--- a\n"))

        assert "0 entries" in str(caught.value)

    def test_the_message_is_singular_for_a_one_entry_script(self, event_log):
        stub = StubPlanner(event_log, [PLAN])
        stub.run(state())

        with pytest.raises(ScriptExhausted) as caught:
            stub.run(state())

        assert "1 entry" in str(caught.value)


class TestRecordingWhatWasHanded:
    def test_the_implementer_records_the_evidence_it_was_given(self, event_log):
        """"Retry with evidence" is otherwise untestable: you can see a second
        attempt happen, but not that the failure was carried into it."""
        evidence = ReviewerRejection(reason="edited an out-of-scope file", violated_constraints=["pricing/money.py"])
        stub = StubImplementer(event_log, [failing_edits(1), fixing_edits()])

        stub.run(state(plan=PLAN))
        stub.run(state(plan=PLAN, evidence=evidence))

        assert stub.seen[0]["evidence"] is None
        assert stub.seen[1]["evidence"] == evidence

    def test_the_reviewer_records_the_diff_it_was_shown(self, event_log):
        stub = StubReviewer(event_log, [APPROVED])

        stub.run(state(plan=PLAN, diff="--- a\n+++ b\n"))

        assert stub.seen == [{"plan": PLAN, "diff": "--- a\n+++ b\n"}]

    def test_the_planner_records_its_two_inputs(self, event_log):
        stub = StubPlanner(event_log, [PLAN])

        stub.run(state())

        assert set(stub.seen[0]) == {"task_description", "failure_input"}

    def test_a_stub_sees_only_its_contracted_fields(self, event_log):
        """The stubs inherit the base class's filtering, so a scripted Reviewer
        is as blind to `test_result` as a real one."""
        stub = StubReviewer(event_log, [APPROVED])

        stub.run(state(plan=PLAN, diff="--- a\n", edits=[FileEdit(path=DISCOUNTS, new_content="x")]))

        assert set(stub.seen[0]) == {"plan", "diff"}


class TestStubsHonourTheContract:
    def test_a_stub_still_asserts_its_required_fields(self, event_log):
        from harness.agents.base import AgentContractError

        with pytest.raises(AgentContractError):
            StubImplementer(event_log, [fixing_edits()]).run(state())  # no plan

    def test_a_stub_logs_which_script_entry_it_used(self, event_log):
        import json

        stub = StubReviewer(event_log, [APPROVED])
        stub.run(state(plan=PLAN, diff="--- a\n"))

        events = [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert [e["event"] for e in events] == ["stub_scripted", "agent_produced"]
        assert events[0]["payload"] == {"call": 1, "entries": 1}


# -- the edit generators, verified through the real Tester -------------------


class TestEditGenerators:
    def test_a_failing_variant_leaves_the_bug_in_place(self):
        [edit] = failing_edits(1)

        assert edit.path == DISCOUNTS
        assert BUG in edit.new_content
        assert FIX not in edit.new_content

    def test_successive_failing_variants_differ_byte_wise(self):
        """Identical bytes would trip the livelock check on attempt 2 and halt a
        run the test meant to send around the retry path."""
        first = failing_edits(1)[0].new_content
        second = failing_edits(2)[0].new_content

        assert first != second
        assert BUG in first and BUG in second

    def test_the_same_marker_twice_is_byte_identical(self):
        """Which is how a livelock test asks for a duplicate."""
        assert failing_edits(1)[0].new_content == failing_edits(1)[0].new_content

    def test_the_fixing_variant_carries_the_one_character_fix(self):
        [edit] = fixing_edits()

        assert FIX in edit.new_content
        assert BUG not in edit.new_content

    def test_the_generators_do_not_write_to_the_fixture(self):
        before = (FIXTURE / DISCOUNTS).read_text(encoding="utf-8")

        failing_edits(1)
        fixing_edits()
        broken_edits()

        assert (FIXTURE / DISCOUNTS).read_text(encoding="utf-8") == before


class TestGeneratorsAgainstTheRealTester:
    """The claim that matters: red is really red and green is really green."""

    def test_a_failing_variant_produces_a_red_suite(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        write_edits(repo_path, failing_edits(1))

        result = Tester(event_log).run(state(repo_path=repo_path)).test_result

        assert result.passed is False
        assert result.exit_code == 1
        assert result.failed_tests == [FAILING_NODEID]
        assert result.summary_parsed is True

    def test_the_second_failing_variant_is_red_for_the_same_reason(self, runs_root, event_log):
        """The marker must be inert. A variant that failed differently would make
        a retry test pass for a reason the test did not intend."""
        repo_path = prepare_run_dir("run_b", FIXTURE, runs_root)
        write_edits(repo_path, failing_edits(2))

        result = Tester(event_log).run(state(repo_path=repo_path)).test_result

        assert result.failed_tests == [FAILING_NODEID]
        assert "1 failed, 19 passed" in result.stdout

    def test_the_fixing_variant_produces_a_green_suite(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_c", FIXTURE, runs_root)
        write_edits(repo_path, fixing_edits())

        result = Tester(event_log).run(state(repo_path=repo_path)).test_result

        assert result.passed is True
        assert result.exit_code == 0
        assert result.failed_tests == []
        assert "20 passed" in result.stdout

    def test_the_broken_variant_cannot_be_collected(self, runs_root, event_log):
        """`summary_parsed=False` is the branch that must escalate rather than
        retry, and this is the only generator that reaches it."""
        repo_path = prepare_run_dir("run_d", FIXTURE, runs_root)
        write_edits(repo_path, broken_edits())

        result = Tester(event_log).run(state(repo_path=repo_path)).test_result

        assert result.passed is False
        assert result.summary_parsed is False
        assert result.exit_code not in (0, 1)
        assert result.failed_tests == []

    def test_a_scripted_run_goes_red_red_green(self, runs_root, event_log):
        """The whole "fail twice, then succeed" shape, driven by the stub and
        judged by the real Tester -- with each attempt against a fresh baseline,
        as the loop will do it."""
        implementer = StubImplementer(event_log, [failing_edits(1), failing_edits(2), fixing_edits()])
        outcomes = []
        diffs = []

        for attempt in range(1, 4):
            repo_path = prepare_run_dir(f"attempt_{attempt}", FIXTURE, runs_root)
            edits = implementer.run(state(plan=PLAN, attempt_count=attempt)).edits
            diffs.append(edits[0].new_content)
            write_edits(repo_path, edits)
            outcomes.append(Tester(event_log).run(state(repo_path=repo_path)).test_result.passed)

        assert outcomes == [False, False, True]
        assert len(set(diffs)) == 3
