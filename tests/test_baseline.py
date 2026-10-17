"""Tests for baseline.py -- the single-agent benchmark arm.

A scripted client, never a real one: no API calls and no key. Judged by the real
Tester against the real `fixture_repo_1`, the same way `test_loop.py` is, so
"retry then green" is two scripted edit lists and pytest decides the colour.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fixture_edits import DISCOUNTS, failing_edits, fixing_edits

from baseline import SYSTEM_PROMPT, run_baseline
from harness.agents.implementer import MAX_TOKENS, THINKING_LEVEL, ImplementerResponse
from harness.agents.stubs import StubApprover
from harness.events import EventLog
from harness.llm import DEFAULT_MODEL, LLMResult, LLMTruncatedError
from harness.loop import MAX_ATTEMPTS
from harness.state import FileEdit, Status, TaskState, TesterFailure
from harness.workspace import prepare_run_dir

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
TASK_ID = "baseline_fixture_repo_1_20261011T120000Z"
FAILURE_INPUT = "FAILED tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"
USAGE = {"input_tokens": 100, "output_tokens": 50, "thinking_tokens": 20}


class ScriptedClient:
    """Hands out one scripted response per call. Raises an entry that is an exception."""

    def __init__(self, responses: Sequence[list[FileEdit] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        self.calls.append(
            {"system": system, "user": user, "schema": schema,
             "max_tokens": max_tokens, "thinking_level": thinking_level}
        )
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return LLMResult(
            value=ImplementerResponse(edits=response),
            model=DEFAULT_MODEL,
            stop_reason="STOP",
            parse_attempts=1,
            usage=USAGE,
        )


def run(tmp_path, responses, answers=None):
    log = EventLog(TASK_ID, log_dir=tmp_path / "logs")
    runs_root = tmp_path / "runs"
    state = TaskState(
        task_id=TASK_ID,
        repo_path=prepare_run_dir(TASK_ID, FIXTURE, runs_root),
        task_description="Fix the failing test(s) in this repository.",
        failure_input=FAILURE_INPUT,
    )
    client = ScriptedClient(responses)
    approver = StubApprover([True] * len(responses) if answers is None else answers)
    final = run_baseline(
        state,
        fixture_path=FIXTURE,
        client=client,
        event_log=log,
        approve=approver,
        runs_root=runs_root,
    )
    return final, client, approver, log


def events(log: EventLog) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


def named(log: EventLog, name: str) -> list[dict]:
    return [event for event in events(log) if event["event"] == name]


class TestFirstTryGreen:
    def test_one_call_one_attempt_succeeded(self, tmp_path):
        final, client, approver, log = run(tmp_path, [fixing_edits()])

        assert final.status is Status.SUCCEEDED
        assert final.attempt_count == 1
        assert len(client.calls) == 1
        assert approver.seen == [final.diff]  # the gate saw the diff that was applied
        assert [e["event"] for e in named(log, "run_finished")] == ["run_finished"]

    def test_uses_the_implementers_settings_and_no_evidence(self, tmp_path):
        _, client, _, _ = run(tmp_path, [fixing_edits()])

        call = client.calls[0]
        assert call["system"] == SYSTEM_PROMPT
        assert call["schema"] is ImplementerResponse
        assert call["max_tokens"] == MAX_TOKENS
        assert call["thinking_level"] == THINKING_LEVEL
        assert FAILURE_INPUT in call["user"]
        assert f'<file path="{DISCOUNTS}">' in call["user"]
        assert "previous_attempt_failed_tests" not in call["user"]

    def test_logs_token_usage_per_call(self, tmp_path):
        _, _, _, log = run(tmp_path, [fixing_edits()])

        responses = named(log, "llm_response")
        assert len(responses) == 1
        assert responses[0]["agent"] == "baseline"
        assert responses[0]["payload"]["usage"] == USAGE


class TestRetryThenGreen:
    def test_the_failure_is_carried_into_the_second_prompt(self, tmp_path):
        final, client, _, log = run(tmp_path, [failing_edits(1), fixing_edits()])

        assert final.status is Status.SUCCEEDED
        assert final.attempt_count == 2
        assert isinstance(final.evidence, TesterFailure)
        assert "previous_attempt_failed_tests" not in client.calls[0]["user"]
        assert "previous_attempt_failed_tests" in client.calls[1]["user"]
        assert "test_an_order_at_the_bulk_threshold_is_discounted" in client.calls[1]["user"]
        assert len(named(log, "llm_response")) == 2
        assert len(named(log, "baseline_reset")) == 2

    def test_each_attempt_starts_from_the_baseline(self, tmp_path):
        _, client, _, _ = run(tmp_path, [failing_edits(1), fixing_edits()])

        # Attempt 1's marker reached disk, but the reset removed it before attempt 2.
        assert "# attempt 1" not in client.calls[1]["user"]


class TestHittingTheCap:
    def test_stops_at_max_attempts(self, tmp_path):
        responses = [failing_edits(n) for n in range(1, MAX_ATTEMPTS + 1)]
        final, client, _, log = run(tmp_path, responses)

        assert final.status is Status.ESCALATED_RETRY_LIMIT
        assert final.attempt_count == MAX_ATTEMPTS
        assert len(client.calls) == MAX_ATTEMPTS
        assert len(named(log, "test_failed")) == MAX_ATTEMPTS
        assert len(named(log, "llm_response")) == MAX_ATTEMPTS


class TestTruncatedResponse:
    def test_usage_is_logged_and_the_error_propagates(self, tmp_path):
        truncated = LLMTruncatedError("ran out of budget", stage="response", raw_text="")
        truncated.call_payload = {
            "model": DEFAULT_MODEL,
            "stop_reason": "MAX_TOKENS",
            "parse_attempts": 1,
            "usage": USAGE,
        }

        with pytest.raises(LLMTruncatedError):
            run(tmp_path, [failing_edits(1), truncated])

        log = EventLog(TASK_ID, log_dir=tmp_path / "logs")
        responses = named(log, "llm_response")
        assert [r["payload"]["stop_reason"] for r in responses] == ["STOP", "MAX_TOKENS"]
        assert responses[1]["attempt"] == 2
        assert named(log, "run_finished") == []  # ends as an Implementer truncation does


class TestTestFileEdits:
    def test_are_applied_and_flagged(self, tmp_path):
        test_file = "tests/test_orders.py"
        original = (FIXTURE / test_file).read_text(encoding="utf-8")
        edits = fixing_edits() + [FileEdit(path=test_file, new_content=original + "\n# touched\n")]

        final, _, _, log = run(tmp_path, [edits])

        assert final.status is Status.SUCCEEDED
        applied = named(log, "edits_applied")[0]["payload"]
        assert applied["test_paths"] == [test_file]
        assert set(applied["paths"]) == {DISCOUNTS, test_file}


# -- diff mode -------------------------------------------------------------------

from fixture_edits import BUG, FIX  # noqa: E402

from baseline import SYSTEM_PROMPT_DIFF  # noqa: E402
from harness.agents.implementer import DiffImplementerResponse, SearchReplace  # noqa: E402
from harness.state import ReviewerRejection  # noqa: E402

FIX_BLOCK = [SearchReplace(path=DISCOUNTS, search=BUG, replace=FIX)]
MISS_BLOCK = [SearchReplace(path=DISCOUNTS, search="if quantity >> tier:", replace=FIX)]
# Arm A is shown test files by path only, so a block guessing at one's contents
# cannot match: the blind rewrite full mode allows is not expressible here.
BLIND_TEST_BLOCK = [SearchReplace(path="tests/test_orders.py", search="def test_guess():", replace="x")]


class DiffScriptedClient(ScriptedClient):
    """`ScriptedClient`, answering with edit blocks instead of whole files."""

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens, "thinking_level": thinking_level})
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return LLMResult(value=DiffImplementerResponse(edits=response), model=DEFAULT_MODEL,
                         stop_reason="STOP", parse_attempts=1, usage=USAGE)


def run_diff(tmp_path, responses):
    log = EventLog(TASK_ID, log_dir=tmp_path / "logs")
    runs_root = tmp_path / "runs"
    state = TaskState(task_id=TASK_ID, repo_path=prepare_run_dir(TASK_ID, FIXTURE, runs_root),
                      task_description="Fix the failing test(s) in this repository.",
                      failure_input=FAILURE_INPUT)
    client = DiffScriptedClient(responses)
    final = run_baseline(state, fixture_path=FIXTURE, client=client, event_log=log,
                         approve=StubApprover([True] * len(responses)), runs_root=runs_root,
                         edit_format="diff")
    return final, client, log


class TestDiffMode:
    def test_a_block_that_applies_goes_green_in_one_call(self, tmp_path):
        final, client, log = run_diff(tmp_path, [FIX_BLOCK])

        assert final.status is Status.SUCCEEDED and final.attempt_count == 1
        assert final.edits == fixing_edits()  # resolved to the same full-file edit
        call = client.calls[0]
        assert (call["system"], call["schema"]) == (SYSTEM_PROMPT_DIFF, DiffImplementerResponse)
        assert (call["max_tokens"], call["thinking_level"]) == (MAX_TOKENS, THINKING_LEVEL)
        assert named(log, "run_started")[0]["payload"]["edit_format"] == "diff"
        request = named(log, "llm_request")[0]
        assert request["agent"] == "baseline" and request["payload"]["edit_format"] == "diff"

    def test_the_repair_call_rescues_a_block_and_costs_no_attempt(self, tmp_path):
        final, client, log = run_diff(tmp_path, [MISS_BLOCK, FIX_BLOCK])

        assert final.status is Status.SUCCEEDED and final.attempt_count == 1
        assert len(client.calls) == 2
        assert [e["payload"].get("repair_of") for e in named(log, "llm_request")] == [None, "edit_apply"]
        assert named(log, "edit_apply_failed") == []

    def test_a_block_still_failing_after_repair_consumes_the_attempt(self, tmp_path):
        final, client, log = run_diff(tmp_path, [MISS_BLOCK, MISS_BLOCK, FIX_BLOCK])

        assert final.status is Status.SUCCEEDED and final.attempt_count == 2
        assert len(client.calls) == 3
        [failed] = named(log, "edit_apply_failed")
        assert failed["attempt"] == 1 and failed["agent"] == "harness"
        assert isinstance(final.evidence, ReviewerRejection)
        assert "search began: 'if quantity >> tier:'" in client.calls[2]["user"]
        assert len(named(log, "edits_applied")) == 1

    def test_a_blind_test_file_edit_cannot_apply(self, tmp_path):
        final, _, log = run_diff(tmp_path, [BLIND_TEST_BLOCK, BLIND_TEST_BLOCK, FIX_BLOCK])

        assert final.status is Status.SUCCEEDED
        [failed] = named(log, "edit_apply_failed")
        assert failed["payload"]["failures"][0]["path"] == "tests/test_orders.py"
        assert all(e["payload"]["test_paths"] == [] for e in named(log, "edits_applied"))

    def test_full_mode_stamps_nothing_new(self, tmp_path):
        _, _, _, log = run(tmp_path, [fixing_edits()])

        assert "edit_format" not in named(log, "run_started")[0]["payload"]
        assert "edit_format" not in named(log, "llm_request")[0]["payload"]

    def test_an_unknown_format_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown edit format"):
            run_baseline(None, fixture_path=FIXTURE, client=None, event_log=None,
                         approve=None, edit_format="patch")
