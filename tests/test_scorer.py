"""Tests for scorer.py. Stub clients only: no API calls and no key.

Each test drives a real arm run (A through `run_baseline`, C through `run_arm`)
against a task built from `fixture_repo_1`, whose `tests/test_orders.py` plays
the hidden upstream test. Then it scores the run. The Tester is real throughout.
"""

import json
import shutil
from pathlib import Path

import pytest
from fixture_edits import BUG, DISCOUNTS, baseline, failing_edits, fixing_edits
from test_baseline import ScriptedClient
from test_harness_arms import APPROVED, SchemaClient

from baseline import run_baseline
from harness.events import EventLog
from harness.llm import LLMTransportError
from harness.loop import MAX_ATTEMPTS
from harness.state import FileEdit, TaskState
from harness.workspace import IGNORED, prepare_run_dir
from harness_arms import ArmEventLog, run_arm
from scorer import CRASHED, score_run

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
HIDDEN_TEST = "tests/test_orders.py"
TARGET = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"
TASK_ID = "scored_20261013T120000Z"


@pytest.fixture
def bench(tmp_path):
    """A task directory, its hidden test, and its manifest entry."""
    task_dir = tmp_path / "task"
    shutil.copytree(FIXTURE, task_dir, ignore=IGNORED)
    hidden = tmp_path / "hidden"
    (hidden / "tests").mkdir(parents=True)
    shutil.copy2(task_dir / HIDDEN_TEST, hidden / HIDDEN_TEST)
    task = {
        "task": "fixture_1",
        "hidden_tests": [HIDDEN_TEST],
        "red_at_task": [TARGET],
        "src_files": [DISCOUNTS],
        "src_lines": 2,
    }
    return tmp_path, task_dir, hidden, task


def run_a(bench, responses):
    """Arm A to its end. An exception from the client ends the run, as it would live."""
    root, task_dir, _, _ = bench
    log = EventLog(TASK_ID, log_dir=root / "logs")
    state = TaskState(task_id=TASK_ID, repo_path=prepare_run_dir(TASK_ID, task_dir, root / "runs"),
                      task_description="Fix the failing test.", failure_input=TARGET)
    try:
        run_baseline(state, fixture_path=task_dir, client=ScriptedClient(responses), event_log=log,
                     approve=lambda diff: True, runs_root=root / "runs")
    except Exception:
        pass
    return log


def score(bench, log, prices=None):
    root, task_dir, hidden, task = bench
    kwargs = {"prices_path": prices} if prices else {}
    return score_run(task=task, task_dir=task_dir, hidden_dir=hidden, run_dir=root / "runs" / TASK_ID,
                     run_log=log.path, score_log_dir=root / "score_logs", **kwargs)


def edit_test(content: str) -> FileEdit:
    return FileEdit(path=HIDDEN_TEST, new_content=content)


class TestResolved:
    def test_a_correct_fix_is_resolved(self, bench):
        row = score(bench, run_a(bench, [fixing_edits()]))

        assert row["status"] == "succeeded"
        assert row["resolved"] is True
        assert row["targets_pass"] is True
        assert row["regressions"] == []
        assert row["collected_before"] == row["collected_after"] == 20
        assert row["collected_delta"] == 0
        assert row["arm"] == "baseline"
        assert row["flags"]["test_files_edited"] == []
        assert row["flags"]["upstream"]["same_files"] is True
        assert row["flags"]["upstream"]["run_src_lines"] == 2

    def test_arm_c_calls_are_counted_per_agent(self, bench):
        root, task_dir, _, _ = bench
        log = ArmEventLog(TASK_ID, "C", log_dir=root / "logs")
        state = TaskState(task_id=TASK_ID, repo_path=prepare_run_dir(TASK_ID, task_dir, root / "runs"),
                          task_description="Fix the failing test.", failure_input=TARGET)
        run_arm("C", state, fixture_path=task_dir, event_log=log, runs_root=root / "runs",
                client=SchemaClient(edits=[fixing_edits()], verdicts=[APPROVED]))

        row = score(bench, log)

        assert row["arm"] == "C" and row["resolved"] is True
        assert {agent: c["requests"] for agent, c in row["calls"].items()} == {
            "planner": 1, "implementer": 1, "reviewer": 1}
        assert row["totals"]["tokens"]["input_tokens"] == 300
        assert len(row["model_versions"]) == 1
        assert row["wall_time_s"] >= 0
        assert json.loads(json.dumps(row)) == row  # one JSONL row


class TestTestEdits:
    def test_disabling_the_test_goes_green_in_the_run_but_not_after_restore(self, bench):
        original = (FIXTURE / HIDDEN_TEST).read_text(encoding="utf-8")
        disabled = original.replace("def test_an_order_at_the_bulk_threshold_is_discounted",
                                    "def _disabled_bulk_threshold")
        row = score(bench, run_a(bench, [[edit_test(disabled)]]))

        assert row["status"] == "succeeded"  # green inside the run
        assert row["resolved"] is False  # red once the upstream test is back
        assert row["targets_pass"] is False
        assert row["flags"]["test_files_edited"] == [HIDDEN_TEST]
        assert row["flags"]["applied_test_paths"] == [HIDDEN_TEST]
        assert row["flags"]["upstream"]["same_files"] is False


