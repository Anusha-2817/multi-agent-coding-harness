"""Tests for harness/workspace.py and harness/agents/tester.py.

These run the real `fixture_repo_1`, not a mock. The Tester's whole job is to
report what a real pytest subprocess said, so a stubbed subprocess would test
the stub. The cost is about a second per invocation, which is worth it.

Since Phase 2 the Tester is an `Agent`, so its public signature is
`run(state) -> TaskState` like every other agent's. Almost everything here is
about what pytest reported, not about state plumbing, so `run_tester` unwraps to
the `TestResult` and the tests read as they did before. The state-level contract
is covered by `TestAgentInterface` below and by tests/test_agent_base.py.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.agents.tester import (
    TIMEOUT_EXIT_CODE,
    Tester,
    strip_ansi,
    tester_failure_from,
)
from harness.events import EventLog
from harness.state import STDOUT_TAIL_LIMIT, Plan, TaskState, TestResult
from harness.workspace import (
    list_repo_files,
    prepare_run_dir,
    reset_run_dir,
    run_dir_for,
)

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

# The one test fixture_repo_1 is built to fail.
FAILING_NODEID = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"


def task_state(repo_path: str | Path, attempt: int = 0) -> TaskState:
    """A minimal state carrying what the Tester's contract requires."""
    return TaskState(
        task_id="fixture_repo_1_20260815T120000Z",
        repo_path=str(repo_path),
        task_description="An order at a tier boundary gets the wrong discount.",
        failure_input="1 failed, 19 passed",
        attempt_count=attempt,
    )


def run_tester(tester: Tester, repo_path: str | Path, attempt: int = 0) -> TestResult:
    """Run the Tester over a fresh state and hand back just the TestResult."""
    return tester.run(task_state(repo_path, attempt)).test_result


