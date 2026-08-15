"""Tests for harness/workspace.py and harness/agents/tester.py.

These run the real `fixture_repo_1`, not a mock. The Tester's whole job is to
report what a real pytest subprocess said, so a stubbed subprocess would test
the stub. The cost is about a second per invocation, which is worth it.
"""

from pathlib import Path

import pytest

from harness.agents.tester import (
    TIMEOUT_EXIT_CODE,
    Tester,
    tester_failure_from,
)
from harness.events import EventLog
from harness.state import STDOUT_TAIL_LIMIT, TestResult
from harness.workspace import prepare_run_dir, reset_run_dir, run_dir_for

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"

# The one test fixture_repo_1 is built to fail.
FAILING_NODEID = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"


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

        result = Tester(event_log).run(repo_path)

        assert result.passed is False
        assert result.exit_code == 1
        assert result.failed_tests == [FAILING_NODEID]
        assert "1 failed, 19 passed" in result.stdout

    def test_traceback_carries_the_assertion(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log).run(repo_path)

        assert "AssertionError" in result.traceback
        assert "Decimal('125.00')" in result.traceback
        assert "Decimal('118.75')" in result.traceback

    def test_the_whole_suite_runs_not_just_the_failing_test(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log).run(repo_path)

        assert "collected 20 items" in result.stdout

    def test_a_fixed_fixture_passes(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        fix_the_bug(repo_path)

        result = Tester(event_log).run(repo_path)

        assert result.passed is True
        assert result.exit_code == 0
        assert result.failed_tests == []
        assert result.traceback == ""
        assert "20 passed" in result.stdout

    def test_the_fixture_directory_is_untouched_by_a_run(self, runs_root, event_log):
        before = snapshot(FIXTURE)
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        Tester(event_log).run(repo_path)

        assert snapshot(FIXTURE) == before

    def test_a_run_leaves_no_caches_in_the_fixture(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        Tester(event_log).run(repo_path)

        assert not list(FIXTURE.rglob("__pycache__"))
        assert not list(FIXTURE.rglob(".pytest_cache"))

    def test_stale_caches_are_cleared_before_pytest_runs(self, runs_root, event_log):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        stale = repo_path / "pricing" / "__pycache__"
        stale.mkdir()
        (stale / "discounts.cpython-999.pyc").write_bytes(b"not valid bytecode")

        result = Tester(event_log).run(str(repo_path))

        assert not (stale / "discounts.cpython-999.pyc").exists()
        assert result.exit_code == 1


class TestSummaryParsed:
    """`summary_parsed` is False only when a per-test outcome set was expected
    and could not be read. Not on a pass, where pytest prints no summary at all,
    and not on a timeout, where nothing finished."""

    def test_true_when_tests_fail_normally(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log).run(repo_path)

        assert result.summary_parsed is True
        assert result.failed_tests == [FAILING_NODEID]

    def test_true_when_everything_passes(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        fix_the_bug(repo_path)

        result = Tester(event_log).run(repo_path)

        assert result.summary_parsed is True
        assert result.failed_tests == []

    def test_true_on_timeout(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log, timeout=0.001).run(repo_path)

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

        result = Tester(event_log).run(str(repo_path))

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

        result = Tester(event_log).run(str(repo_path))

        assert result.failed_tests == []

    def test_a_collection_error_still_leaves_something_to_read(self, runs_root, event_log):
        repo_path = Path(prepare_run_dir("run_a", FIXTURE, runs_root))
        (repo_path / "tests" / "test_broken.py").write_text(
            "import a_module_that_does_not_exist\n", encoding="utf-8"
        )

        result = Tester(event_log).run(str(repo_path))

        assert "a_module_that_does_not_exist" in result.traceback + result.stdout


class TestEventLogging:
    def test_both_events_are_logged(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        Tester(event_log).run(repo_path, attempt=3)

        events = [line for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert len(events) == 2

    def test_the_completed_event_carries_the_whole_result(self, runs_root, event_log):
        import json

        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log).run(repo_path, attempt=3)

        records = [json.loads(line) for line in event_log.path.read_text(encoding="utf-8").splitlines()]
        assert [r["event"] for r in records] == ["test_run_started", "test_run_completed"]
        assert all(r["agent"] == "tester" for r in records)
        assert all(r["attempt"] == 3 for r in records)
        assert TestResult.model_validate(records[1]["payload"]["test_result"]) == result


class TestTimeout:
    def test_a_timeout_is_reported_not_raised(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        result = Tester(event_log, timeout=0.001).run(repo_path)

        assert result.passed is False
        assert result.exit_code == TIMEOUT_EXIT_CODE
        assert "timed out" in result.traceback

    def test_a_timeout_still_logs_both_events(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)

        Tester(event_log, timeout=0.001).run(repo_path)

        assert len(event_log.path.read_text(encoding="utf-8").splitlines()) == 2


class TestTesterFailureFrom:
    def test_carries_the_result_across(self, runs_root, event_log):
        repo_path = prepare_run_dir("run_a", FIXTURE, runs_root)
        result = Tester(event_log).run(repo_path)

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
