"""Tests for harness/agents/base.py.

The base class is the enforcement half of role purity, so these tests are mostly
about what it *refuses*: an agent called without its inputs, an agent that
produced nothing, an agent whose signature has drifted from the contract table,
an agent handed a field it has no business reading.

The probe agents here are deliberately tiny and local. Using the real agents
would test their behaviour as well as the base class's, and the base class's
behaviour is the only thing in question.
"""

import json

import pytest
from pydantic import ValidationError

from harness.agents.base import CONTRACTS, Agent, AgentContractError, Contract
from harness.events import EventLog
from harness.state import (
    FileEdit,
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    Status,
    TaskState,
    TestResult,
)

PLAN = Plan(
    summary="Use an inclusive lower bound.",
    steps=["Change > to >= in tier_for"],
    target_files=["pricing/discounts.py"],
    constraints=["Do not change the tier table"],
)


@pytest.fixture
def event_log(tmp_path) -> EventLog:
    return EventLog("fixture_repo_1_20260815T120000Z", log_dir=tmp_path / "logs")


def state(**overrides) -> TaskState:
    base = {
        "task_id": "fixture_repo_1_20260815T120000Z",
        "repo_path": "runs/fixture_repo_1_20260815T120000Z",
        "task_description": "An order at a tier boundary gets the wrong discount.",
        "failure_input": "1 failed, 19 passed",
    }
    return TaskState(**(base | overrides))


def records(event_log: EventLog) -> list[dict]:
    return [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]


# -- probe agents ------------------------------------------------------------


class ProbeReviewer(Agent):
    """A Reviewer that records exactly what it was handed and approves."""

    name = "reviewer"

    def __init__(self, event_log, verdict=None):
        super().__init__(event_log)
        self.seen: list[dict] = []
        self.verdict = verdict or ReviewVerdict(approved=True, reason="fine", violated_constraints=[])

    def _run(self, *, plan, diff):
        self.seen.append({"plan": plan, "diff": diff})
        return self.verdict


class ProbeImplementer(Agent):
    """An Implementer that records the optional `evidence` it was handed."""

    name = "implementer"

    def __init__(self, event_log, edits=None):
        super().__init__(event_log)
        self.seen: list[dict] = []
        self.edits = edits if edits is not None else [FileEdit(path="pricing/discounts.py", new_content="x = 1\n")]

    def _run(self, *, plan, evidence):
        self.seen.append({"plan": plan, "evidence": evidence})
        return self.edits


class SilentPlanner(Agent):
    """Runs, produces nothing. The other half of the contract."""

    name = "planner"

    def _run(self, *, task_description, failure_input):
        return None


class DriftedReviewer(Agent):
    """A signature that has drifted from the contract table."""

    name = "reviewer"

    def _run(self, *, plan):  # missing `diff`
        return ReviewVerdict(approved=True, reason="fine", violated_constraints=[])


class GreedyReviewer(Agent):
    """A signature asking for a field its contract does not supply."""

    name = "reviewer"

    def _run(self, *, plan, diff, test_result):
        return ReviewVerdict(approved=True, reason="fine", violated_constraints=[])


class WrongTypeReviewer(Agent):
    """Returns something that is not a ReviewVerdict."""

    name = "reviewer"

    def _run(self, *, plan, diff):
        return "looks good to me"


class Nameless(Agent):
    name = "auditor"

    def _run(self, **inputs):
        return "x"


# -- the contract table ------------------------------------------------------