class TestRegressions:
    def test_a_fix_that_breaks_other_tests_is_a_regression(self, bench):
        # Inclusive and off by one: the target passes, two other boundaries break.
        overshoot = [FileEdit(path=DISCOUNTS,
                              new_content=baseline().replace(BUG, "if quantity >= tier.min_quantity - 1:"))]
        row = score(bench, run_a(bench, [overshoot] * MAX_ATTEMPTS))

        assert row["status"] == "escalated_retry_limit"
        assert row["resolved"] is False
        assert row["targets_pass"] is True
        assert len(row["regressions"]) == 2
        assert TARGET not in row["regressions"]
        assert row["attempts"] == MAX_ATTEMPTS

    def test_a_deleted_test_file_shows_in_collected_delta(self, bench):
        emptied = FileEdit(path="tests/test_money.py", new_content="")
        row = score(bench, run_a(bench, [fixing_edits() + [emptied]]))

        assert row["suite_exit"] == 0  # what is left passes...
        assert row["collected_delta"] < 0
        assert row["resolved"] is False  # ...but removing tests is not a fix
        assert "tests/test_money.py" in row["flags"]["test_files_edited"]


class TestCrashes:
    def test_no_run_finished_is_crashed_even_when_the_disk_is_green(self, bench):
        log = run_a(bench, [fixing_edits()])
        lines = log.path.read_text(encoding="utf-8").splitlines()
        log.path.write_text("\n".join(l for l in lines if '"run_finished"' not in l) + "\n",
                            encoding="utf-8")

        row = score(bench, log)

        assert row["status"] == CRASHED
        assert row["suite_exit"] == 0  # the fix is on disk
        assert row["resolved"] is False

    def test_an_unmatched_llm_request_is_a_failed_call(self, bench):
        dropped = LLMTransportError("HTTP 503", status_code=503, retryable=True)
        row = score(bench, run_a(bench, [failing_edits(1), dropped]))

        assert row["status"] == CRASHED
        assert row["calls"]["baseline"] == {**row["calls"]["baseline"],
                                            "requests": 2, "responses": 1, "failed": 1}
        assert row["totals"]["failed"] == 1
        assert row["attempts"] == 2


class TestCost:
    def test_the_default_models_shipped_prices_are_null_so_cost_is_null(self, bench):
        from harness.llm import DEFAULT_MODEL

        shipped = json.loads((Path(__file__).resolve().parent.parent / "prices.json").read_text())
        assert all(rate is None for rate in shipped["models"][DEFAULT_MODEL].values())

        assert score(bench, run_a(bench, [fixing_edits()]))["cost_usd"] is None

    def test_the_shipped_38_price_is_the_dated_openrouter_listing(self):
        shipped = json.loads((Path(__file__).resolve().parent.parent / "prices.json").read_text())
        entry = shipped["models"]["google/gemini-3.8-flash"]

        assert entry["date"] == "2026-10-16" and "promotional" in entry["source"]
        assert (entry["input_per_mtok"], entry["cached_input_per_mtok"], entry["output_per_mtok"]) == (
            0.75, 0.075, 3.75)
        # Not given with the listing, so not invented: a run with thinking tokens
        # still costs null until this is filled in.
        assert entry["thinking_per_mtok"] is None

    def test_a_38_run_without_thinking_tokens_gets_a_cost(self):
        from scorer import cost, load_prices

        prices = load_prices(Path(__file__).resolve().parent.parent / "prices.json")
        events = [{"event": "llm_response", "payload": {"model": "google/gemini-3.8-flash", "usage": {
            "input_tokens": 1_000_000, "cache_read_input_tokens": 0, "output_tokens": 1_000_000,
            "thinking_tokens": 0}}}]

        assert cost(events, prices) == pytest.approx(0.75 + 3.75)

    def test_filled_prices_give_a_cost(self, bench, tmp_path):
        from harness.llm import DEFAULT_MODEL

        prices = tmp_path / "prices.json"
        prices.write_text(json.dumps({"source": "test", "date": "2026-10-13", "models": {
            DEFAULT_MODEL: {"input_per_mtok": 1.0, "cached_input_per_mtok": 0.5,
                            "output_per_mtok": 2.0, "thinking_per_mtok": 3.0}}}))

        row = score(bench, run_a(bench, [fixing_edits()]), prices=prices)

        # One call: 100 input, 50 output, 20 thinking (test_baseline.USAGE).
        assert row["cost_usd"] == pytest.approx((100 * 1.0 + 50 * 2.0 + 20 * 3.0) / 1_000_000)


class TestDiffModeFields:
    def test_a_full_run_scores_as_full_with_nothing_to_count(self, bench):
        row = score(bench, run_a(bench, [fixing_edits()]))

        assert row["edit_format"] == "full"
        assert (row["edit_apply_failures"], row["edit_repairs"]) == (0, 0)
        assert row["flags"]["test_path_apply_failures"] == []

    def test_a_diff_run_counts_repairs_failures_and_blind_test_edits(self, bench):
        from test_baseline import BLIND_TEST_BLOCK, FIX_BLOCK, DiffScriptedClient

        root, task_dir, _, _ = bench
        log = EventLog(TASK_ID, log_dir=root / "logs")
        state = TaskState(task_id=TASK_ID, repo_path=prepare_run_dir(TASK_ID, task_dir, root / "runs"),
                          task_description="Fix the failing test.", failure_input=TARGET)
        run_baseline(state, fixture_path=task_dir, edit_format="diff", event_log=log,
                     client=DiffScriptedClient([BLIND_TEST_BLOCK, BLIND_TEST_BLOCK, BLIND_TEST_BLOCK, FIX_BLOCK]),
                     approve=lambda diff: True, runs_root=root / "runs")

        row = score(bench, log)

        assert row["resolved"] is True and row["attempts"] == 2
        assert row["edit_format"] == "diff"
        assert row["edit_apply_failures"] == 1
        assert row["edit_repairs"] == 2  # one per attempt, whether or not it rescued anything
        assert row["totals"]["requests"] == 4  # the repair calls are counted as calls
        assert row["flags"]["test_path_apply_failures"] == ["tests/test_orders.py"]
