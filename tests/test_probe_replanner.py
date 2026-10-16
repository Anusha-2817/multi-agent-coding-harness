"""The Stage 0 probe's offline parts: trigger replay, prompt, grading, gate.

No call is made and no bench file is read; events are built here.
"""

from __future__ import annotations

import pytest

from harness.agents.planner import SYSTEM_PROMPT
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
