"""The Tester: runs the fixture's suite and reports what pytest said.

The only agent that needs no LLM. It reads `repo_path` and nothing else -- not
the plan, not the diff -- so its verdict cannot be coloured by what the change
was supposed to do. It runs the *whole* suite, because "fix it without breaking
anything else" is only verifiable against all of it.

It also does not own the directory it tests. `repo_path` arrives already
prepared by `harness.workspace`, and the Tester never resets or deletes it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from harness.events import EventLog
from harness.state import TesterFailure, TestResult

# A hung pytest must not hang the harness. Generous, because a real suite on a
# cold cache is slow and a false timeout would be reported as a test failure.
DEFAULT_TIMEOUT = 120.0

# No pytest run produced this: the process never exited. Deliberately outside
# pytest's own 0-5 range so it cannot be confused with a real outcome.
TIMEOUT_EXIT_CODE = -1

SUMMARY_HEADER = "short test summary info"

# The only two pytest exit codes that come with a trustworthy per-test outcome
# set. 2 (interrupted), 3 (internal error), 4 (usage error) and 5 (nothing
# collected) all mean the suite never produced one.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1

# `FAILED tests/test_orders.py::test_name` or `FAILED ...::test_name - AssertionError: ...`
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(.+)$")

# Caches are cleared inside the run directory, not just at copy time -- the
# previous attempt's pytest run wrote its own. Stale caches cause phantom results.
_CACHE_DIRS = ("__pycache__", ".pytest_cache")


def _clear_caches(repo_path: Path) -> None:
    for name in _CACHE_DIRS:
        for stale in repo_path.rglob(name):
            shutil.rmtree(stale, ignore_errors=True)


def _parse_summary(stdout: str) -> tuple[list[str], bool]:
    """Pull nodeids out of pytest's "short test summary info" section.

    Returns the nodeids and whether the section was found at all.

    Text, not pytest internals: parsing the reporting API would couple the
    harness to a pytest version, and a plugin would be a second thing to keep
    working. The summary section is stable, human-readable, and already on
    stdout.

    Only called for exit code 1, so `ERROR` lines here are setup/teardown errors
    on collected tests and carry full nodeids. File-level collection errors
    arrive with exit code 2 and never reach this function -- which is what keeps
    bare file paths out of `failed_tests`.
    """
    lines = stdout.splitlines()

    start = None
    for index, line in enumerate(lines):
        if line.startswith("=") and SUMMARY_HEADER in line:
            start = index + 1
            break
    if start is None:
        return [], False

    nodeids = []
    for line in lines[start:]:
        if line.startswith("="):  # the closing "== 1 failed, 19 passed ==" bar
            break
        match = _SUMMARY_LINE.match(line)
        if match:
            # Split on " - " so a trailing "- AssertionError: ..." is dropped
            # without truncating a parametrised id that contains spaces.
            nodeids.append(match.group(1).split(" - ", 1)[0].strip())
    return nodeids, True


def _failure_detail(stdout: str) -> str:
    """The FAILURES (or ERRORS) block: everything up to the summary section.

    This is what lands in `TestResult.traceback`, and from there in the evidence
    the Implementer reads. It already contains expected-vs-actual, which is why
    `TesterFailure` does not parse that out separately.
    """
    lines = stdout.splitlines()

    start = None
    for index, line in enumerate(lines):
        if line.startswith("=") and ("FAILURES" in line or "ERRORS" in line):
            start = index
            break
    if start is None:
        return ""

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("=") and SUMMARY_HEADER in lines[index]:
            end = index
            break

    return "\n".join(lines[start:end]).strip()


class Tester:
    """Runs the suite in `repo_path` and reports the result.

    Contract, per CLAUDE.md: requires `repo_path`, produces `test_result`, must
    never read `plan` or `diff`. Declared and enforced by the agent base class
    in Phase 2, not restated here -- an unenforced copy would drift.
    """

    def __init__(self, event_log: EventLog, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.event_log = event_log
        self.timeout = timeout

    def run(self, repo_path: str, *, attempt: int = 0) -> TestResult:
        """Clear caches, run the whole suite, and report what pytest said."""
        directory = Path(repo_path)
        _clear_caches(directory)

        self.event_log.append(
            attempt=attempt,
            agent="tester",
            event="test_run_started",
            payload={"repo_path": str(directory), "timeout": self.timeout},
        )

        result = self._invoke_pytest(directory)

        self.event_log.append(
            attempt=attempt,
            agent="tester",
            event="test_run_completed",
            payload={"test_result": result},
        )
        return result

    def _invoke_pytest(self, directory: Path) -> TestResult:
        try:
            completed = subprocess.run(
                # sys.executable, so the subprocess uses the same interpreter and
                # virtualenv as the harness. A bare `pytest` could be anything.
                [sys.executable, "-m", "pytest"],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as expired:
            # Whatever pytest managed to print before it was killed is still
            # worth keeping -- it usually shows which test was running.
            partial = expired.stdout or ""
            return TestResult(
                passed=False,
                failed_tests=[],
                # True, not False: nothing finished, so there is no per-test
                # outcome set to have failed to read. The absence of a summary
                # here is honest rather than a parse failure, and the timeout is
                # already unmistakable from `exit_code` and `traceback`.
                summary_parsed=True,
                traceback=(
                    f"pytest timed out after {self.timeout} seconds and was killed. "
                    f"No exit code was produced."
                ),
                stdout=partial,
                exit_code=TIMEOUT_EXIT_CODE,
            )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        detail = _failure_detail(stdout)

        if completed.returncode == PYTEST_OK:
            # pytest prints no summary section when nothing failed. Its absence
            # is expected here, not a failure to read one.
            failed_tests, summary_parsed = [], True
        elif completed.returncode == PYTEST_TESTS_FAILED:
            failed_tests, summary_parsed = _parse_summary(stdout)
        else:
            # Interrupted, internal error, usage error, or nothing collected.
            # The suite never produced a per-test outcome set, so which tests
            # failed is unanswerable rather than empty.
            failed_tests, summary_parsed = [], False

        return TestResult(
            passed=completed.returncode == PYTEST_OK,
            failed_tests=failed_tests,
            summary_parsed=summary_parsed,
            # stderr is the fallback because a crash before the FAILURES block
            # exists still has to leave the Implementer something to read.
            traceback=detail or (stderr.strip() if completed.returncode != PYTEST_OK else ""),
            stdout=stdout,
            exit_code=completed.returncode,
        )


def tester_failure_from(result: TestResult) -> TesterFailure:
    """Package a failed `TestResult` as evidence for the Implementer.

    Standalone, and the Tester does not call it. The Tester reports; the harness
    decides that the report is a failure and writes `evidence` -- the same split
    as the Reviewer and `ReviewerRejection`, and consistent with the ownership
    table, where `evidence` is written by the harness alone.

    `stdout_tail` is truncated by `TesterFailure` itself, so the whole stdout can
    be handed over here and the cap holds wherever the model is constructed.
    """
    return TesterFailure(
        failed_tests=result.failed_tests,
        traceback=result.traceback,
        stdout_tail=result.stdout,
    )
