"""The Stage 0 probe's offline parts: trigger replay, prompt, grading, gate.

No call is made and no bench file is read; events are built here.
"""

from __future__ import annotations

import json

import pytest

import probe_replanner
from harness.agents.planner import SYSTEM_PROMPT
from harness.events import EventLog
from harness.llm import LLMResult, LLMTransportError, LLMTruncatedError
from harness.state import Plan
from probe_replanner import (
    REPLAN_SYSTEM_PROMPT,
    gate,
    grade,
    render_replan_message,
    replay_trigger,
)

PLAN = Plan(
    summary="Use the source slice for FSTRING_MIDDLE.",
    steps=["In src_to_tokens, set tok_text = line[scol:ecol] for FSTRING_MIDDLE."],
    target_files=["tokenize_rt.py"],
    constraints=["Only tokenize_rt.py changes."],
)
FAIL_A = "E   ' a {' != ' a {{'"
FAIL_B = "E   IndexError"


def ev(attempt, agent, event, **payload):
    return {"attempt": attempt, "agent": agent, "event": event, "payload": payload}


def attempt_tested(attempt, traceback, *, passed=False, summary_parsed=True):
    return [
        ev(attempt, "harness", "diff_rendered", diff=f"diff {attempt}\n"),
        ev(attempt, "tester", "agent_produced", test_result={
            "passed": passed, "failed_tests": [] if passed else ["t::x"],
            "summary_parsed": summary_parsed, "traceback": traceback,
            "stdout": "tail", "exit_code": 0 if passed else 1,
        }),
    ]


def run(*attempts):
    events = [
        ev(0, "harness", "failure_discovery", output="original failure"),
        ev(0, "planner", "agent_produced", plan=PLAN.model_dump()),
    ]
    for chunk in attempts:
        events.extend(chunk)
    return events


class TestReplayTrigger:
    def test_two_identical_tested_failures_trigger_with_the_second(self):
        trigger = replay_trigger("r", run(attempt_tested(1, FAIL_A), attempt_tested(2, FAIL_A)))

        assert (trigger.kind, trigger.at_attempt, trigger.attempts_left) == ("stalled_failure", 2, 3)
        assert trigger.tried_diff == "diff 2\n"
        assert trigger.failure.traceback == FAIL_A
        assert trigger.plan == PLAN and trigger.failure_input == "original failure"

    def test_a_changed_failure_is_progress_not_a_stall(self):
        trigger = replay_trigger("r", run(attempt_tested(1, FAIL_B), attempt_tested(2, FAIL_A), attempt_tested(3, FAIL_A)))

        assert trigger.at_attempt == 3

    def test_livelock_hands_over_the_last_tested_diff_not_the_duplicate(self):
        events = run(
            attempt_tested(2, FAIL_A),
            [ev(3, "harness", "review_rejected", reason="stray hunk")],
            [ev(4, "harness", "diff_rendered", diff="diff 2\n"),
             ev(4, "harness", "livelock_detected", diff="diff 2\n")],
        )
        trigger = replay_trigger("r", events)

        assert (trigger.kind, trigger.at_attempt, trigger.attempts_left) == ("livelock", 4, 1)
        assert trigger.failure.traceback == FAIL_A

    def test_a_livelock_before_any_tested_diff_does_not_replan(self):
        events = run([ev(1, "harness", "diff_rendered", diff="d\n"),
                      ev(2, "harness", "livelock_detected", diff="d\n")])

        assert replay_trigger("r", events) is None

    def test_a_trigger_with_no_attempt_left_does_not_replan(self):
        events = run(attempt_tested(4, FAIL_A), attempt_tested(5, FAIL_A))

        assert replay_trigger("r", events) is None

    @pytest.mark.parametrize("kwargs", [{"passed": True}, {"summary_parsed": False}])
    def test_green_or_broken_suite_ends_the_replay(self, kwargs):
        events = run(attempt_tested(1, FAIL_A, **kwargs), attempt_tested(2, FAIL_A), attempt_tested(3, FAIL_A))

        assert replay_trigger("r", events) is None


