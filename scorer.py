"""Scores one finished benchmark run. Not part of the harness.

`score_run(...)` returns one JSON-serialisable row, in the shape the driver
appends to a results JSONL file. It is arm-agnostic: A, B and C logs use the
same event names, so one reader covers all three.

The order of operations is the point:

1. **Snapshot before anything is restored.** The run directory is compared
   against a fresh copy of the task, giving the final patch and which test files
   the run changed. What is on disk depends on how the run ended -- the reset is
   at the top of an attempt, so a halt before apply leaves the untouched task.
2. **Restore the hidden tests** over the run directory: the upstream copy of the
   fix commit's test files, so a run that edited a test cannot change its score.
3. **Run the suite** with the real Tester, logging to a separate score log so the
   run's own log stays its record.

`resolved` is the suite exiting 0 after the restore. It needs no test names, so
F9 cannot fool it. It also requires `collected_delta >= 0`: a run that removes
tests is not a fix, even when what is left passes. The "before" side (`collected_before`, the baseline failures
that `regressions` is measured against) is measured, not read from the manifest:
the same suite is run on a scratch copy of the pristine task.

A run with no `run_finished` is `crashed`, and `crashed` is never resolved,
whatever is on disk. Test-file edits and the comparison with the upstream fix
are flags, never part of the score.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.agents.planner import _is_test_file
from harness.agents.tester import Tester, _clear_caches, strip_ansi
from harness.events import EventLog
from harness.workspace import IGNORED, list_repo_files, normalize_path

PRICES = Path(__file__).resolve().parent / "prices.json"
CRASHED = "crashed"
USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_input_tokens")

# The outcome words in pytest's final "== 3 failed, 19 passed in 0.4s ==" bar
# that each stand for one collected test. "subtests passed" is not one of them.
_OUTCOME = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b")


def score_run(
    *,
    task: dict[str, Any],
    task_dir: str | Path,
    hidden_dir: str | Path,
    run_dir: str | Path,
    run_log: str | Path,
    score_log_dir: str | Path = "logs",
    prices_path: str | Path = PRICES,
) -> dict[str, Any]:
    """Score one run. `task` is the run's entry from `hidden/manifest.json`."""
    run_dir, task_dir, hidden_dir = Path(run_dir), Path(task_dir), Path(hidden_dir)
    events = read_events(run_log)
    started = _first(events, "run_started")
    finished = _first(events, "run_finished")
    score_log = EventLog(f"score_{run_dir.name}", log_dir=score_log_dir)

    # 1. Snapshot, before the restore touches anything.
    patch = snapshot(task_dir, run_dir)

    # 2-3. Restore the hidden tests and run the suite.
    for relative in task["hidden_tests"]:
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hidden_dir / relative, target)
    after = _suite(run_dir, score_log)
    before = _baseline(task_dir, score_log)

    status = finished["payload"]["status"] if finished else CRASHED
    named = after["summary_parsed"] and not (after["exit_code"] != 0 and not after["failed"])
    failed_after = set(after["failed"])

    calls = calls_by_agent(events)
    delta = _delta(before["collected"], after["collected"])
    return {
        "task": task["task"],
        "arm": started["payload"].get("arm") if started else None,
        "run_id": run_dir.name,
        "run_log": str(run_log),
        "score_log": str(score_log.path),
        "status": status,
        "resolved": (status != CRASHED and after["exit_code"] == 0
                     and delta is not None and delta >= 0),
        "suite_exit": after["exit_code"],
        "targets_pass": not (failed_after & set(task["red_at_task"])) if named else None,
        "regressions": sorted(failed_after - set(before["failed"])) if named else None,
        "collected_before": before["collected"],
        "collected_after": after["collected"],
        "collected_delta": delta,
        "attempts": _attempts(events, finished),
        "model_versions": sorted({e["payload"]["model"] for e in events
                                  if e["event"] == "llm_response" and e["payload"].get("model")}),
        "wall_time_s": _wall_time(events),
        "calls": calls,
        "totals": _totals(calls),
        "cost_usd": cost(events, load_prices(prices_path)),
        "flags": {
            "test_files_edited": patch["test_files"],
            "applied_test_paths": _applied_test_paths(events),
            "final_patch": patch,
            "upstream": {
                "src_files": task["src_files"],
                "src_lines": task["src_lines"],
                "same_files": sorted(patch["src_files"]) == sorted(task["src_files"]),
                "run_src_lines": patch["src_lines"],
            },
        },
    }


# -- the run directory -------------------------------------------------------


