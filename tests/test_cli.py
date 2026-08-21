"""Tests for the entrypoint's own logic: the task file, the gate, the report.

Not the wiring. Running `main` end to end would call the API, and the loop it
assembles already has 81 tests of its own in `test_loop.py`. What is untested
anywhere else is the boundary code -- reading a hand-authored JSON file, and a
human at a terminal -- so that is what is here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import read_task_file, report, terminal_approval
from harness.state import Status, TaskState, TesterFailure


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