class TestPrompt:
    def test_the_system_prompt_is_the_planners_plus_an_addendum(self):
        assert REPLAN_SYSTEM_PROMPT.startswith(SYSTEM_PROMPT)
        assert "second plan" in REPLAN_SYSTEM_PROMPT

    def test_the_message_is_the_planners_then_the_refuting_evidence(self, tmp_path):
        (tmp_path / "tokenize_rt.py").write_text("x = 1\n", encoding="utf-8")
        trigger = replay_trigger("r", run(attempt_tested(1, FAIL_A), attempt_tested(2, FAIL_A)))

        message = render_replan_message(
            repo_path=str(tmp_path), task_description="fix it", failure_input="original failure",
            plan=trigger.plan, tried_diff=trigger.tried_diff, failure=trigger.failure,
        )

        assert message.startswith("<task_description>\nfix it\n</task_description>")
        order = ["<failing_test_output>", "<repository>", "<previous_plan>",
                 "<diff_that_implemented_it>", "<test_failure_after_that_diff>"]
        assert [message.index(tag) for tag in order] == sorted(message.index(tag) for tag in order)
        assert "line[scol:ecol]" in message and "diff 2\n" in message and FAIL_A in message

    def test_rendering_is_deterministic(self, tmp_path):
        (tmp_path / "tokenize_rt.py").write_text("x = 1\n", encoding="utf-8")
        trigger = replay_trigger("r", run(attempt_tested(1, FAIL_A), attempt_tested(2, FAIL_A)))
        kwargs = dict(repo_path=str(tmp_path), task_description="d", failure_input="f",
                      plan=trigger.plan, tried_diff=trigger.tried_diff, failure=trigger.failure)

        assert render_replan_message(**kwargs) == render_replan_message(**kwargs)


def plan_with(*steps: str, summary: str = "s") -> Plan:
    return Plan(summary=summary, steps=list(steps), target_files=["tokenize_rt.py"], constraints=[])


class TestGrade:
    @pytest.mark.parametrize("step", [
        "Set tok_text = line[scol:ecol] when sline == eline.",
        "Compute tok_text by slicing the source line(s) from (sline, scol) to (eline, ecol).",
    ])
    def test_the_refuted_mechanism_fails(self, step):
        assert grade(plan_with(step)).label == "FAIL"

    @pytest.mark.parametrize("step", [
        "For FSTRING_MIDDLE, set tok_text = tok_text.replace('{', '{{').replace('}', '}}').",
        "Double every brace in tok_text for FSTRING_MIDDLE tokens.",
        "Advance ecol by tok_text.count('{') + tok_text.count('}') before computing last_col.",
        "Re-escape the braces that tokenize unescaped.",
    ])
    def test_a_brace_or_ecol_mechanism_passes(self, step):
        assert grade(plan_with(step)).label == "PASS"

    def test_double_braces_as_a_description_of_the_source_is_not_a_mechanism(self):
        # Recorded plan 9bc91c, verbatim in substance: it describes the bug, then slices.
        plan = plan_with(
            "tokenize may unescape double braces (`{{` and `}}` become `{` and `}`).",
            "Set tok_text = line[scol:ecol] for FSTRING_MIDDLE.",
        )

        assert grade(plan).label == "FAIL"

    def test_both_at_once_is_mixed_and_does_not_count(self):
        verdict = grade(plan_with("Slice line[scol:ecol], then double each brace."))

        assert verdict.label == "MIXED"

    def test_constraints_are_not_read(self):
        plan = Plan(summary="s", steps=["Slice line[scol:ecol]."], target_files=["tokenize_rt.py"],
                    constraints=["Braces must be doubled in the output."])

        assert grade(plan).label == "FAIL"


@pytest.mark.parametrize(("passes", "expected"), [
    (12, "BUILD D"), (6, "BUILD D"), (5, "JUDGMENT CALL: read the plans"),
    (3, "JUDGMENT CALL: read the plans"), (2, "DO NOT BUILD D"), (0, "DO NOT BUILD D"),
])
def test_gate_on_twelve(passes, expected):
    assert gate(passes, 12) == expected


# -- the run: truncation, the results file, resume ---------------------------

GOOD = plan_with("Double every brace in tok_text for FSTRING_MIDDLE tokens.")
FAIL_PLAN = plan_with("Set tok_text = line[scol:ecol] for FSTRING_MIDDLE.")
USAGE = {"input_tokens": 5000, "output_tokens": 900, "cache_read_input_tokens": 0, "thinking_tokens": 0}