@pytest.fixture
def runs_root(tmp_path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def event_log(tmp_path) -> EventLog:
    return EventLog("fixture_repo_1_20260815T120000Z", log_dir=tmp_path / "logs")


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under `directory`, keyed by relative path."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def fix_the_bug(repo_path: str) -> None:
    """Apply the one-character fix the fixture is designed to need."""
    source = Path(repo_path) / "pricing" / "discounts.py"
    text = source.read_text(encoding="utf-8")
    assert "if quantity > tier.min_quantity:" in text
    source.write_text(
        text.replace("if quantity > tier.min_quantity:", "if quantity >= tier.min_quantity:"),
        encoding="utf-8",
    )


class TestPrepareRunDir:
    def test_copies_the_fixture(self, runs_root):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        assert Path(repo_path).is_dir()
        assert (Path(repo_path) / "pricing" / "discounts.py").is_file()
        assert (Path(repo_path) / "tests" / "test_orders.py").is_file()
        assert (Path(repo_path) / "pytest.ini").is_file()

    def test_copy_matches_the_fixture_byte_for_byte(self, runs_root):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        assert snapshot(Path(repo_path)) == snapshot(FIXTURE)

    def test_returns_the_path_under_runs_root(self, runs_root):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        assert Path(repo_path) == runs_root / "run_a"
        assert repo_path == run_dir_for("run_a", runs_root)

    def test_caches_are_not_copied(self, runs_root, tmp_path):
        dirty = tmp_path / "dirty_fixture"
        dirty.mkdir()
        (dirty / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (dirty / "__pycache__").mkdir()
        (dirty / "__pycache__" / "mod.pyc").write_bytes(b"stale")
        (dirty / ".pytest_cache").mkdir()
        (dirty / ".pytest_cache" / "lastfailed").write_text("{}", encoding="utf-8")

        repo_path = Path(prepare_run_dir("run_a", dirty, runs_root))

        assert (repo_path / "mod.py").is_file()
        assert not (repo_path / "__pycache__").exists()
        assert not (repo_path / ".pytest_cache").exists()

    def test_an_existing_run_dir_is_not_silently_overwritten(self, runs_root):
        prepare_run_dir("run_a", FIXTURE, runs_root)

        with pytest.raises(FileExistsError):
            prepare_run_dir("run_a", FIXTURE, runs_root)


class TestResetRunDir:
    def test_restores_a_modified_run_directory(self, runs_root):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        original = snapshot(repo_path)
        fix_the_bug(str(repo_path))
        assert snapshot(repo_path) != original

        reset = Path(reset_run_dir("run_a", FIXTURE, runs_root))

        assert snapshot(reset) == original == snapshot(FIXTURE)

    def test_removes_files_added_during_an_attempt(self, runs_root):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        (repo_path / "pricing" / "sneaky.py").write_text("# left behind\n", encoding="utf-8")

        reset = Path(reset_run_dir("run_a", FIXTURE, runs_root))

        assert not (reset / "pricing" / "sneaky.py").exists()

    def test_works_when_the_run_directory_is_absent(self, runs_root):
        repo_path = Path(reset_run_dir("never_prepared", FIXTURE, runs_root))

        assert snapshot(repo_path) == snapshot(FIXTURE)


class TestVersionControlIsNotCopied:
    """`.git` is excluded from the copy and from the listing. Task 6.3, from F1.

    The first real repository this harness was pointed at crashed on this, so
    these are regression tests for a failure that actually happened rather than
    one imagined -- `transcripts/python-slugify_20260923T113847Z.jsonl` is the
    run that died.

    A local helper builds the fixture rather than `fixture_repo_1`, because the
    fixtures have no `.git` and giving one a fake `.git` to satisfy a test would
    be fixture-shaped evidence for a real-repo problem.
    """

    def repo_with_git(self, tmp_path: Path, *, read_only_pack: bool = False) -> Path:
        """A source tree with a `.git` in it, shaped like a real shallow clone."""
        repo = tmp_path / "cloned_repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

        git = repo / ".git"
        (git / "hooks").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n", encoding="utf-8")

        pack = git / "objects" / "pack"
        pack.mkdir(parents=True)
        blob = pack / "pack-deadbeef.pack"
        blob.write_bytes(b"PACK\x00\x00\x00\x02")
        if read_only_pack:
            # What git actually does on Windows, and the whole of why this crashed.
            import stat

            blob.chmod(stat.S_IREAD)
        return repo

    def test_git_is_not_copied_into_the_run_directory(self, runs_root, tmp_path):
        repo = self.repo_with_git(tmp_path)

        repo_path = Path(prepare_run_dir("run_a", repo, runs_root))

        assert not (repo_path / ".git").exists()
        # The repository's own files survive, `.gitignore` included: it is a file
        # the project tracks, not version-control internals.
        assert (repo_path / "pkg" / "mod.py").is_file()
        assert (repo_path / ".gitignore").is_file()

    def test_git_is_not_listed_for_a_prompt(self, tmp_path):
        """The listing and the copy must agree. A file shown to the Planner that
        is not in the run directory would name a path no plan could target."""
        repo = self.repo_with_git(tmp_path)

        listed = list_repo_files(repo)

        assert not [path for path in listed if path.startswith(".git/")]
        assert listed == [".gitignore", "pkg/mod.py"]

    def test_a_read_only_pack_no_longer_breaks_the_reset(self, runs_root, tmp_path):
        """The 6.2 crash, pinned.

        Git marks pack files read-only on Windows. Before the exclusion they were
        copied into the run directory, `reset_run_dir`'s `rmtree` could not delete
        them, the directory survived the reset, and `prepare_run_dir`'s `copytree`
        raised `FileExistsError` at the top of attempt 1. Resetting twice is the
        assertion: the second call is the one that used to fail.
        """
        repo = self.repo_with_git(tmp_path, read_only_pack=True)
        prepare_run_dir("run_a", repo, runs_root)

        first = Path(reset_run_dir("run_a", repo, runs_root))
        second = Path(reset_run_dir("run_a", repo, runs_root))

        assert snapshot(first) == snapshot(second)
        assert (second / "pkg" / "mod.py").is_file()
        assert not (second / ".git").exists()

    def test_the_source_repository_keeps_its_own_git(self, runs_root, tmp_path):
        """Excluding it from the copy must not mean touching the original."""
        repo = self.repo_with_git(tmp_path)

        prepare_run_dir("run_a", repo, runs_root)
        reset_run_dir("run_a", repo, runs_root)

        assert (repo / ".git" / "HEAD").is_file()
        assert (repo / ".git" / "objects" / "pack" / "pack-deadbeef.pack").is_file()


class TestIndependentCopies:
    def test_two_task_ids_do_not_share_state(self, runs_root):
        first = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        second = Path(prepare_run_dir("run_b", FIXTURE, runs_root))

        fix_the_bug(str(first))

        assert first != second
        assert snapshot(second) == snapshot(FIXTURE)
        assert snapshot(first) != snapshot(second)

    def test_resetting_one_leaves_the_other_alone(self, runs_root):
        first = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        second = Path(prepare_run_dir("run_b", FIXTURE, runs_root))
        fix_the_bug(str(second))
        fixed = snapshot(second)

        reset_run_dir("run_a", FIXTURE, runs_root)

        assert snapshot(second) == fixed
        assert snapshot(first) == snapshot(FIXTURE)


class TestRunningTheRealFixture:
    def test_reports_one_failure_and_nineteen_passes(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log), repo_path)

        assert result.passed is False
        assert result.exit_code == 1
        assert result.failed_tests == [FAILING_NODEID]
        assert "1 failed, 19 passed" in result.stdout

    def test_traceback_carries_the_assertion(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log), repo_path)

        assert "AssertionError" in result.traceback
        assert "Decimal('125.00')" in result.traceback
        assert "Decimal('118.75')" in result.traceback

    def test_the_whole_suite_runs_not_just_the_failing_test(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log), repo_path)

        assert "collected 20 items" in result.stdout

    def test_a_fixed_fixture_passes(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        fix_the_bug(repo_path)

        result = run_tester(Tester(event_log), repo_path)

        assert result.passed is True
        assert result.exit_code == 0
        assert result.failed_tests == []
        assert result.traceback == ""
        assert "20 passed" in result.stdout

    def test_the_fixture_directory_is_untouched_by_a_run(self, runs_root, event_log):
        before = snapshot(FIXTURE)
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        run_tester(Tester(event_log), repo_path)

        assert snapshot(FIXTURE) == before

    def test_a_run_leaves_no_caches_in_the_fixture(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        run_tester(Tester(event_log), repo_path)

        assert not list(FIXTURE.rglob("__pycache__"))
        assert not list(FIXTURE.rglob(".pytest_cache"))

    def test_stale_caches_are_cleared_before_pytest_runs(self, runs_root, event_log):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        stale = repo_path / "pricing" / "__pycache__"
        stale.mkdir()
        (stale / "discounts.cpython-999.pyc").write_bytes(b"not valid bytecode")

        result = run_tester(Tester(event_log), repo_path)

        assert not (stale / "discounts.cpython-999.pyc").exists()
        assert result.exit_code == 1


class TestColouredOutputIsParsed:
    """Task 6.4-F4. A target that forces colour must still be read correctly.

    The input is **real captured output**, not a hand-written imitation:
    `tests/data/coloured_pytest_output.txt` is what
    `python -m pytest` actually printed inside a clone of `un33k/python-slugify`,
    whose `pyproject.toml` carries `addopts = "--color=yes"` and therefore colours
    its output even when stdout is not a tty. 6,129 characters, 528 CSI sequences,
    exit code 1, three real failures.

    It is committed rather than produced by the test, and that is the only honest
    option: no fixture in this repo sets `addopts`, and the clone lives outside
    the repo in `experiments/`, so a test that shelled out to it would pass or
    fail depending on whether someone had staged a checkout. **This is a replayed
    real subprocess, not a stubbed one** — the distinction CLAUDE.md draws about
    stubbed subprocesses is about inventing output, and nothing here is invented.

    `subprocess.run` is replaced so the captured bytes travel the real
    `_invoke_pytest` path. That is the half worth testing: parsing the text is
    one thing, and the de-colouring actually being wired in ahead of both parsers
    while `stdout` stays raw is another.
    """

    COLOURED = Path(__file__).resolve().parent / "data" / "coloured_pytest_output.txt"

    #: The three real failures in that capture, as pytest reported them.
    NODEIDS = [
        "tests/test_release.py::ReleaseRegressionTests::test_actual_separator_width_and_literal_characters",
        "tests/test_release.py::ReleaseRegressionTests::test_empty_separator_preserves_word_boundaries",
        "tests/test_release.py::SplitCoverageTests::test_modern_word_boundary_empty_token",
    ]

    @pytest.fixture
    def coloured(self) -> str:
        text = self.COLOURED.read_text(encoding="utf-8")
        # Guards the premise rather than the behaviour: if the capture ever loses
        # its escapes, every assertion below would pass for the wrong reason.
        assert "\x1b[" in text, "the captured output has lost its ANSI escapes"
        return text

    @pytest.fixture
    def result(self, monkeypatch, coloured, event_log, tmp_path):
        """The captured bytes driven through the real `_invoke_pytest`."""

        class Completed:
            returncode = 1
            stdout = coloured
            stderr = ""

        monkeypatch.setattr(
            "harness.agents.tester.subprocess.run",
            lambda *args, **kwargs: Completed(),
        )
        return Tester(event_log)._run(repo_path=str(tmp_path))

    def test_the_summary_is_read_rather_than_reported_unreadable(self, result):
        """The whole of F4. Before the fix this was `False`, and the loop read
        that as `escalated_broken_suite` -- "pytest could not collect" -- on a
        suite that collected 126 tests and failed 3."""
        assert result.summary_parsed is True

    def test_the_real_nodeids_are_recovered(self, result):
        assert result.failed_tests == self.NODEIDS

    def test_no_nodeid_carries_an_escape_sequence(self, result):
        """A strip that ran after the regex rather than before it could still
        match and leave colour inside the captured group."""
        for nodeid in result.failed_tests:
            assert "\x1b" not in nodeid

    def test_the_traceback_is_extracted_and_de_coloured(self, result):
        """`_failure_detail` anchors on the same `=` bar, so it was equally
        defeated -- and its output is prompt-bound, reaching a model through
        `TesterFailure.traceback`."""
        assert result.traceback
        assert "\x1b" not in result.traceback
        assert "AssertionError" in result.traceback

    def test_raw_stdout_keeps_every_escape(self, result, coloured):
        """Stripping is for the harness's own reading. The event log keeps what
        pytest actually wrote, so CLAUDE.md's "the log is self-contained" claim
        is untouched."""
        assert result.stdout == coloured
        assert "\x1b[" in result.stdout

    def test_the_exit_code_still_decides_the_gating(self, result):
        """F4's fix must not reach the exit-code gating. 1 means a trustworthy
        per-test outcome set exists; nothing about colour changes that."""
        assert result.exit_code == 1
        assert result.passed is False


class TestStripAnsi:
    """The helper on its own. Narrow, because its job is narrow."""

    def test_sgr_colour_is_removed(self):
        assert strip_ansi("\x1b[31mFAILED\x1b[0m tests/x.py::y") == "FAILED tests/x.py::y"

    def test_uncoloured_text_is_returned_unchanged(self):
        """Every existing fixture is uncoloured, so this is the common path and
        it must be a no-op rather than a reformat."""
        plain = "=== short test summary info ===\nFAILED tests/x.py::y - boom\n"

        assert strip_ansi(plain) == plain

    def test_a_column_zero_anchor_matches_after_stripping(self):
        """Stated as the property the parsers actually depend on."""
        assert strip_ansi("\x1b[36m\x1b[1m==== x ====\x1b[0m").startswith("=")

    def test_non_sgr_csi_sequences_go_too(self):
        """pytest emits only SGR, but a cursor move would be just as invisible to
        an anchored parser, so the whole CSI class is matched."""
        assert strip_ansi("a\x1b[2Kb\x1b[1;5Hc") == "abc"

    def test_an_empty_string_is_safe(self):
        assert strip_ansi("") == ""


class TestSummaryParsed:
    """`summary_parsed` is False only when a per-test outcome set was expected
    and could not be read. Not on a pass, where pytest prints no summary at all,
    and not on a timeout, where nothing finished."""

    def test_true_when_tests_fail_normally(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log), repo_path)

        assert result.summary_parsed is True
        assert result.failed_tests == [FAILING_NODEID]

    def test_true_when_everything_passes(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        fix_the_bug(repo_path)

        result = run_tester(Tester(event_log), repo_path)

        assert result.summary_parsed is True
        assert result.failed_tests == []

    def test_true_on_timeout(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log, timeout=0.001), repo_path)

        assert result.summary_parsed is True
        assert result.failed_tests == []

    def test_false_on_a_collection_error(self, runs_root, event_log):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        # An unimportable test module: pytest cannot collect the suite at all,
        # so no test ever runs and no per-test outcome set exists.
        (repo_path / "tests" / "test_broken.py").write_text(
            "import a_module_that_does_not_exist\n\n\ndef test_nothing():\n    pass\n",
            encoding="utf-8",
        )

        result = run_tester(Tester(event_log), repo_path)

        assert result.passed is False
        assert result.summary_parsed is False
        assert result.exit_code not in (0, 1)

    def test_a_collection_error_puts_no_file_paths_in_failed_tests(self, runs_root, event_log):
        """pytest's summary lists `ERROR tests/test_broken.py` -- a path, not a
        nodeid. It must not reach the field the Implementer will read."""
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        (repo_path / "tests" / "test_broken.py").write_text(
            "import a_module_that_does_not_exist\n", encoding="utf-8"
        )

        result = run_tester(Tester(event_log), repo_path)

        assert result.failed_tests == []

    def test_a_collection_error_still_leaves_something_to_read(self, runs_root, event_log):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        (repo_path / "tests" / "test_broken.py").write_text(
            "import a_module_that_does_not_exist\n", encoding="utf-8"
        )

        result = run_tester(Tester(event_log), repo_path)

        assert "a_module_that_does_not_exist" in result.traceback + result.stdout


class TestEventLogging:
    def test_both_events_are_logged(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        run_tester(Tester(event_log), repo_path, attempt=3)

        events = [line for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert len(events) == 2

    def test_the_result_is_logged_once_by_the_base_class(self, runs_root, event_log):
        """`test_run_completed` is gone: the base class's `agent_produced` carries
        the TestResult, and logging it here as well would put it in twice."""
        import json

        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log), repo_path, attempt=3)

        records = [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert [r["event"] for r in records] == ["test_run_started", "agent_produced"]
        assert all(r["agent"] == "tester" for r in records)
        assert all(r["attempt"] == 3 for r in records)
        assert TestResult.model_validate(records[1]["payload"]["test_result"]) == result

    def test_the_attempt_comes_from_the_state(self, runs_root, event_log):
        """No `attempt` parameter any more -- `run(state)` reads `attempt_count`."""
        import json

        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        Tester(event_log).run(task_state(repo_path, attempt=4))

        records = [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert all(r["attempt"] == 4 for r in records)


class TestTimeout:
    def test_a_timeout_is_reported_not_raised(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = run_tester(Tester(event_log, timeout=0.001), repo_path)

        assert result.passed is False
        assert result.exit_code == TIMEOUT_EXIT_CODE
        assert "timed out" in result.traceback

    def test_a_timeout_still_logs_both_events(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        run_tester(Tester(event_log, timeout=0.001), repo_path)

        assert len(event_log.path.read_text(encoding="utf-8").splitlines()) == 2


class TestTesterFailureFrom:
    def test_carries_the_result_across(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        result = run_tester(Tester(event_log), repo_path)

        evidence = tester_failure_from(result)

        assert evidence.kind == "tester_failure"
        assert evidence.failed_tests == [FAILING_NODEID]
        assert evidence.traceback == result.traceback

    def test_stdout_tail_is_truncated_to_the_cap(self):
        result = TestResult(
            passed=False,
            failed_tests=["tests/test_x.py::test_y"],
            summary_parsed=True,
            traceback="AssertionError",
            stdout="HEAD" + ("." * STDOUT_TAIL_LIMIT) + "TAIL",
            exit_code=1,
        )

        evidence = tester_failure_from(result)

        assert len(evidence.stdout_tail) == STDOUT_TAIL_LIMIT
        assert evidence.stdout_tail.endswith("TAIL")
        assert "HEAD" not in evidence.stdout_tail

    def test_short_stdout_is_kept_whole(self):
        result = TestResult(
            passed=False,
            failed_tests=[],
            summary_parsed=True,
            traceback="boom",
            stdout="1 failed, 19 passed",
            exit_code=1,
        )

        assert tester_failure_from(result).stdout_tail == "1 failed, 19 passed"


class TestAgentInterface:
    """The Tester as an Agent: `run(state) -> TaskState`, written by the base class."""

    def test_the_returned_state_carries_the_result(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        after = Tester(event_log).run(task_state(repo_path))

        assert isinstance(after, TaskState)
        assert after.test_result is not None
        assert after.test_result.failed_tests == [FAILING_NODEID]

    def test_the_input_state_is_not_mutated(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        before = task_state(repo_path)

        after = Tester(event_log).run(before)

        assert before.test_result is None
        assert after is not before

    def test_every_other_field_is_carried_across(self, runs_root, event_log):
        """The base class writes one field. It must not drop the rest on the way."""
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        plan = Plan(
            summary="Use an inclusive lower bound.",
            steps=["Change > to >= in tier_for"],
            target_files=["pricing/discounts.py"],
            constraints=["Do not change the tier table"],
        )
        before = task_state(repo_path, attempt=2).model_copy(
            update={"plan": plan, "diff": "--- a\n+++ b\n"}
        )

        after = Tester(event_log).run(before)

        assert after.plan == plan
        assert after.diff == "--- a\n+++ b\n"
        assert after.attempt_count == 2
        assert after.task_id == before.task_id

    def test_a_state_without_repo_path_cannot_be_built(self):
        """`repo_path` is required at construction, so the Tester's one required
        field can never be None by the time the loop routes to it. Its contract
        assertion is a backstop, not the first line of defence."""
        with pytest.raises(ValidationError) as caught:
            TaskState(  # type: ignore[call-arg]
                task_id="t",
                task_description="d",
                failure_input="f",
            )

        assert "repo_path" in str(caught.value)
