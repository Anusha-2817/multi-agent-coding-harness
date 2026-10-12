"""Tests for harness_arms.py -- benchmark arms B and C.

A scripted client, never a real one: no API calls and no key. The client answers
by schema, so the same script drives both arms and the only thing that can
differ is whether a `ReviewVerdict` is ever asked for. Judged by the real Tester
against the real `fixture_repo_1`, as in `test_baseline.py`.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fixture_edits import DISCOUNTS, failing_edits, fixing_edits

from harness_arms import BYPASS_REASON, ArmEventLog, run_arm
from harness.agents.implementer import ImplementerResponse
from harness.llm import DEFAULT_MODEL, LLMResult
from harness.state import FileEdit, Plan, ReviewVerdict, Status, TaskState
from harness.workspace import prepare_run_dir

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
TASK_ID = "arm_fixture_repo_1_20261011T120000Z"
USAGE = {"input_tokens": 100, "output_tokens": 50, "thinking_tokens": 20}

PLAN = Plan(
    summary="Use an inclusive lower bound in tier_for.",
    steps=["Change > to >= in tier_for"],
    target_files=[DISCOUNTS],
    constraints=[],
)
APPROVED = ReviewVerdict(reason="hunk 1 carries out step 1", approved=True, violated_constraints=[])
REJECTED = ReviewVerdict(reason="hunk 1 adds a comment no step asks for", approved=False,
                         violated_constraints=[])


class SchemaClient:
    """Answers each call from the script for the schema it was asked for."""

    def __init__(self, *, edits: Sequence[list[FileEdit]], verdicts: Sequence[ReviewVerdict] = ()):
        self.scripts = {
            Plan: [PLAN],
            ImplementerResponse: [ImplementerResponse(edits=e) for e in edits],
            ReviewVerdict: list(verdicts),
        }
        self.calls: list[dict] = []

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens, "thinking_level": thinking_level})
        value = self.scripts[schema].pop(0)
        return LLMResult(value=value, model=DEFAULT_MODEL, stop_reason="STOP",
                         parse_attempts=1, usage=USAGE)

    def schemas(self) -> list[type]:
        return [call["schema"] for call in self.calls]

    def calls_for(self, schema: type) -> list[dict]:
        return [call for call in self.calls if call["schema"] is schema]


def run(tmp_path, arm, client):
    log = ArmEventLog(TASK_ID, arm, log_dir=tmp_path / "logs")
    runs_root = tmp_path / "runs"
    state = TaskState(
        task_id=TASK_ID,
        repo_path=prepare_run_dir(TASK_ID, FIXTURE, runs_root),
        task_description="Fix the failing test(s) in this repository.",
        failure_input="FAILED tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted",
    )
    final = run_arm(arm, state, fixture_path=FIXTURE, client=client, event_log=log,
                    runs_root=runs_root)
    events = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    return final, events


def named(events, name):
    return [event for event in events if event["event"] == name]


def harness_steps(events):
    return [event["event"] for event in events if event["agent"] == "harness"]


@pytest.fixture
def both_green(tmp_path):
    """The same script through both arms: one correct diff, approved in C."""
    b_client = SchemaClient(edits=[fixing_edits()])
    c_client = SchemaClient(edits=[fixing_edits()], verdicts=[APPROVED])
    b = run(tmp_path / "b", "B", b_client)
    c = run(tmp_path / "c", "C", c_client)
    return b, b_client, c, c_client


class TestReviewerCalls:
    def test_arm_b_makes_no_reviewer_model_call(self, both_green):
        (final, events), client, _, _ = both_green

        assert final.status is Status.SUCCEEDED
        assert client.schemas() == [Plan, ImplementerResponse]
        reviewer = [e["event"] for e in events if e["agent"] == "reviewer"]
        assert reviewer == ["review_bypassed", "agent_produced"]
        assert final.review.reason == BYPASS_REASON

    def test_arm_c_calls_the_reviewer_model(self, both_green):
        _, _, (final, events), client = both_green

        assert final.status is Status.SUCCEEDED
        assert client.schemas() == [Plan, ImplementerResponse, ReviewVerdict]
        reviewer = [e["event"] for e in events if e["agent"] == "reviewer"]
        assert reviewer == ["llm_request", "llm_response", "agent_produced"]
        assert final.review == APPROVED


class TestLogsAreComparable:
    def test_run_started_names_the_arm(self, both_green):
        (_, b_events), _, (_, c_events), _ = both_green

        assert named(b_events, "run_started")[0]["payload"]["arm"] == "B"
        assert named(c_events, "run_started")[0]["payload"]["arm"] == "C"

    def test_only_run_started_is_stamped(self, both_green):
        (_, b_events), _, (_, c_events), _ = both_green

        def stamped(events):
            return [e["event"] for e in events if "arm" in e["payload"]]

        # `review_bypassed` carries its own `arm`; the log adds it nowhere but run_started.
        assert stamped(b_events) == ["run_started", "review_bypassed"]
        assert stamped(c_events) == ["run_started"]

    def test_harness_steps_are_identical(self, both_green):
        (_, b_events), _, (_, c_events), _ = both_green

        assert harness_steps(b_events) == harness_steps(c_events) == [
            "run_started", "baseline_reset", "diff_rendered",
            "approval_granted", "edits_applied", "run_finished",
        ]

    def test_both_log_a_review_and_the_same_usage_events(self, both_green):
        (_, b_events), _, (_, c_events), _ = both_green

        for events in (b_events, c_events):
            produced = [e for e in named(events, "agent_produced") if e["agent"] == "reviewer"]
            assert len(produced) == 1 and "review" in produced[0]["payload"]
        # Planner and Implementer usage is logged identically; C adds the Reviewer's.
        b_usage = [e["agent"] for e in named(b_events, "llm_response")]
        c_usage = [e["agent"] for e in named(c_events, "llm_response")]
        assert b_usage == ["planner", "implementer"]
        assert c_usage == ["planner", "implementer", "reviewer"]

    def test_planner_and_implementer_prompts_are_identical(self, both_green):
        _, b_client, _, c_client = both_green

        for schema in (Plan, ImplementerResponse):
            b_call, c_call = b_client.calls_for(schema)[0], c_client.calls_for(schema)[0]
            assert b_call == c_call


class TestTheVerdictIsTheOnlyDifference:
    def test_a_diff_c_rejects_is_applied_and_tested_in_b(self, tmp_path):
        b_final, b_events = run(tmp_path / "b", "B",
                                SchemaClient(edits=[failing_edits(1), fixing_edits()]))
        c_final, c_events = run(tmp_path / "c", "C",
                                SchemaClient(edits=[failing_edits(1), fixing_edits()],
                                             verdicts=[REJECTED, APPROVED]))

        assert b_final.status is c_final.status is Status.SUCCEEDED
        assert b_final.attempt_count == c_final.attempt_count == 2
        assert b_final.evidence.kind == "tester_failure"
        assert c_final.evidence.kind == "reviewer_rejection"
        assert [e["attempt"] for e in named(b_events, "edits_applied")] == [1, 2]
        assert [e["attempt"] for e in named(c_events, "edits_applied")] == [2]

    def test_an_unknown_arm_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown arm"):
            run(tmp_path, "A", SchemaClient(edits=[]))
