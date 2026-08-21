"""Tests for what the Planner and Implementer actually send.

Prompts are the only part of an LLM-backed agent that can be checked without a
model in the way, so they are checked hard here. Every rendering function is
pure -- state and files in, string out -- which is why they are module-level
functions rather than methods buried inside `_run`.

The two agents are also exercised end to end against a fake client, to prove the
rendered text is what reaches the wire and that the produced value is unwrapped
correctly. No API key, no network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harness.agents.implementer import (
    RESET_NOTICE,
    Implementer,
    ImplementerResponse,
    render_current_files,
    render_evidence,
    render_plan,
)
from harness.agents.implementer import render_user_message as implementer_message
from harness.agents.planner import Planner, render_repo
from harness.agents.planner import render_user_message as planner_message
from harness.events import EventLog
from harness.llm import LLMResult
from harness.state import FileEdit, Plan, ReviewerRejection, TaskState, TesterFailure

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
DISCOUNTS = "pricing/discounts.py"

PLAN = Plan(
    summary="tier_for uses a strict comparison where an inclusive one was meant.",
    steps=["Change `>` to `>=` in tier_for's loop condition."],
    target_files=[DISCOUNTS],
    constraints=["Do not change the TIERS table", "Do not edit any test file"],
)

REJECTION = ReviewerRejection(
    reason="edited 1 file(s) outside the plan's target_files: pricing/money.py",
    violated_constraints=["pricing/money.py"],
)

FAILURE = TesterFailure(
    failed_tests=["tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"],
    traceback="E       AssertionError: assert Decimal('125.00') == Decimal('118.75')",
    stdout_tail="1 failed, 19 passed in 0.23s",
)


@pytest.fixture
def repo(tmp_path) -> str:
    """A copy of the real fixture, so the renderers read real files."""
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return str(target)


@pytest.fixture
def event_log(tmp_path) -> EventLog:
    return EventLog("prompt_test", log_dir=tmp_path / "logs")


class FakeClient:
    """Records the call, returns a scripted value. Decides nothing."""

    def __init__(self, value):
        self.value = value
        self.calls: list[dict] = []

    def complete_structured(self, *, system, user, schema, max_tokens):
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "max_tokens": max_tokens}
        )
        return LLMResult(
            value=self.value,
            model="claude-opus-5",
            stop_reason="end_turn",
            parse_attempts=1,
            usage={"input_tokens": 10, "output_tokens": 5},
        )


def logged(event_log: EventLog) -> list[dict]:
    """Every event the log holds, oldest first."""
    return [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]


def state(repo_path: str, **fields) -> TaskState:
    return TaskState(
        task_id="fixture_repo_1_20260821T120000Z",
        repo_path=repo_path,
        task_description="Orders of exactly 10 units miss the bulk discount.",
        failure_input="rootdir: D:\\somewhere\\else\n1 failed, 19 passed\n",
        **fields,
    )


# -- the Planner's prompt ----------------------------------------------------


class TestPlannerRepoRendering:
    def test_source_files_are_inlined_with_their_contents(self, repo):
        rendered = render_repo(repo)

        assert f'<file path="{DISCOUNTS}">' in rendered
        assert "def tier_for(quantity: int) -> Tier:" in rendered

    def test_test_files_are_listed_but_not_inlined(self, repo):
        """The failing test's body is already in `failure_input`, and showing a
        model the assertions it must satisfy invites it to edit them instead."""
        rendered = render_repo(repo)

        assert "tests/test_orders.py" in rendered
        assert '<file path="tests/test_orders.py">' not in rendered
        assert "def test_an_order_at_the_bulk_threshold_is_discounted" not in rendered

    def test_non_python_files_are_listed_but_not_inlined(self, repo):
        rendered = render_repo(repo)

        assert "pytest.ini" in rendered
        assert '<file path="pytest.ini">' not in rendered

    def test_the_task_file_is_not_inlined(self, repo):
        """It carries `failure_input`, which is already in the prompt verbatim."""
        rendered = render_repo(repo)

        assert '<file path="task.json">' not in rendered

    def test_cache_directories_never_reach_the_prompt(self, repo):
        Path(repo, "pricing", "__pycache__").mkdir()
        Path(repo, "pricing", "__pycache__", "discounts.pyc").write_bytes(b"\x00")

        rendered = render_repo(repo)

        assert "__pycache__" not in rendered

    def test_rendering_is_byte_stable_across_calls(self, repo):
        """Sorted output is what lets the API cache the prompt prefix instead of
        re-reading it on every attempt."""
        assert render_repo(repo) == render_repo(repo)

    def test_an_oversized_file_is_listed_rather_than_inlined(self, repo):
        Path(repo, "pricing", "huge.py").write_text("# pad\n" * 20_000, encoding="utf-8")

        rendered = render_repo(repo)

        assert "pricing/huge.py" in rendered
        assert '<file path="pricing/huge.py">' not in rendered
        assert "characters, not shown" in rendered


class TestPlannerUserMessage:
    def test_the_failure_input_is_carried_verbatim(self, repo):
        """CLAUDE.md is explicit that the noise is part of the input, stale
        rootdir line included."""
        failure = "==== FAILURES ====\nrootdir: D:\\stale\\path\nE  assert 1 == 2\n"

        rendered = planner_message(
            repo_path=repo, task_description="something broke", failure_input=failure
        )

        assert failure in rendered

    def test_the_three_inputs_are_separately_tagged(self, repo):
        rendered = planner_message(
            repo_path=repo, task_description="something broke", failure_input="boom"
        )

        for tag in ("task_description", "failing_test_output", "repository"):
            assert f"<{tag}>" in rendered and f"</{tag}>" in rendered

    def test_the_task_description_appears(self, repo):
        rendered = planner_message(
            repo_path=repo, task_description="the unique description", failure_input="boom"
        )

        assert "the unique description" in rendered


class TestPlannerSystemPrompt:
    def test_it_explains_that_target_files_is_enforced(self):
        from harness.agents.planner import SYSTEM_PROMPT

        assert "target_files" in SYSTEM_PROMPT
        assert "rejects an edit to any path outside this list" in SYSTEM_PROMPT

    def test_it_warns_about_the_stale_rootdir_line(self):
        """The fixture's `failure_input` really does carry a rootdir pointing
        somewhere the code no longer is."""
        from harness.agents.planner import SYSTEM_PROMPT

        assert "rootdir" in SYSTEM_PROMPT

    def test_it_tells_the_planner_not_to_write_code(self):
        from harness.agents.planner import SYSTEM_PROMPT

        assert "do not write code" in SYSTEM_PROMPT.lower()


class TestThePlannerAgent:
    def test_it_sends_the_rendered_message_and_returns_the_plan(self, repo, event_log):
        client = FakeClient(PLAN)

        produced = Planner(event_log, client).run(state(repo))

        assert produced.plan == PLAN
        assert client.calls[0]["schema"] is Plan
        assert client.calls[0]["user"] == planner_message(
            repo_path=repo,
            task_description="Orders of exactly 10 units miss the bulk discount.",
            failure_input="rootdir: D:\\somewhere\\else\n1 failed, 19 passed\n",
        )

    def test_it_logs_the_call_without_relogging_the_plan(self, repo, event_log):
        client = FakeClient(PLAN)

        Planner(event_log, client).run(state(repo))

        events = logged(event_log)
        names = [event["event"] for event in events]
        assert names == ["llm_request", "llm_response", "agent_produced"]
        assert "plan" not in events[1]["payload"]
        assert events[2]["payload"]["plan"]["summary"] == PLAN.summary

    def test_planner_events_are_stamped_attempt_zero(self, repo, event_log):
        """The counter increments on the Implementer, so the Planner runs at 0."""
        Planner(event_log, FakeClient(PLAN)).run(state(repo))

        assert {event["attempt"] for event in logged(event_log)} == {0}


# -- the Implementer's prompt ------------------------------------------------


class TestImplementerPlanRendering:
    def test_every_plan_field_appears(self):
        rendered = render_plan(PLAN)

        assert PLAN.summary in rendered
        assert PLAN.steps[0] in rendered
        assert DISCOUNTS in rendered
        assert "Do not change the TIERS table" in rendered

    def test_steps_are_numbered(self):
        plan = PLAN.model_copy(update={"steps": ["first thing", "second thing"]})

        rendered = render_plan(plan)

        assert "1. first thing" in rendered
        assert "2. second thing" in rendered

    def test_empty_optional_sections_are_omitted_not_left_bare(self):
        plan = PLAN.model_copy(update={"steps": [], "constraints": []})

        rendered = render_plan(plan)

        assert "Steps:" not in rendered
        assert "Constraints:" not in rendered
        assert "Target files:" in rendered


class TestImplementerFileRendering:
    def test_only_the_target_files_are_shown(self, repo):
        """Narrower than the Planner's view: exactly the set it may edit."""
        rendered = render_current_files(repo, PLAN)

        assert f'<file path="{DISCOUNTS}">' in rendered
        assert "pricing/money.py" not in rendered

    def test_the_contents_are_the_real_file(self, repo):
        rendered = render_current_files(repo, PLAN)

        assert "if quantity > tier.min_quantity:" in rendered

    def test_a_file_the_plan_names_but_that_does_not_exist_renders_empty(self, repo):
        plan = PLAN.model_copy(update={"target_files": ["pricing/brand_new.py"]})

        rendered = render_current_files(repo, plan)

        assert '<file path="pricing/brand_new.py">\n</file>' in rendered

    def test_a_target_path_escaping_the_run_directory_raises(self, repo):
        """`normalize_path` deliberately does not resolve `..` away, so a plan
        can name an escaping path. Catching it on the read means it never
        reaches the write."""
        plan = PLAN.model_copy(update={"target_files": ["../../secrets.py"]})

        with pytest.raises(ValueError) as caught:
            render_current_files(repo, plan)

        assert "outside" in str(caught.value)


