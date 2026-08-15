"""Tests for harness/state.py.

These cover the three properties the typed state exists to guarantee: Group B
fields start as None so the agent contract assertion means something, malformed
state fails at construction rather than mid-run, and the evidence union survives
a trip through the JSONL event log.
"""

import pytest
from pydantic import ValidationError

from harness.state import (
    STDOUT_TAIL_LIMIT,
    Evidence,
    FileEdit,
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    Status,
    TaskState,
    TesterFailure,
    TestResult,
    make_task_id,
)

# `TestResult` and `TesterFailure` match pytest's `Test*` collection glob; the
# opt-out that keeps them from being collected lives in the root conftest.py.

# The Group A fields, which the harness supplies at init.
INIT_FIELDS = {
    "task_id": "fixture_repo_1_20260814T120000Z",
    "repo_path": "runs/fixture_repo_1_20260814T120000Z",
    "task_description": "Fix the failing test in calculator.py",
    "failure_input": "AssertionError: assert 5 == 6",
}


def make_state(**overrides) -> TaskState:
    return TaskState(**{**INIT_FIELDS, **overrides})


class TestConstruction:
    def test_constructs_with_group_b_fields_as_none(self):
        state = make_state()

        assert state.plan is None
        assert state.edits is None
        assert state.diff is None
        assert state.review is None
        assert state.test_result is None
        assert state.evidence is None

    def test_group_a_fields_are_carried_through(self):
        state = make_state()

        assert state.task_id == INIT_FIELDS["task_id"]
        assert state.repo_path == INIT_FIELDS["repo_path"]
        assert state.task_description == INIT_FIELDS["task_description"]
        assert state.failure_input == INIT_FIELDS["failure_input"]

    def test_group_c_fields_get_real_defaults(self):
        state = make_state()

        assert state.attempt_count == 0
        assert state.previous_diffs == []
        assert state.status is Status.RUNNING

    @pytest.mark.parametrize("missing", sorted(INIT_FIELDS))
    def test_missing_group_a_field_raises(self, missing):
        fields = {k: v for k, v in INIT_FIELDS.items() if k != missing}

        with pytest.raises(ValidationError) as exc_info:
            TaskState(**fields)

        assert missing in str(exc_info.value)

    def test_unknown_field_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            make_state(tset_result="typo for test_result")

        assert "tset_result" in str(exc_info.value)

    def test_unknown_field_raises_on_nested_models_too(self):
        with pytest.raises(ValidationError):
            FileEdit(path="a.py", new_content="x = 1", mode="append")


class TestTestResultModel:
    def test_summary_parsed_is_required(self):
        with pytest.raises(ValidationError) as exc_info:
            TestResult(passed=False, failed_tests=[], traceback="", stdout="", exit_code=1)

        assert "summary_parsed" in str(exc_info.value)

    def test_an_unreadable_summary_is_distinct_from_nothing_failing(self):
        """`failed_tests == []` alone cannot tell these two apart."""
        nothing_failed = TestResult(
            passed=True, failed_tests=[], summary_parsed=True, traceback="", stdout="", exit_code=0
        )
        could_not_tell = TestResult(
            passed=False, failed_tests=[], summary_parsed=False, traceback="boom", stdout="", exit_code=2
        )

        assert nothing_failed.failed_tests == could_not_tell.failed_tests == []
        assert nothing_failed.summary_parsed != could_not_tell.summary_parsed


class TestReviewVerdict:
    def test_violated_constraints_is_required_even_when_approving(self):
        with pytest.raises(ValidationError) as exc_info:
            ReviewVerdict(approved=True, reason="Looks good")

        assert "violated_constraints" in str(exc_info.value)

    def test_approving_verdict_states_the_empty_list_explicitly(self):
        verdict = ReviewVerdict(approved=True, reason="Looks good", violated_constraints=[])

        assert verdict.violated_constraints == []


