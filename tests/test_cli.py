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

import json
from pathlib import Path

import pytest

from cli import main, read_task_file, report, stub_agents, stub_targets, terminal_approval
from harness.events import EventLog
from harness.loop import MAX_ATTEMPTS
from harness.state import Status, TaskState, TesterFailure
from harness.workspace import read_repo_file

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"


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
    def state(self, **fields) -> TaskState:
        return TaskState(
            task_id="fixture_repo_1_20260821T120000Z",
            repo_path="runs/fixture_repo_1_20260821T120000Z",
            task_description="d",
            failure_input="f",
            **fields,
        )

    def test_it_names_the_status_the_attempts_and_where_to_look(self, capsys):
        report(self.state(status=Status.SUCCEEDED, attempt_count=2))

        out = capsys.readouterr().out
        assert "succeeded" in out
        assert "2 of 5" in out
        assert "logs/fixture_repo_1_20260821T120000Z.jsonl" in out

    def test_surviving_evidence_is_reported_even_on_a_success(self, capsys):
        """CLAUDE.md: a run that failed once and then succeeded ends with the
        failure still in `evidence`, and that is the interesting part."""
        recovered = self.state(
            status=Status.SUCCEEDED,
            attempt_count=2,
            evidence=TesterFailure(failed_tests=["t::x"], traceback="boom", stdout_tail=""),
        )

        report(recovered)

        assert "tester_failure" in capsys.readouterr().out

    def test_a_run_with_no_evidence_says_nothing_about_it(self, capsys):
        report(self.state(status=Status.SUCCEEDED, attempt_count=1))

        assert "last failure" not in capsys.readouterr().out


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
        assert "last failure  tester_failure" in out

    def test_the_real_path_still_demands_a_key(self, monkeypatch, capsys):
        """The mirror of the above: dropping `--stub` with no key exits before
        anything is copied."""
        with pytest.raises(SystemExit) as caught:
            main(["--task", str(FIXTURE / "task.json")])

        assert "GEMINI_API_KEY" in str(caught.value)