class TestEvidenceRendering:
    def test_a_rejection_says_nothing_was_applied(self):
        rendered = render_evidence(REJECTION)

        assert "never applied" in rendered
        assert "no \ntests were run" in rendered or "no tests were run" in rendered

    def test_a_rejection_carries_the_reason_and_the_violated_constraints(self):
        rendered = render_evidence(REJECTION)

        assert REJECTION.reason in rendered
        assert "- pricing/money.py" in rendered

    def test_a_rejection_warns_that_repeating_it_halts_the_run(self):
        """Livelock is a real halt condition and the cheapest wrong response to
        a rejection is to send the same thing again."""
        assert "halts the run" in render_evidence(REJECTION)

    def test_an_empty_constraint_list_omits_the_heading(self):
        """The no-edits branch writes `[]`; a bare heading reads as truncation."""
        empty = ReviewerRejection(reason="the Implementer produced no edits", violated_constraints=[])

        rendered = render_evidence(empty)

        assert "Constraints it violated" not in rendered
        assert "the Implementer produced no edits" in rendered

    def test_a_test_failure_says_the_change_was_applied_and_ran(self):
        rendered = render_evidence(FAILURE)

        assert "was applied" in rendered
        assert "test suite ran" in rendered

    def test_a_test_failure_lists_the_failing_nodeids(self):
        rendered = render_evidence(FAILURE)

        assert f"- {FAILURE.failed_tests[0]}" in rendered

    def test_a_test_failure_carries_the_traceback_and_the_stdout_tail(self):
        rendered = render_evidence(FAILURE)

        assert FAILURE.traceback in rendered
        assert FAILURE.stdout_tail in rendered

    def test_the_two_kinds_use_different_tags(self):
        """A generic 'that did not work' would throw away the whole reason there
        are two evidence types rather than an `error: str`."""
        assert "<previous_attempt_rejected>" in render_evidence(REJECTION)
        assert "<previous_attempt_failed_tests>" in render_evidence(FAILURE)
        assert "<previous_attempt_rejected>" not in render_evidence(FAILURE)
        assert "<previous_attempt_failed_tests>" not in render_evidence(REJECTION)

    def test_a_rejection_carries_no_test_shaped_language(self):
        """It describes a world where nothing ran, so nothing in it should read
        as though something did."""
        rendered = render_evidence(REJECTION).lower()

        assert "traceback" not in rendered
        assert "suite" not in rendered
        assert "failed:" not in rendered

    def test_a_test_failure_carries_no_review_shaped_language(self):
        rendered = render_evidence(FAILURE).lower()

        assert "rejected" not in rendered
        assert "review" not in rendered

    @pytest.mark.parametrize("evidence", [REJECTION, FAILURE], ids=["rejection", "test_failure"])
    def test_both_kinds_carry_the_reset_warning(self, evidence):
        """The single most misreadable thing about a retry: the files in the
        prompt are the originals, not the model's previous output."""
        rendered = render_evidence(evidence)

        assert RESET_NOTICE in rendered
        assert "originals, not your edited versions" in rendered

    def test_an_unknown_evidence_kind_raises(self):
        with pytest.raises(TypeError):
            render_evidence(object())


