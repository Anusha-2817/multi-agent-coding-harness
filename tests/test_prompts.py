"""Tests for what the Planner, Implementer, and Reviewer actually send.

Prompts are the only part of an LLM-backed agent that can be checked without a
model in the way, so they are checked hard here. Every rendering function is
pure -- state and files in, string out -- which is why they are module-level
functions rather than methods buried inside `_run`.

All three agents are also exercised end to end against a fake client, to prove
the rendered text is what reaches the wire and that the produced value is
unwrapped correctly. No API key, no network.

What these cannot check is judgment: whether the Reviewer's prompt actually
stops it rubber-stamping is not a property of the rendered text. That needs a
diff which stays inside `target_files` and is unfaithful to the plan, which is
Phase 5's fixture.
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
from harness.agents.reviewer import NONE_STATED, Reviewer
from harness.agents.reviewer import render_plan as reviewer_render_plan
from harness.agents.reviewer import render_user_message as reviewer_message
from harness.events import EventLog
from harness.llm import DEFAULT_MODEL, LLMResult
from harness.state import (
    FileEdit,
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    TaskState,
    TesterFailure,
)

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

# A real unified diff, shaped like what `render_diff` emits: `a/`-`b/` labels, no
# timestamps. The Reviewer's user message must carry this untouched, and the
# `---`/`+++`/`@@` in it are why the sections are tagged rather than delimited.
DIFF = """\
--- a/pricing/discounts.py
+++ b/pricing/discounts.py
@@ -18,7 +18,7 @@
     for tier in TIERS:
-        if quantity > tier.minimum:
+        if quantity >= tier.minimum:
             return tier
     return BASE_TIER