def snapshot(task_dir: Path, run_dir: Path) -> dict[str, Any]:
    """What the run left on disk, against the pristine task. Caches and .git ignored."""
    before, after = set(list_repo_files(task_dir)), set(list_repo_files(run_dir))
    changed = sorted(
        path for path in before | after
        if path not in before or path not in after
        or (task_dir / path).read_bytes() != (run_dir / path).read_bytes()
    )
    lines = {path: _changed_lines(task_dir / path, run_dir / path) for path in changed}
    src = [path for path in changed if not _is_test_file(path)]
    return {
        "changed": changed,
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "test_files": [path for path in changed if _is_test_file(path)],
        "src_files": src,
        "src_lines": sum(lines[path] for path in src),
        "lines": lines,
    }


def _changed_lines(old: Path, new: Path) -> int:
    def read(path: Path) -> list[str]:
        return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []

    return sum(
        1 for line in difflib.unified_diff(read(old), read(new), lineterm="")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def _suite(directory: Path, log: EventLog) -> dict[str, Any]:
    try:
        result = Tester(log)._run(repo_path=str(directory))
    finally:
        _clear_caches(directory)
    return {
        "exit_code": result.exit_code,
        "failed": result.failed_tests,
        "summary_parsed": result.summary_parsed,
        "collected": collected(result.stdout),
    }


def _baseline(task_dir: Path, log: EventLog) -> dict[str, Any]:
    """The same suite on a scratch copy of the pristine task, which is never written."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "task"
        shutil.copytree(task_dir, copy, ignore=IGNORED)
        return _suite(copy, log)


def collected(stdout: str) -> int | None:
    """Tests pytest accounted for in its final bar, or None if there is no bar."""
    bars = [line for line in strip_ansi(stdout).splitlines() if line.startswith("=")]
    for bar in reversed(bars):
        counts = _OUTCOME.findall(bar)
        if counts:
            return sum(int(number) for number, _ in counts)
    return None


def _delta(before: int | None, after: int | None) -> int | None:
    return None if before is None or after is None else after - before


# -- the run log -------------------------------------------------------------


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Every readable event, oldest first. A bad line is skipped, as in `cli.read_events`."""
    events = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def _first(events: list[dict], name: str) -> dict | None:
    return next((e for e in events if e.get("event") == name), None)


def calls_by_agent(events: list[dict]) -> dict[str, dict[str, Any]]:
    """Requests, responses and token sums per agent. A request with no response failed.

    Calls within one agent are sequential, so the count difference is the number
    of requests that never came back -- a transport error, which logs nothing
    after its `llm_request`. A truncated call does log its `llm_response`.
    """
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") not in ("llm_request", "llm_response"):
            continue
        entry = calls.setdefault(event["agent"], {"requests": 0, "responses": 0, "failed": 0,
                                                  "tokens": dict.fromkeys(USAGE_KEYS, 0)})
        if event["event"] == "llm_request":
            entry["requests"] += 1
        else:
            entry["responses"] += 1
            for key in USAGE_KEYS:
                entry["tokens"][key] += event["payload"].get("usage", {}).get(key, 0)
    for entry in calls.values():
        entry["failed"] = entry["requests"] - entry["responses"]
    return calls


def _totals(calls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": sum(c["requests"] for c in calls.values()),
        "failed": sum(c["failed"] for c in calls.values()),
        "tokens": {key: sum(c["tokens"][key] for c in calls.values()) for key in USAGE_KEYS},
    }


def _attempts(events: list[dict], finished: dict | None) -> int:
    if finished:
        return finished["payload"]["attempt_count"]
    return max((e.get("attempt", 0) for e in events), default=0)


def _wall_time(events: list[dict]) -> float | None:
    stamps = [datetime.fromisoformat(e["ts"]) for e in events if "ts" in e]
    return round((max(stamps) - min(stamps)).total_seconds(), 3) if stamps else None


def _applied_test_paths(events: list[dict]) -> list[str]:
    """Test paths in any `edits_applied`, classified here so A, B and C agree."""
    paths = {normalize_path(p) for e in events if e.get("event") == "edits_applied"
             for p in e["payload"].get("paths", [])}
    return sorted(p for p in paths if _is_test_file(p))


# -- cost --------------------------------------------------------------------


def load_prices(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cost(events: list[dict], prices: dict[str, Any]) -> float | None:
    """USD for every `llm_response`, or None if any price the run needs is missing."""
    total = 0.0
    for event in events:
        if event.get("event") != "llm_response":
            continue
        rates = prices.get("models", {}).get(event["payload"].get("model"))
        if rates is None:
            return None
        usage = {key: event["payload"].get("usage", {}).get(key, 0) for key in USAGE_KEYS}
        parts = {
            "input_per_mtok": usage["input_tokens"] - usage["cache_read_input_tokens"],
            "cached_input_per_mtok": usage["cache_read_input_tokens"],
            "output_per_mtok": usage["output_tokens"],
            "thinking_per_mtok": usage["thinking_tokens"],
        }
        for rate_name, tokens in parts.items():
            if not tokens:
                continue
            rate = rates.get(rate_name)
            if rate is None:
                return None
            total += tokens * rate / 1_000_000
    return round(total, 6)