class FakeClient:
    """Answers from a script: a Plan, or an exception to raise. Records every call."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(value=item, model="m", stop_reason="STOP", parse_attempts=1, usage=USAGE)


def truncated():
    error = LLMTruncatedError("ran out of budget", stage="response", raw_text="")
    error.call_payload = {"model": "m", "stop_reason": "MAX_TOKENS", "parse_attempts": 1,
                          "usage": {**USAGE, "output_tokens": 24000}}
    return error


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tokenize_rt.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(probe_replanner, "TASK_DIR", repo)
    triggers = [replay_trigger(name, run(attempt_tested(1, FAIL_A), attempt_tested(2, FAIL_A)))
                for name in ("run_a", "run_b")]
    log = EventLog("probe_test", log_dir=tmp_path / "logs")
    return triggers, log, tmp_path / "results.jsonl"


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestRun:
    def test_the_budget_is_the_implementers_and_thinking_is_the_planners(self, setup):
        triggers, log, results = setup
        client = FakeClient(GOOD)

        probe_replanner.run(triggers[:1], "d", client, log, 1, results)

        assert client.calls[0]["max_tokens"] == 24000
        assert client.calls[0]["thinking_level"] == "LOW"
        assert rows(results)[0]["max_tokens"] == 24000

    def test_a_truncated_sample_is_recorded_and_the_probe_continues(self, setup, capsys):
        triggers, log, results = setup
        client = FakeClient(GOOD, truncated(), GOOD, FAIL_PLAN)

        assert probe_replanner.run(triggers, "d", client, log, 2, results) == 0

        labels = [row["label"] for row in rows(results)]
        assert labels == ["PASS", "TRUNCATED", "PASS", "FAIL"]
        cut = rows(results)[1]
        assert cut["plan"] is None and cut["stop_reason"] == "MAX_TOKENS"
        assert cut["usage"]["output_tokens"] == 24000
        out = capsys.readouterr().out
        assert "PASS 2  MIXED 0  FAIL 1  TRUNCATED 1  (of 4)" in out
        assert "TRUNCATED counts as not PASS" in out

    def test_each_sample_is_written_before_the_next_call(self, setup):
        triggers, log, results = setup
        transport = LLMTransportError("503", status_code=503, retryable=True)
        client = FakeClient(GOOD, transport)

        with pytest.raises(LLMTransportError):
            probe_replanner.run(triggers, "d", client, log, 2, results)

        assert [(row["run_id"], row["sample"], row["label"]) for row in rows(results)] == [
            ("run_a", 1, "PASS")]

    def test_a_rerun_skips_finished_samples_truncated_ones_included(self, setup):
        triggers, log, results = setup
        probe_replanner.run(triggers[:1], "d", FakeClient(GOOD, truncated()), log, 2, results)

        client = FakeClient(FAIL_PLAN, GOOD)
        probe_replanner.run(triggers, "d", client, log, 2, results)

        assert len(client.calls) == 2  # only run_b's two samples
        assert [row["run_id"] for row in rows(results)] == ["run_a", "run_a", "run_b", "run_b"]

    def test_rows_asked_a_different_prompt_are_refused(self, setup):
        triggers, log, results = setup
        probe_replanner.run(triggers[:1], "first description", FakeClient(GOOD), log, 1, results)

        with pytest.raises(SystemExit, match="different prompt"):
            probe_replanner.run(triggers[:1], "second description", FakeClient(GOOD), log, 2, results)

    def test_a_torn_last_line_is_reasked(self, setup):
        triggers, log, results = setup
        probe_replanner.run(triggers[:1], "d", FakeClient(GOOD), log, 1, results)
        with results.open("a", encoding="utf-8") as handle:
            handle.write('{"run_id": "run_a", "sam')

        client = FakeClient(GOOD)
        probe_replanner.run(triggers[:1], "d", client, log, 2, results)

        assert len(client.calls) == 1

    def test_a_torn_line_in_the_middle_refuses(self, setup):
        _, _, results = setup
        results.write_text('{"broken\n{"run_id": "r", "sample": 1}\n', encoding="utf-8")

        with pytest.raises(SystemExit, match=":1 is not JSON"):
            probe_replanner.load_results(results)
