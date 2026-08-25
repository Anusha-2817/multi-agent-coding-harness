"""Tests for the entrypoint: the task file, the gate, the report, and `--stub`.

Mostly boundary code -- reading a hand-authored JSON file, and a human at a
terminal -- because the loop `main` assembles already has 81 tests of its own in
`test_loop.py`.

The exception is `--stub`, which is driven end to end through `main` with no
`GEMINI_API_KEY` in the environment. That is the whole claim the flag makes: no
key, no requests, and every other part of the harness doing its real job. It is
also what turns CLAUDE.md's "no test needs an API key" from a property of the
fakes into a property of the entrypoint.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fixture_edits import broken_edits, failing_edits, fixing_edits
from test_loop import run_scenario

from cli import (
    attempt_ledger,
    main,
    read_task_file,
    rendered_attempt,
    report,
    stub_agents,
    stub_targets,
    terminal_approval,
)
from harness.events import EventLog
from harness.loop import MAX_ATTEMPTS
from harness.state import (
    Plan,
    ReviewerRejection,
    ReviewVerdict,
    Status,
    TaskState,
    TestResult,
    TesterFailure,
)
from harness.workspace import read_repo_file

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

#: A plan shaped like the one the real Planner writes for fixture_repo_1.
#: The report only ever reads `summary` and `target_files` off it.
PLAN = Plan(
    summary="Use an inclusive lower bound at the tier boundary.",
    steps=["Change > to >= in tier_for"],
    target_files=["pricing/discounts.py"],
    constraints=["Do not change the tier table"],
)


def task_file(tmp_path: Path, payload) -> Path:
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestReadingTheTaskFile:
    def test_a_well_formed_file_loads(self, tmp_path):
        path = task_file(tmp_path, {"task_description": "d", "failure_input": "f"})

        assert read_task_file(path) == {"task_description": "d", "failure_input": "f"}

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(SystemExit) as caught:
            read_task_file(tmp_path / "absent.json")

        assert "absent.json" in str(caught.value)

    def test_malformed_json_names_the_file(self, tmp_path):
        path = tmp_path / "task.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(SystemExit) as caught:
            read_task_file(path)

        assert "not valid JSON" in str(caught.value)

    def test_a_missing_field_is_caught_at_the_boundary(self, tmp_path):
        """Rather than three steps later as a contract violation inside an
        agent, where nothing points back at the file."""
        path = task_file(tmp_path, {"task_description": "d"})

        with pytest.raises(SystemExit) as caught:
            read_task_file(path)

        assert "failure_input" in str(caught.value)

    def test_an_extra_field_is_rejected(self, tmp_path):
        """`task.json` holds exactly two hand-authored fields. A `task_id` in
        there is someone reinventing a value the harness generates."""
        path = task_file(
            tmp_path, {"task_description": "d", "failure_input": "f", "task_id": "mine"}
        )

        with pytest.raises(SystemExit) as caught:
            read_task_file(path)

        assert "task_id" in str(caught.value)

    def test_a_json_document_that_is_not_an_object_is_rejected(self, tmp_path):
        path = task_file(tmp_path, ["d", "f"])

        with pytest.raises(SystemExit):
            read_task_file(path)


class TestTheTerminalGate:
    @pytest.mark.parametrize("answer", ["y", "Y", "yes", "  yes  "])
    def test_yes_approves(self, monkeypatch, capsys, answer):
        monkeypatch.setattr("builtins.input", lambda _: answer)

        assert terminal_approval("--- a\n+++ b\n") is True

    @pytest.mark.parametrize("answer", ["n", "N", "no", "  NO "])
    def test_no_refuses(self, monkeypatch, capsys, answer):
        monkeypatch.setattr("builtins.input", lambda _: answer)

        assert terminal_approval("--- a\n+++ b\n") is False

    def test_the_diff_is_printed_before_the_question(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _: "n")

        terminal_approval("--- a/pricing/discounts.py\n+++ b/pricing/discounts.py\n")

        assert "pricing/discounts.py" in capsys.readouterr().out

    def test_an_unrecognised_answer_asks_again(self, monkeypatch, capsys):
        answers = iter(["maybe", "later", "y"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

        assert terminal_approval("diff") is True
        assert "please answer y or n" in capsys.readouterr().out

    def test_unreadable_stdin_refuses(self, monkeypatch, capsys):
        """A gate whose failure mode is "approve" is not a gate. Piped input, a
        CI job, or `< /dev/null` must not apply a diff."""

        def eof(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)

        assert terminal_approval("diff") is False
        assert "refusal" in capsys.readouterr().out


class TestTheReport:
    """The escalation output, Phase 4.

    Two kinds of test here, and the split is deliberate. The *renderings* are
    unit tests against hand-built terminal states -- cheap, and they can reach
    states a `--stub` run cannot produce. The two renderings a stub run *can*
    reach are also driven end to end in `TestAStubRunEndToEnd`, because those are
    the ones a human will actually meet.
    """

    def state(self, **fields) -> TaskState:
        return TaskState(
            task_id="fixture_repo_1_20260821T120000Z",
            repo_path="runs/fixture_repo_1_20260821T120000Z",
            task_description="d",
            failure_input="f",
            **fields,
        )

    def log(self, tmp_path: Path, *events: dict) -> Path:
        """A log file holding exactly `events`. The report's only input besides state."""
        path = tmp_path / "run.jsonl"
        path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )
        return path

    def event(self, event: str, attempt: int = 1, **payload) -> dict:
        return {
            "ts": "2026-08-21T12:00:00+00:00",
            "task_id": "fixture_repo_1_20260821T120000Z",
            "attempt": attempt,
            "agent": "harness",
            "event": event,
            "payload": payload,
        }

    # -- the common header ---------------------------------------------------

    def test_it_names_the_status_the_attempts_and_where_to_look(self, capsys, tmp_path):
        log = self.log(tmp_path)
        report(self.state(status=Status.SUCCEEDED, attempt_count=2), log)

        out = capsys.readouterr().out
        assert "succeeded" in out
        assert "2 of 5" in out
        assert str(log) in out

    def test_the_halt_reason_comes_from_the_log(self, capsys, tmp_path):
        """`_halt` writes it to `run_finished` and nowhere else -- there is no
        `reason` field on `TaskState`, which is why the report reads the log."""
        log = self.log(
            tmp_path,
            self.event("run_finished", status="succeeded", reason="the suite is green."),
        )

        report(self.state(status=Status.SUCCEEDED, attempt_count=1), log)

        assert "reason        the suite is green." in capsys.readouterr().out

    def test_a_missing_log_costs_detail_not_the_report(self, capsys, tmp_path):
        """The mirror of `EventLog.append`'s own rule. The run is over and its
        outcome is already known; a reporting problem must not become a traceback."""
        report(self.state(status=Status.SUCCEEDED, attempt_count=1), tmp_path / "nope.jsonl")

        out = capsys.readouterr().out
        assert "succeeded" in out
        assert "reason" not in out

    def test_a_corrupt_line_is_skipped(self, capsys, tmp_path):
        path = tmp_path / "run.jsonl"
        path.write_text(
            "{not json\n"
            + json.dumps(self.event("run_finished", reason="the suite is green."))
            + "\n",
            encoding="utf-8",
        )

        report(self.state(status=Status.SUCCEEDED, attempt_count=1), path)

        assert "the suite is green." in capsys.readouterr().out

    # -- succeeded -----------------------------------------------------------

    def test_a_success_on_retry_names_what_it_recovered_from(self, capsys, tmp_path):
        """CLAUDE.md: a run that failed once and then succeeded ends with the
        failure still in `evidence`, and that is the interesting part. Every
        attempt that routes back writes evidence, so the surviving one is always
        the immediately preceding attempt's -- hence `attempt_count - 1`."""
        recovered = self.state(
            status=Status.SUCCEEDED,
            attempt_count=2,
            evidence=TesterFailure(failed_tests=["t::x"], traceback="boom", stdout_tail=""),
        )

        report(recovered, self.log(tmp_path))

        assert "Recovered from a tester_failure on attempt 1." in capsys.readouterr().out

    def test_a_clean_run_says_nothing_about_recovery(self, capsys, tmp_path):
        report(self.state(status=Status.SUCCEEDED, attempt_count=1), self.log(tmp_path))

        assert "Recovered" not in capsys.readouterr().out

    # -- escalated_retry_limit -----------------------------------------------

    def test_the_ledger_names_what_each_attempt_died_of(self, capsys, tmp_path):
        """The one thing state cannot answer: `evidence` is the single most
        recent failure, never a history. Five rows, four different causes."""
        log = self.log(
            tmp_path,
            self.event("test_failed", 1, failed_tests=["tests/test_a.py::x"], exit_code=1),
            self.event("scope_check_failed", 2, paths=["pricing/money.py"], target_files=[]),
            self.event("review_rejected", 3, reason="hunk 2 is unaccounted for", violated_constraints=[]),
            self.event("no_edits_produced", 4),
            self.event("test_failed", 5, failed_tests=["tests/test_a.py::x"], exit_code=1),
            self.event("run_finished", 5, reason="5 attempts produced no passing diff."),
        )

        report(self.state(status=Status.ESCALATED_RETRY_LIMIT, attempt_count=5), log)

        out = capsys.readouterr().out
        assert "Attempt ledger" in out
        assert "1  test_failed" in out
        assert "2  scope_check_failed" in out
        assert "pricing/money.py" in out
        assert "3  review_rejected" in out
        assert "hunk 2 is unaccounted for" in out
        assert "4  no_edits_produced" in out
        assert "5  test_failed" in out

    def test_a_test_failure_row_counts_the_rest(self, capsys, tmp_path):
        log = self.log(
            tmp_path,
            self.event("test_failed", 1, failed_tests=["a::x", "b::y", "c::z"], exit_code=1),
        )

        report(self.state(status=Status.ESCALATED_RETRY_LIMIT, attempt_count=1), log)

        assert "a::x (+2 more)" in capsys.readouterr().out

    def test_a_last_row_that_reached_the_tester_says_the_edits_are_on_disk(
        self, capsys, tmp_path
    ):
        """The reset is at the *top* of an attempt, so the last attempt's work
        survives the halt if it got as far as apply."""
        log = self.log(
            tmp_path, self.event("test_failed", 5, failed_tests=["a::x"], exit_code=1)
        )

        report(self.state(status=Status.ESCALATED_RETRY_LIMIT, attempt_count=5), log)

        out = capsys.readouterr().out
        assert "attempt 5's edits are applied" in out

    def test_a_last_row_caught_upstream_says_the_baseline_is_untouched(
        self, capsys, tmp_path
    ):
        """The other half of the same fact, and the reason it is worth printing:
        the two states of the run directory are indistinguishable by eye."""
        log = self.log(
            tmp_path, self.event("scope_check_failed", 5, paths=["x.py"], target_files=[])
        )

        report(self.state(status=Status.ESCALATED_RETRY_LIMIT, attempt_count=5), log)

        out = capsys.readouterr().out
        assert "On disk: nothing" in out
        assert "untouched baseline" in out

    def test_the_retry_limit_prints_the_plan_and_the_last_failure_in_full(
        self, capsys, tmp_path
    ):
        state = self.state(
            status=Status.ESCALATED_RETRY_LIMIT,
            attempt_count=5,
            plan=PLAN,
            evidence=TesterFailure(
                failed_tests=["tests/test_a.py::x"],
                traceback="E  assert 1 == 2",
                stdout_tail="",
            ),
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "Plan under suspicion" in out
        assert PLAN.summary in out
        assert "pricing/discounts.py" in out
        assert "Last failure (tester_failure)" in out
        assert "E  assert 1 == 2" in out

    def test_a_rejection_as_the_last_failure_lists_its_constraints(self, capsys, tmp_path):
        state = self.state(
            status=Status.ESCALATED_RETRY_LIMIT,
            attempt_count=5,
            evidence=ReviewerRejection(
                reason="the change rewrites a function no step mentions",
                violated_constraints=["Do not change the tier table"],
            ),
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "Last failure (reviewer_rejection)" in out
        assert "no step mentions" in out
        assert "Do not change the tier table" in out

    # -- escalated_livelock --------------------------------------------------

    def test_livelock_names_the_attempt_the_diff_came_from(self, capsys, tmp_path):
        """From `diff_rendered`, never from `previous_diffs.index`. See
        `TestTheLivelockAttemptNumber` for why the difference is real."""
        log = self.log(
            tmp_path,
            self.event("diff_rendered", 2, diff="--- a/x\n+++ b/x\n"),
            self.event("livelock_detected", 3, diff="--- a/x\n+++ b/x\n", previous_diffs=1),
        )
        state = self.state(
            status=Status.ESCALATED_LIVELOCK,
            attempt_count=3,
            diff="--- a/x\n+++ b/x\n",
            plan=PLAN,
        )

        report(state, log)

        out = capsys.readouterr().out
        assert "Attempt 3 rendered a diff byte-identical to attempt 2's." in out
        assert "The repeated diff" in out
        assert "Plan under suspicion" in out
        assert "v1 does not replan" in out

    def test_livelock_survives_a_diff_the_log_does_not_hold(self, capsys, tmp_path):
        """Degrades to no attribution line rather than raising."""
        state = self.state(
            status=Status.ESCALATED_LIVELOCK, attempt_count=2, diff="--- a/x\n"
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "byte-identical" not in out
        assert "The repeated diff" in out

    # -- escalated_broken_suite ----------------------------------------------

    def test_a_broken_suite_shows_the_exit_code_and_the_traceback(self, capsys, tmp_path):
        state = self.state(
            status=Status.ESCALATED_BROKEN_SUITE,
            attempt_count=1,
            diff="--- a/x\n+++ b/x\n",
            test_result=TestResult(
                passed=False,
                failed_tests=[],
                summary_parsed=False,
                traceback="E   IndentationError: unexpected indent",
                stdout="",
                exit_code=2,
            ),
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "exit code  2" in out
        assert "IndentationError" in out
        assert "The diff that broke it" in out
        assert "these edits are applied" in out

    def test_a_broken_suite_never_prints_evidence(self, capsys, tmp_path):
        """The loop writes none on this path -- `test_no_evidence_is_written`
        asserts that. A reporter printing `state.evidence` unconditionally would
        show an *earlier* attempt's failure as though it caused this halt. The
        state below carries a stale one precisely to prove it stays hidden."""
        state = self.state(
            status=Status.ESCALATED_BROKEN_SUITE,
            attempt_count=2,
            evidence=TesterFailure(
                failed_tests=["stale::from_attempt_one"], traceback="stale", stdout_tail=""
            ),
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "stale::from_attempt_one" not in out
        assert "Last failure" not in out

    # -- aborted_by_human ----------------------------------------------------

    def test_a_refusal_shows_the_verdict_and_the_diff_it_refused(self, capsys, tmp_path):
        """Printing `state.review` here closes the "widen `approve()` to take the
        verdict" question without widening anything: the gate still receives the
        diff alone, and the report reads the verdict off state afterwards."""
        state = self.state(
            status=Status.ABORTED_BY_HUMAN,
            attempt_count=1,
            diff="--- a/x\n+++ b/x\n",
            review=ReviewVerdict(
                reason="step 1 accounts for the only hunk",
                approved=True,
                violated_constraints=[],
            ),
        )

        report(state, self.log(tmp_path))

        out = capsys.readouterr().out
        assert "What the Reviewer had said about it" in out
        assert "step 1 accounts for the only hunk" in out
        assert "The diff you refused" in out
        assert "On disk: nothing" in out

    # -- the block renderer --------------------------------------------------

    def test_a_diffs_blank_context_lines_keep_their_column(self, capsys, tmp_path):
        """`textwrap.indent` skips whitespace-only lines by default, and a diff's
        blank context line is a single space. Left at the default it lands two
        columns left of everything around it and puts a kink in the one artifact
        the human is being asked to judge."""
        state = self.state(
            status=Status.ABORTED_BY_HUMAN,
            attempt_count=1,
            diff="--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n \n b\n",
        )

        report(state, self.log(tmp_path))

        lines = capsys.readouterr().out.splitlines()
        assert "   a" in lines
        assert "   " in lines
        assert "   b" in lines


class TestTheLivelockAttemptNumber:
    """Why the livelock rendering reads `diff_rendered` rather than doing the
    obvious arithmetic on `previous_diffs`.

    Confirmed against a real three-attempt run before the rendering was written,
    and pinned here so the shortcut cannot be reintroduced as a simplification.
    """

    def events(self, *pairs: tuple[str, int, str]) -> list[dict]:
        return [
            {"attempt": attempt, "event": event, "payload": {"diff": diff}}
            for event, attempt, diff in pairs
        ]

    def test_it_reads_the_attempt_off_the_event(self):
        events = self.events(("diff_rendered", 2, "D"), ("diff_rendered", 3, "D"))

        assert rendered_attempt(events, "D") == 2

    def test_previous_diffs_index_would_have_been_wrong(self):
        """An attempt caught at the scope check renders nothing, so it adds no
        entry to `previous_diffs`. With one such attempt first, the index of the
        duplicated diff sits one below the attempt that produced it."""
        previous_diffs = ["D"]  # attempt 1 was caught upstream of the render

        assert previous_diffs.index("D") + 1 == 1  # the tempting arithmetic
        assert rendered_attempt(self.events(("diff_rendered", 2, "D")), "D") == 2

    def test_an_unknown_diff_is_none_rather_than_an_error(self):
        assert rendered_attempt(self.events(("diff_rendered", 1, "D")), "other") is None


class TestTheAttemptLedger:
    def event(self, event: str, attempt: int, **payload) -> dict:
        return {"attempt": attempt, "event": event, "payload": payload}

    def test_only_the_four_branch_events_become_rows(self):
        """One row per attempt that routed back. `diff_rendered`, `baseline_reset`
        and the agents' own events are not outcomes and must not appear."""
        events = [
            self.event("baseline_reset", 1, repo_path="x"),
            self.event("diff_rendered", 1, diff="d"),
            self.event("test_failed", 1, failed_tests=["a::x"], exit_code=1),
            self.event("run_finished", 1, status="escalated_retry_limit"),
        ]

        assert attempt_ledger(events) == [(1, "test_failed", "a::x")]

    def test_a_test_failure_with_no_named_test_falls_back_to_the_exit_code(self):
        """Reachable: `failed_tests` is empty whenever pytest's summary section
        held no nodeid, and the row still has to say something."""
        events = [self.event("test_failed", 1, failed_tests=[], exit_code=1)]

        assert attempt_ledger(events) == [(1, "test_failed", "exit code 1, no test named")]

    def test_a_rejection_row_is_the_first_line_of_the_reason(self):
        """A real Reviewer's reason is an attribution walk several lines long.
        The ledger is a shape read at a glance; the full text is printed below it."""
        events = [
            self.event("review_rejected", 1, reason="line one\nline two", violated_constraints=[])
        ]

        assert attempt_ledger(events) == [(1, "review_rejected", "line one")]


class TestTheEscalationOutputAgainstRealTerminalStates:
    """Livelock and broken-suite, rendered from states the loop actually produced.

    Neither is reachable from a `--stub` run, and both omissions are structural
    rather than accidental: `stub_agents` scripts a distinct marker per attempt
    precisely so the run does *not* livelock, and the stub edit is an appended
    comment, which always parses. So these two are driven through `test_loop`'s
    scenario helper instead -- a real `run_task`, a real reset, a real pytest, and
    a terminal state the loop built rather than one this file typed out.

    That is sufficient coverage, and the reason is worth stating: neither
    rendering reads anything a model produced. Livelock is a byte comparison over
    rendered diffs and broken-suite is a pytest exit code, so what a real run
    would add over this is a network call and nothing else. The renderings that
    *do* meet a human on the default path -- retry limit and human refusal -- are
    the two driven end to end through `main` above.
    """

    def report_on(self, run) -> str:
        """Render a finished scenario's terminal state and return the output.

        `run.log.path` rather than a derived `logs/<task_id>.jsonl`, which is the
        whole reason `report` takes the path: the scenario wrote its log under a
        temp directory.
        """
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report(run.final, run.log.path)
        return buffer.getvalue()

    @pytest.fixture(scope="class")
    @classmethod
    def stuck(cls, tmp_path_factory):
        """Two attempts, the same marker, so the two diffs are byte-identical."""
        root = tmp_path_factory.mktemp("report_stuck")
        return run_scenario(
            EventLog("report_stuck", log_dir=root / "logs"),
            root / "runs",
            edit_lists=[failing_edits(1), failing_edits(1)],
        )

    @pytest.fixture(scope="class")
    @classmethod
    def broken(cls, tmp_path_factory):
        """An edit that leaves the file unparseable, so pytest cannot collect."""
        root = tmp_path_factory.mktemp("report_broken")
        return run_scenario(
            EventLog("report_broken", log_dir=root / "logs"),
            root / "runs",
            edit_lists=[broken_edits(), fixing_edits()],
        )

    def test_livelock_attributes_the_diff_to_the_attempt_that_first_rendered_it(
        self, stuck
    ):
        assert stuck.final.status is Status.ESCALATED_LIVELOCK
        out = self.report_on(stuck)

        assert "Attempt 2 rendered a diff byte-identical to attempt 1's." in out

    def test_livelock_prints_the_repeated_diff_and_names_the_plan(self, stuck):
        out = self.report_on(stuck)

        assert "The repeated diff" in out
        assert "--- a/pricing/discounts.py" in out
        assert "Plan under suspicion" in out
        assert "v1 does not replan" in out

    def test_livelock_carries_the_halt_reason_from_the_real_log(self, stuck):
        """`LIVELOCK_REASON`, written by `_halt` and read back out of the log --
        the round trip the report depends on, over a log the loop wrote."""
        assert "plan is the likely fault" in self.report_on(stuck)

    def test_a_broken_suite_shows_the_real_exit_code_and_traceback(self, broken):
        assert broken.final.status is Status.ESCALATED_BROKEN_SUITE
        out = self.report_on(broken)

        assert "What pytest said" in out
        assert f"exit code  {broken.final.test_result.exit_code}" in out
        assert "The diff that broke it" in out
        assert "these edits are applied" in out

    def test_a_broken_suite_prints_no_last_failure_section(self, broken):
        """The loop writes no evidence on this path, so there is nothing to show
        -- and showing an earlier attempt's would misattribute the halt."""
        assert broken.final.evidence is None
        assert "Last failure" not in self.report_on(broken)

    def test_neither_escalation_claims_a_success(self, stuck, broken):
        for out in (self.report_on(stuck), self.report_on(broken)):
            assert "Recovered from" not in out
            assert "succeeded" not in out


# -- the --stub flag ---------------------------------------------------------


class TestStubTargets:
    def test_it_finds_the_fixtures_source_files(self):
        assert stub_targets(str(FIXTURE)) == [
            "pricing/__init__.py",
            "pricing/catalog.py",
            "pricing/discounts.py",
            "pricing/money.py",
            "pricing/orders.py",
        ]

    def test_test_files_are_excluded(self):
        targets = stub_targets(str(FIXTURE))

        assert not any(path.startswith("tests/") for path in targets)

    def test_non_python_files_are_excluded(self, tmp_path):
        (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
        (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")

        assert stub_targets(str(tmp_path)) == ["real.py"]

    def test_a_top_level_test_module_is_excluded_too(self, tmp_path):
        (tmp_path / "test_thing.py").write_text("def test_x(): pass\n", encoding="utf-8")
        (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")

        assert stub_targets(str(tmp_path)) == ["real.py"]


class TestStubAgents:
    def test_the_plan_targets_exactly_the_file_the_implementer_edits(self, tmp_path):
        """A plan naming every source file would make the scope check vacuous,
        and the point of a stub run is that every step does its real work."""
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        planner, implementer, _ = stub_agents(log, str(FIXTURE))

        plan = planner.script.entries[0]
        edited = {edit.path for edits in implementer.script.entries for edit in edits}
        assert plan.target_files == ["pricing/__init__.py"]
        assert edited == set(plan.target_files)

    def test_the_planner_gets_one_entry_because_v1_plans_once(self, tmp_path):
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        planner, _, _ = stub_agents(log, str(FIXTURE))

        assert len(planner.script.entries) == 1

    def test_the_scripts_are_long_enough_for_every_attempt(self, tmp_path):
        """`ScriptExhausted` on a legitimate fifth attempt would turn a status
        into a crash."""
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        _, implementer, reviewer = stub_agents(log, str(FIXTURE))

        assert len(implementer.script.entries) == MAX_ATTEMPTS
        assert len(reviewer.script.entries) == MAX_ATTEMPTS

    def test_every_attempt_differs_byte_wise_from_the_others(self, tmp_path):
        """Every attempt starts from an identical baseline, so two edits that
        both merely leave the bug in place render byte-identical diffs and halt
        the run on the livelock check at attempt 2 -- short of the retry path
        this exists to exercise. The marker is what keeps them apart."""
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        _, implementer, _ = stub_agents(log, str(FIXTURE))

        contents = [edits[0].new_content for edits in implementer.script.entries]
        assert len(set(contents)) == MAX_ATTEMPTS

    def test_the_edit_keeps_the_real_baseline_and_only_appends(self, tmp_path):
        """Read from the fixture, never inlined. An inlined module body would be
        a second source of truth that rots the first time the fixture changes."""
        log = EventLog("stub_test", log_dir=tmp_path / "logs")
        baseline = read_repo_file(str(FIXTURE), "pricing/__init__.py")

        _, implementer, _ = stub_agents(log, str(FIXTURE))

        assert implementer.script.entries[0][0].new_content.startswith(baseline)

    def test_the_reviewer_approves_so_the_gate_and_apply_step_are_reached(self, tmp_path):
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        _, _, reviewer = stub_agents(log, str(FIXTURE))

        assert all(verdict.approved for verdict in reviewer.script.entries)

    def test_a_repo_with_no_source_file_is_a_clean_exit(self, tmp_path):
        (tmp_path / "README.md").write_text("nothing here", encoding="utf-8")
        log = EventLog("stub_test", log_dir=tmp_path / "logs")

        with pytest.raises(SystemExit) as caught:
            stub_agents(log, str(tmp_path))

        assert "no non-test Python file" in str(caught.value)


class TestAStubRunEndToEnd:
    """The whole entrypoint, driven for real, with no API key in the environment.

    This is what turns "no test needs a key" from a property of the fakes into a
    property of `cli.py` itself. Nothing here is monkeypatched except the human
    at the gate and the working directory -- the loop, the workspace, the event
    log, and the real Tester all do their actual jobs.
    """

    @pytest.fixture(autouse=True)
    def no_key_and_a_scratch_cwd(self, monkeypatch, tmp_path):
        # `runs/` and `logs/` are resolved relative to the working directory, so
        # chdir keeps a real run out of the developer's own directories.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

    def argv(self) -> list[str]:
        return ["--task", str(FIXTURE / "task.json"), "--stub"]

    def test_a_refusal_at_the_gate_halts_without_a_key(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _: "n")

        code = main(self.argv())

        out = capsys.readouterr().out
        assert code == 1
        assert "aborted_by_human" in out
        assert "attempts      1 of 5" in out

    def test_the_abort_rendering_runs_for_real(self, monkeypatch, capsys):
        """One of the two escalation renderings a stub run can actually reach, so
        it is exercised through `main` rather than against a hand-built state.

        The Reviewer's reason here is the stub's own, which is the point: the
        report read it off `state.review` after the halt, with the gate's
        signature untouched."""
        monkeypatch.setattr("builtins.input", lambda _: "n")

        main(self.argv())

        out = capsys.readouterr().out
        assert "reason        the human rejected the diff at the approval gate." in out
        assert "What the Reviewer had said about it" in out
        assert "stub verdict: no model read this diff" in out
        assert "The diff you refused" in out
        assert "On disk: nothing." in out

    def test_the_banner_says_the_agents_are_stubbed(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _: "n")

        main(self.argv())

        assert "stubbed (no API calls)" in capsys.readouterr().out

    def test_the_diff_reaches_the_gate(self, monkeypatch, capsys):
        """Everything up to the approval gate really ran: plan, reset, implement,
        no-edits check, scope check, render, livelock check, review."""
        monkeypatch.setattr("builtins.input", lambda _: "n")

        main(self.argv())

        out = capsys.readouterr().out
        assert "--- a/pricing/__init__.py" in out
        assert "# stub attempt 1" in out

    def test_the_log_records_stubs_rather_than_llm_calls(self, monkeypatch, capsys, tmp_path):
        """Which agents ran is unambiguous from the log without `run_started`
        growing a field for it."""
        monkeypatch.setattr("builtins.input", lambda _: "n")

        main(self.argv())

        events = [
            json.loads(line)
            for log in (tmp_path / "logs").glob("*.jsonl")
            for line in log.read_text(encoding="utf-8").splitlines()
        ]
        names = {event["event"] for event in events}
        assert "stub_scripted" in names
        assert "llm_request" not in names

    def test_it_runs_to_the_cap_and_cannot_succeed(self, monkeypatch, capsys):
        """A scripted agent decides nothing, so it cannot fix a bug it was never
        told about. The suite stays red and the run halts at the retry limit --
        having exercised apply, the real Tester, the evidence, and the retry."""
        monkeypatch.setattr("builtins.input", lambda _: "y")

        code = main(self.argv())

        out = capsys.readouterr().out
        assert code == 1
        assert "escalated_retry_limit" in out
        assert "attempts      5 of 5" in out
        assert "Last failure (tester_failure)" in out

    def test_the_retry_limit_ledger_runs_for_real(self, monkeypatch, capsys):
        """The other rendering a stub run reaches, and the more valuable one: the
        ledger is built from a log five real attempts actually wrote.

        Every row reads `test_failed` because the stub fails the same way five
        times over -- which is the honest shape of this particular run, and still
        proves the ledger is assembled from the log rather than from `evidence`,
        which holds only the last of the five."""
        monkeypatch.setattr("builtins.input", lambda _: "y")

        main(self.argv())

        out = capsys.readouterr().out
        assert "Attempt ledger" in out
        for attempt in range(1, MAX_ATTEMPTS + 1):
            assert f"  {attempt}  test_failed" in out
        assert "attempt 5's edits are applied" in out
        assert "Plan under suspicion" in out
        assert "Stub plan: no model was consulted" in out

    def test_the_real_path_still_demands_a_key(self, monkeypatch, capsys):
        """The mirror of the above: dropping `--stub` with no key exits before
        anything is copied."""
        with pytest.raises(SystemExit) as caught:
            main(["--task", str(FIXTURE / "task.json")])

        assert "GEMINI_API_KEY" in str(caught.value)