class TestImplementerUserMessage:
    def test_a_first_attempt_carries_no_evidence_section(self, repo):
        rendered = implementer_message(repo_path=repo, plan=PLAN, evidence=None)

        assert "previous_attempt" not in rendered
        assert RESET_NOTICE not in rendered

    def test_evidence_comes_last_so_the_prefix_is_stable(self, repo):
        """Plan and files are byte-identical on every attempt; only the evidence
        changes. Stable prefix first is what makes the cache useful."""
        first = implementer_message(repo_path=repo, plan=PLAN, evidence=None)
        retry = implementer_message(repo_path=repo, plan=PLAN, evidence=FAILURE)

        assert retry.startswith(first)

    def test_the_prefix_is_identical_across_both_evidence_kinds(self, repo):
        after_rejection = implementer_message(repo_path=repo, plan=PLAN, evidence=REJECTION)
        after_failure = implementer_message(repo_path=repo, plan=PLAN, evidence=FAILURE)
        shared = implementer_message(repo_path=repo, plan=PLAN, evidence=None)

        assert after_rejection.startswith(shared)
        assert after_failure.startswith(shared)

    def test_the_sections_appear_in_order(self, repo):
        rendered = implementer_message(repo_path=repo, plan=PLAN, evidence=REJECTION)

        assert (
            rendered.index("<plan>")
            < rendered.index("<current_files>")
            < rendered.index("<previous_attempt_rejected>")
        )


