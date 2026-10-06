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

from harness.agents.base import Agent
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

# CSI sequences -- `ESC [ ... <final byte>`. pytest's colour output is all SGR
# (`\x1b[31m`, `\x1b[0m`), but the class is matched whole rather than narrowed to
# `m`: a stray cursor-move or erase-line would be just as invisible to a parser
# anchored at column zero, and narrowing buys nothing.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Return `text` with ANSI CSI escape sequences removed.

    **Task 6.5, fixing 6.2's finding F4.** A target repo can force colour on in
    its own pytest config — `python-slugify` carries `addopts = "--color=yes"`,
    which colours output even when stdout is not a tty, and the Tester always
    captures via `subprocess`. Measured on a real capture, committed as
    `tests/data/coloured_pytest_output.txt`: 528 escape sequences in 6,129
    characters.

    Both parsers below anchor at column zero: `_parse_summary` looks for a line
    that `startswith("=")` and matches rows against `^(?:FAILED|ERROR)`, and
    `_failure_detail` looks for the same `=` bar. Coloured lines begin
    `\\x1b[36m\\x1b[1m====` and `\\x1b[31mFAILED`, so every anchor misses, no
    summary section is found, and `_parse_summary` reports
    `([], summary_parsed=False)` on an ordinary test failure. The loop reads that
    as `escalated_broken_suite` — "pytest could not collect" — which is a
    **misrouted status**, not merely a lost list: a failing attempt escalates
    instead of retrying, so on any colour-forcing repo the retry path is
    unreachable.

    **This is applied to a copy, and the raw bytes are kept.** `TestResult.stdout`
    stays exactly what pytest wrote, so the event log keeps the real output and
    CLAUDE.md's "a run's log is self-contained" claim is unaffected. Stripping is
    for the harness's own reading, not a correction of what the target emitted.

    The alternative was passing `--color=no` to the subprocess. Rejected: the
    target's `addopts` would still be appended after our flag on some pytest
    versions, it changes the invocation rather than our parsing of it, and the
    Tester's job is to report what the suite actually said. Reading the output
    correctly is our problem to solve on our side.
    """
    return _ANSI_CSI.sub("", text)


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


class Tester(Agent):
    """Runs the suite in `repo_path` and reports the result.

    Its contract lives in `CONTRACTS["tester"]`, not here: requires `repo_path`,
    produces `test_result`. The "must never read `plan` or `diff`" half needs no
    declaration at all -- `_run` is handed `repo_path` and nothing else, so there
    is no plan in scope to be tempted by.

    `timeout` is an extra constructor argument, not an extra `run` argument. The
    public signature stays `run(state) -> TaskState` like every other agent; how
    long this one is willing to wait for a subprocess is its own business.
    """

    name = "tester"

    def __init__(self, event_log: EventLog, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(event_log)
        self.timeout = timeout

    def _run(self, *, repo_path: str) -> TestResult:
        """Clear caches, run the whole suite, and report what pytest said.

        Only `test_run_started` is logged here. The result is carried by the base
        class's `agent_produced` event, so logging a completion event too would
        put the same `TestResult` in the log twice.
        """
        directory = Path(repo_path)
        _clear_caches(directory)

        self.log(
            event="test_run_started",
            payload={"repo_path": str(directory), "timeout": self.timeout},
        )

        return self._invoke_pytest(directory)

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

        # Parsed from a de-coloured copy; `stdout` below is still the raw bytes.
        # See `strip_ansi` -- a target that forces colour in its own `addopts`
        # otherwise defeats every column-zero anchor in both parsers.
        plain_stdout = strip_ansi(stdout)
        detail = _failure_detail(plain_stdout)

        if completed.returncode == PYTEST_OK:
            # pytest prints no summary section when nothing failed. Its absence
            # is expected here, not a failure to read one.
            failed_tests, summary_parsed = [], True
        elif completed.returncode == PYTEST_TESTS_FAILED:
            failed_tests, summary_parsed = _parse_summary(plain_stdout)
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
            # exists still has to leave the Implementer something to read. It is
            # de-coloured for the same reason `detail` is: both fill this one
            # field, and leaving the treatment to depend on which branch filled
            # it would put escapes in front of a model on the crash path only.
            traceback=detail
            or (strip_ansi(stderr).strip() if completed.returncode != PYTEST_OK else ""),
            # Raw, deliberately. The log keeps what pytest actually wrote.
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