class TestStdoutTailTruncation:
    def test_long_stdout_tail_is_truncated_to_the_limit(self):
        stdout = "".join(str(i % 10) for i in range(STDOUT_TAIL_LIMIT * 3))

        failure = TesterFailure(failed_tests=[], traceback="", stdout_tail=stdout)

        assert len(failure.stdout_tail) == STDOUT_TAIL_LIMIT

    def test_truncation_keeps_the_tail_not_the_head(self):
        stdout = "HEAD" + ("." * STDOUT_TAIL_LIMIT) + "TAIL"

        failure = TesterFailure(failed_tests=[], traceback="", stdout_tail=stdout)

        assert failure.stdout_tail == stdout[-STDOUT_TAIL_LIMIT:]
        assert failure.stdout_tail.endswith("TAIL")
        assert "HEAD" not in failure.stdout_tail

    def test_short_stdout_tail_is_left_alone(self):
        failure = TesterFailure(failed_tests=[], traceback="", stdout_tail="1 failed")

        assert failure.stdout_tail == "1 failed"


class TestEvidenceRoundTrip:
    """The event log inlines full state as JSON. The `kind` discriminator is what
    lets the union come back as the right type."""

    def test_reviewer_rejection_round_trips(self):
        state = make_state(
            evidence=ReviewerRejection(
                reason="Rewrote an unrelated helper",
                violated_constraints=["touch only calculator.py"],
            )
        )

        restored = TaskState.model_validate_json(state.model_dump_json())

        assert isinstance(restored.evidence, ReviewerRejection)
        assert restored.evidence.kind == "reviewer_rejection"
        assert restored.evidence.reason == "Rewrote an unrelated helper"
        assert restored.evidence.violated_constraints == ["touch only calculator.py"]
        assert restored == state

    def test_tester_failure_round_trips(self):
        state = make_state(
            evidence=TesterFailure(
                failed_tests=["tests/test_calculator.py::test_add"],
                traceback="AssertionError: assert 5 == 6",
                stdout_tail="1 failed, 3 passed",
            )
        )

        restored = TaskState.model_validate_json(state.model_dump_json())

        assert isinstance(restored.evidence, TesterFailure)
        assert restored.evidence.kind == "tester_failure"
        assert restored.evidence.failed_tests == ["tests/test_calculator.py::test_add"]
        assert restored.evidence.stdout_tail == "1 failed, 3 passed"
        assert restored == state

    def test_kind_is_written_into_the_serialised_json(self):
        state = make_state(
            evidence=TesterFailure(failed_tests=[], traceback="", stdout_tail="")
        )

        assert '"kind":"tester_failure"' in state.model_dump_json()

    def test_evidence_with_an_unknown_kind_is_rejected(self):
        with pytest.raises(ValidationError):
            make_state(evidence={"kind": "human_rejection", "reason": "no"})

    def test_none_evidence_round_trips_as_none(self):
        state = make_state()

        restored = TaskState.model_validate_json(state.model_dump_json())

        assert restored.evidence is None


class TestFullStateRoundTrip:
    def test_a_populated_state_survives_the_event_log(self):
        state = make_state(
            plan=Plan(
                summary="Correct the off-by-one in add()",
                steps=["Read calculator.py", "Fix the return expression"],
                target_files=["calculator.py"],
                constraints=["Do not modify the tests"],
            ),
            edits=[FileEdit(path="calculator.py", new_content="def add(a, b):\n    return a + b\n")],
            diff="--- a/calculator.py\n+++ b/calculator.py\n",
            review=ReviewVerdict(approved=True, reason="Minimal and in scope", violated_constraints=[]),
            test_result=TestResult(
                passed=True,
                failed_tests=[],
                summary_parsed=True,
                traceback="",
                stdout="4 passed",
                exit_code=0,
            ),
            attempt_count=2,
            previous_diffs=["--- a/calculator.py\n+++ b/calculator.py\n(older)"],
            status=Status.SUCCEEDED,
        )

        restored = TaskState.model_validate_json(state.model_dump_json())

        assert restored == state

    def test_status_serialises_as_its_string_value(self):
        state = make_state(status=Status.ESCALATED_LIVELOCK)

        assert '"status":"escalated_livelock"' in state.model_dump_json()


class TestMakeTaskId:
    def test_task_id_starts_with_the_fixture_dir_name(self):
        assert make_task_id("fixture_repo_1").startswith("fixture_repo_1_")

    def test_task_id_is_safe_as_a_path_segment(self):
        task_id = make_task_id("fixture_repo_1")

        assert not set(task_id) & set(':/\\<>"|?*')