class TestContractTable:
    def test_it_holds_exactly_the_four_agents(self):
        assert sorted(CONTRACTS) == ["implementer", "planner", "reviewer", "tester"]

    def test_it_matches_the_documented_contracts(self):
        """CLAUDE.md, "Agent contracts". If this test and that table disagree,
        one of them is wrong and it is worth finding out which."""
        assert CONTRACTS["planner"] == Contract(
            requires=("task_description", "failure_input"), produces="plan"
        )
        assert CONTRACTS["implementer"] == Contract(
            requires=("plan",), optional=("evidence",), produces="edits"
        )
        assert CONTRACTS["reviewer"] == Contract(requires=("plan", "diff"), produces="review")
        assert CONTRACTS["tester"] == Contract(requires=("repo_path",), produces="test_result")

    def test_no_agent_requires_a_field_it_produces(self):
        for name, contract in CONTRACTS.items():
            assert contract.produces not in contract.requires, name
            assert contract.produces not in contract.optional, name

    def test_each_field_has_exactly_one_producer(self):
        produced = [contract.produces for contract in CONTRACTS.values()]
        assert len(produced) == len(set(produced))

    def test_test_result_is_read_by_nobody(self):
        """Invariant 2 and 3, stated as a property of the table: the Tester writes
        `test_result` and no agent's contract lets it back in."""
        for name, contract in CONTRACTS.items():
            assert "test_result" not in contract.requires + contract.optional, name

    def test_an_agent_with_no_contract_cannot_be_constructed(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            Nameless(event_log)

        assert "auditor" in str(caught.value)
        assert "CONTRACTS" in str(caught.value)


# -- the requires assertion --------------------------------------------------


class TestRequiredFields:
    def test_a_missing_required_field_raises_naming_the_agent(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            ProbeReviewer(event_log).run(state(plan=PLAN))  # no diff

        assert str(caught.value).startswith("reviewer:")

    def test_all_missing_fields_are_named_at_once(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            ProbeReviewer(event_log).run(state())  # neither plan nor diff

        assert caught.value.missing == ("plan", "diff")
        assert "plan" in str(caught.value)
        assert "diff" in str(caught.value)

    def test_the_agent_never_runs_when_an_input_is_missing(self, event_log):
        probe = ProbeReviewer(event_log)

        with pytest.raises(AgentContractError):
            probe.run(state(plan=PLAN))

        assert probe.seen == []

    def test_a_present_required_field_passes(self, event_log):
        probe = ProbeReviewer(event_log)

        probe.run(state(plan=PLAN, diff="--- a\n+++ b\n"))

        assert probe.seen == [{"plan": PLAN, "diff": "--- a\n+++ b\n"}]

    def test_an_empty_string_is_present(self, event_log):
        """`None` means "not yet produced". An empty diff is a produced value --
        an odd one, but the Reviewer's job to judge, not the base class's."""
        probe = ProbeReviewer(event_log)

        probe.run(state(plan=PLAN, diff=""))

        assert probe.seen[0]["diff"] == ""


# -- what _run can and cannot see --------------------------------------------


class TestOnlyContractedFieldsAreVisible:
    def test_the_reviewer_is_not_handed_test_result(self, event_log):
        """Invariant 2, structurally. The Reviewer cannot free-ride on the
        Tester's verdict because it was never given one."""
        probe = ProbeReviewer(event_log)
        result = TestResult(
            passed=False,
            failed_tests=["tests/test_orders.py::test_x"],
            summary_parsed=True,
            traceback="AssertionError",
            stdout="1 failed",
            exit_code=1,
        )

        probe.run(state(plan=PLAN, diff="--- a\n", test_result=result))

        assert set(probe.seen[0]) == {"plan", "diff"}

    def test_an_optional_field_is_passed_when_absent(self, event_log):
        probe = ProbeImplementer(event_log)

        probe.run(state(plan=PLAN))

        assert probe.seen == [{"plan": PLAN, "evidence": None}]

    def test_an_optional_field_is_passed_when_present(self, event_log):
        probe = ProbeImplementer(event_log)
        evidence = ReviewerRejection(reason="out of scope", violated_constraints=["pricing/money.py"])

        probe.run(state(plan=PLAN, evidence=evidence))

        assert probe.seen[0]["evidence"] == evidence

    def test_a_missing_optional_field_does_not_raise(self, event_log):
        after = ProbeImplementer(event_log).run(state(plan=PLAN))

        assert after.edits is not None


# -- signature agreement -----------------------------------------------------


class TestSignatureAgreement:
    def test_a_signature_missing_a_contracted_field_fails_on_first_call(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            DriftedReviewer(event_log).run(state(plan=PLAN, diff="--- a\n"))

        assert "_run" in str(caught.value)
        assert "diff" in str(caught.value)

    def test_a_signature_asking_for_more_than_the_contract_fails(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            GreedyReviewer(event_log).run(state(plan=PLAN, diff="--- a\n"))

        assert "test_result" in str(caught.value)

    def test_the_mismatch_is_not_reported_as_a_plain_type_error(self, event_log):
        """A TypeError raised *inside* `_run` is a bug in the agent. Binding the
        signature before calling keeps the two diagnoses apart."""

        class ExplodingReviewer(Agent):
            name = "reviewer"

            def _run(self, *, plan, diff):
                raise TypeError("something inside the reviewer went wrong")

        with pytest.raises(TypeError) as caught:
            ExplodingReviewer(event_log).run(state(plan=PLAN, diff="--- a\n"))

        assert not isinstance(caught.value, AgentContractError)
        assert "inside the reviewer" in str(caught.value)


# -- the produces half -------------------------------------------------------


class TestProducedField:
    def test_the_base_class_writes_the_produced_field(self, event_log):
        verdict = ReviewVerdict(approved=False, reason="too broad", violated_constraints=["c1"])

        after = ProbeReviewer(event_log, verdict).run(state(plan=PLAN, diff="--- a\n"))

        assert after.review == verdict

    def test_the_returned_state_is_new(self, event_log):
        before = state(plan=PLAN, diff="--- a\n")

        after = ProbeReviewer(event_log).run(before)

        assert after is not before
        assert before.review is None

    def test_nothing_else_is_disturbed(self, event_log):
        before = state(plan=PLAN, diff="--- a\n", attempt_count=3, previous_diffs=["old"])

        after = ProbeReviewer(event_log).run(before)

        assert after.attempt_count == 3
        assert after.previous_diffs == ["old"]
        assert after.plan == PLAN
        assert after.status is Status.RUNNING

    def test_producing_none_is_a_contract_violation(self, event_log):
        with pytest.raises(AgentContractError) as caught:
            SilentPlanner(event_log).run(state())

        assert caught.value.missing == ("plan",)
        assert "plan" in str(caught.value)

    def test_the_result_is_revalidated(self, event_log):
        """`model_copy(update=...)` would let this through unchecked, and the bad
        value would surface somewhere much later with nothing pointing here."""
        with pytest.raises(ValidationError) as caught:
            WrongTypeReviewer(event_log).run(state(plan=PLAN, diff="--- a\n"))

        assert "review" in str(caught.value)
        assert "ReviewVerdict" in str(caught.value)

    def test_a_list_valued_field_is_revalidated_element_by_element(self, event_log):
        """The reason for revalidating rather than an isinstance check: `edits` is
        `list[FileEdit]`, and a list of the wrong thing is still a list."""

        class SloppyImplementer(Agent):
            name = "implementer"

            def _run(self, *, plan, evidence):
                return ["pricing/discounts.py"]  # str, not FileEdit

        with pytest.raises(ValidationError) as caught:
            SloppyImplementer(event_log).run(state(plan=PLAN))

        assert "edits" in str(caught.value)
        assert "FileEdit" in str(caught.value)


# -- event logging -----------------------------------------------------------


class TestEventLogging:
    def test_a_produced_value_is_logged_under_its_field_name(self, event_log):
        verdict = ReviewVerdict(approved=True, reason="minimal", violated_constraints=[])

        ProbeReviewer(event_log, verdict).run(state(plan=PLAN, diff="--- a\n"))

        [record] = records(event_log)
        assert record["event"] == "agent_produced"
        assert record["agent"] == "reviewer"
        assert ReviewVerdict.model_validate(record["payload"]["review"]) == verdict

    def test_the_attempt_comes_from_the_state(self, event_log):
        ProbeReviewer(event_log).run(state(plan=PLAN, diff="--- a\n", attempt_count=4))

        assert records(event_log)[0]["attempt"] == 4

    def test_a_violation_is_logged_before_it_is_raised(self, event_log):
        with pytest.raises(AgentContractError):
            ProbeReviewer(event_log).run(state())

        [record] = records(event_log)
        assert record["event"] == "contract_violation"
        assert record["agent"] == "reviewer"
        assert record["payload"]["missing"] == ["plan", "diff"]

    def test_a_domain_event_is_stamped_with_the_same_attempt(self, event_log):
        class ChattyReviewer(Agent):
            name = "reviewer"

            def _run(self, *, plan, diff):
                self.log(event="review_started", payload={"diff_lines": len(diff.splitlines())})
                return ReviewVerdict(approved=True, reason="ok", violated_constraints=[])

        ChattyReviewer(event_log).run(state(plan=PLAN, diff="a\nb\n", attempt_count=2))

        assert [r["event"] for r in records(event_log)] == ["review_started", "agent_produced"]
        assert all(r["attempt"] == 2 for r in records(event_log))