"""


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
            # Bound to the real constant rather than a literal. This value is
            # inert -- no assertion reads it -- and the literal that used to be
            # here outlived the provider it named by a whole phase.
            model=DEFAULT_MODEL,
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


# -- the Reviewer's prompt ---------------------------------------------------


class TestReviewerPlanRendering:
    """A second `render_plan`, deliberately. See the docstring on the function:
    the Reviewer cites steps by number and needs to know when a section is empty
    rather than dropped."""

    def test_every_plan_field_appears(self):
        rendered = reviewer_render_plan(PLAN)

        assert PLAN.summary in rendered
        assert PLAN.steps[0] in rendered
        assert DISCOUNTS in rendered
        assert "Do not change the TIERS table" in rendered

    def test_steps_are_numbered_because_the_reason_cites_them_by_number(self):
        plan = PLAN.model_copy(update={"steps": ["first thing", "second thing"]})

        rendered = reviewer_render_plan(plan)

        assert "1. first thing" in rendered
        assert "2. second thing" in rendered

    def test_an_empty_section_says_none_stated_rather_than_being_dropped(self):
        """The Implementer's renderer omits an empty heading, correctly. Here the
        emptiness is information: the agent is told to check each entry in turn,
        so "there are none" and "the section was cut" must not look alike."""
        plan = PLAN.model_copy(update={"steps": [], "constraints": []})

        rendered = reviewer_render_plan(plan)

        assert f"Steps:\n{NONE_STATED}" in rendered
        assert f"Constraints:\n{NONE_STATED}" in rendered

    def test_it_differs_from_the_implementers_rendering_on_an_empty_plan(self):
        """The duplication is load-bearing, not an oversight. If these two ever
        converge, one of them has lost a property it was given on purpose."""
        plan = PLAN.model_copy(update={"steps": [], "constraints": []})

        assert reviewer_render_plan(plan) != render_plan(plan)


class TestReviewerUserMessage:
    def test_the_diff_is_carried_verbatim(self):
        rendered = reviewer_message(plan=PLAN, diff=DIFF)

        assert DIFF in rendered

    def test_the_two_inputs_are_separately_tagged(self):
        rendered = reviewer_message(plan=PLAN, diff=DIFF)

        assert "<plan>" in rendered and "</plan>" in rendered
        assert "<diff>" in rendered and "</diff>" in rendered

    def test_the_plan_comes_before_the_diff(self):
        rendered = reviewer_message(plan=PLAN, diff=DIFF)

        assert rendered.index("<plan>") < rendered.index("<diff>")

    def test_no_repository_contents_reach_the_prompt(self):
        """The contract is `plan` and `diff`. Widening it is the revisit trigger
        Phase 5 is supposed to supply evidence for, not something that should
        creep in through the renderer."""
        rendered = reviewer_message(plan=PLAN, diff=DIFF)

        assert "<file " not in rendered
        assert "<current_files>" not in rendered

    def test_a_diff_full_of_markdown_lookalikes_survives(self):
        """`---`, `+++` and `@@` are exactly what a lightweight delimiter would
        collide with, which is why the sections are tagged."""
        rendered = reviewer_message(plan=PLAN, diff=DIFF)

        assert "--- a/pricing/discounts.py" in rendered
        assert "+++ b/pricing/discounts.py" in rendered


class TestReviewerSystemPrompt:
    def test_it_says_the_reviewer_will_not_get_test_results(self):
        """Invariant 2 is enforced by omission in `base.py`. Saying so in the
        prompt is what stops the model asking for them or assuming them."""
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "you do not have the test results" in SYSTEM_PROMPT.lower()

    def test_it_names_the_hardcoded_fix_as_the_case_blind_review_exists_for(self):
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "hardcoding the values the test happens to use" in SYSTEM_PROMPT
        assert "special-cases" in SYSTEM_PROMPT

    def test_it_says_the_mechanical_checks_have_already_run(self):
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "Do not re-check those" in SYSTEM_PROMPT
        assert "target_files" in SYSTEM_PROMPT

    def test_it_asks_for_attribution_in_both_directions(self):
        """Hunk-to-step catches overreach; step-to-hunk catches an unfinished
        change. Only one of the two is the obvious one."""
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "name the numbered step it carries out" in SYSTEM_PROMPT
        assert "A step with no hunk is" in SYSTEM_PROMPT

    def test_it_puts_the_reason_before_the_verdict(self):
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "Write your reason first, then decide." in SYSTEM_PROMPT

    def test_it_forbids_reviewing_the_plan_and_says_what_that_would_cost(self):
        """v1 never replans, so a "this plan is wrong" rejection routes back to
        an Implementer that can only implement the same plan again.

        Asserted against whitespace-collapsed text: this sentence spans a line
        break, and a prompt reflowed for readability should not fail a test
        about what the prompt says."""
        from harness.agents.reviewer import SYSTEM_PROMPT

        unwrapped = " ".join(SYSTEM_PROMPT.split())
        assert "You do not review the plan." in unwrapped
        assert "Nothing in this system replans" in unwrapped

    def test_it_names_the_cost_of_a_wrong_rejection_as_well_as_a_wrong_approval(self):
        """A prompt that only names one of the two errors optimises for that one.
        This is the half that keeps it from rejecting good diffs."""
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "Approving a change that does not match the plan puts it on disk" in SYSTEM_PROMPT
        assert "costs one of five attempts" in SYSTEM_PROMPT
        assert 'neither "be strict" nor "be lenient"' in SYSTEM_PROMPT

    def test_it_lists_things_that_are_not_grounds_for_rejection(self):
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "These are not grounds for rejection" in SYSTEM_PROMPT
        assert "Style, naming, formatting" in SYSTEM_PROMPT
        assert "Missing tests" in SYSTEM_PROMPT

    def test_it_requires_a_rejection_the_implementer_can_act_on(self):
        """The Implementer retries blind to its own diff, so the reason is the
        only channel. It doubles as a filter on vague rejections."""
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "the reason is the only thing the implementer will be shown" in SYSTEM_PROMPT
        assert "does not get to see the diff it wrote" in SYSTEM_PROMPT

    def test_it_confines_violated_constraints_to_the_plans_own_wording(self):
        from harness.agents.reviewer import SYSTEM_PROMPT

        assert "word for word as the plan wrote" in SYSTEM_PROMPT
        assert "Put nothing else in that field" in SYSTEM_PROMPT


class TestTheReviewerAgent:
    def test_it_sends_the_rendered_message_and_returns_the_verdict(self, repo, event_log):
        verdict = ReviewVerdict(
            reason="hunk 1 carries out step 1; no unaccounted hunks.",
            approved=True,
            violated_constraints=[],
        )
        client = FakeClient(verdict)

        produced = Reviewer(event_log, client).run(state(repo, plan=PLAN, diff=DIFF))

        assert produced.review == verdict
        assert client.calls[0]["user"] == reviewer_message(plan=PLAN, diff=DIFF)

    def test_the_verdict_needs_no_envelope(self, repo, event_log):
        """Unlike `list[FileEdit]`, `ReviewVerdict` is already an object at the
        schema root, which is what `response_json_schema` accepts."""
        client = FakeClient(
            ReviewVerdict(reason="fine", approved=True, violated_constraints=[])
        )

        Reviewer(event_log, client).run(state(repo, plan=PLAN, diff=DIFF))

        assert client.calls[0]["schema"] is ReviewVerdict

    def test_it_logs_the_call_without_relogging_the_verdict(self, repo, event_log):
        client = FakeClient(
            ReviewVerdict(reason="too broad", approved=False, violated_constraints=["c1"])
        )

        Reviewer(event_log, client).run(state(repo, plan=PLAN, diff=DIFF))

        events = logged(event_log)
        assert [event["event"] for event in events] == [
            "llm_request",
            "llm_response",
            "agent_produced",
        ]
        assert "review" not in events[1]["payload"]
        assert events[2]["payload"]["review"]["approved"] is False

    def test_the_schema_puts_reason_before_approved(self):
        """Property order is emission order, and reason-first is the cheapest
        guard there is against a verdict written before the reasoning."""
        properties = list(ReviewVerdict.model_json_schema()["properties"])

        assert properties.index("reason") < properties.index("approved")