class TestImplementerSystemPrompt:
    def test_it_demands_complete_file_contents(self):
        from harness.agents.implementer import SYSTEM_PROMPT

        assert "complete new contents" in SYSTEM_PROMPT
        assert "Not a diff" in SYSTEM_PROMPT

    def test_it_forbids_elisions(self):
        """A model's instinct on 'fix this file' is to show the changed lines,
        and an elision is read as a file that lost everything left out."""
        from harness.agents.implementer import SYSTEM_PROMPT

        assert "rest unchanged" in SYSTEM_PROMPT

    def test_it_says_out_of_scope_edits_cost_an_attempt(self):
        from harness.agents.implementer import SYSTEM_PROMPT

        assert "target_files" in SYSTEM_PROMPT
        assert "rejected without review" in SYSTEM_PROMPT

    def test_it_asks_for_line_endings_to_be_preserved(self):
        from harness.agents.implementer import SYSTEM_PROMPT

        assert "line\nendings" in SYSTEM_PROMPT or "line endings" in SYSTEM_PROMPT


class TestTheImplementerAgent:
    def test_it_unwraps_the_envelope_into_a_bare_edit_list(self, repo, event_log):
        """`ImplementerResponse` exists only because structured outputs needs an
        object at the schema root. `_run` still produces `list[FileEdit]`."""
        edits = [FileEdit(path=DISCOUNTS, new_content="fixed\n")]
        client = FakeClient(ImplementerResponse(edits=edits))

        produced = Implementer(event_log, client).run(state(repo, plan=PLAN))

        assert produced.edits == edits
        assert client.calls[0]["schema"] is ImplementerResponse

    def test_it_sends_the_rendered_message(self, repo, event_log):
        client = FakeClient(ImplementerResponse(edits=[FileEdit(path=DISCOUNTS, new_content="x\n")]))

        Implementer(event_log, client).run(state(repo, plan=PLAN, evidence=FAILURE))

        assert client.calls[0]["user"] == implementer_message(
            repo_path=repo, plan=PLAN, evidence=FAILURE
        )

    def test_the_log_records_which_kind_of_evidence_the_retry_was_told(self, repo, event_log):
        client = FakeClient(ImplementerResponse(edits=[FileEdit(path=DISCOUNTS, new_content="x\n")]))

        Implementer(event_log, client).run(state(repo, plan=PLAN, evidence=REJECTION))

        assert logged(event_log)[0]["payload"]["evidence_kind"] == "reviewer_rejection"

    def test_a_first_attempt_logs_no_evidence_kind(self, repo, event_log):
        client = FakeClient(ImplementerResponse(edits=[FileEdit(path=DISCOUNTS, new_content="x\n")]))

        Implementer(event_log, client).run(state(repo, plan=PLAN))

        assert logged(event_log)[0]["payload"]["evidence_kind"] is None
