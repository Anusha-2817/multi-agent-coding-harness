"""Benchmark driver: runs arms A, B and C over the task set and scores each run.

`python driver.py --split dev` runs every (task, arm, repeat) not already in the
results file, scores it with `scorer.score_run`, and appends one row per run.
Not part of the harness; nothing in `harness/` imports it.

**Order.** Repeats are the outer loop, tasks the next, and within a task the arms
run in a shuffled order seeded by `(seed, task, repeat)` -- so the order is the
same on every resume, and stopping early leaves balanced results.

**What stops the driver, and what does not.**

| Exception | What happens |
| --- | --- |
| `LLMTransportError`, 429, 402 or 401, `retryable=False` (daily quota, credits, key) | stop cleanly; the cut-off run is not written |
| any other `LLMTransportError` | infrastructure: not written, redone on resume, capped per run |
| `LLMResponseError` (incl. truncated, refused) | a genuine outcome: scored as crashed, with the type |
| anything else, `AgentContractError` included | a harness bug: re-raised with its traceback |

The daily-quota test needs no harness change: `llm._transport_error` sets
`retryable=False` on a 429 only when the message names a per-day quota. Every
call a quota or infrastructure run spent is written to the incidents file.

**Safety.** The driver writes only under its tasks, runs and logs roots and the
results file. It refuses to start if any of those is inside a git repository, or
inside one of the four staged clones or the hidden-test directory, which it only
reads. Each task directory is built once with `git archive` and never written
afterwards, except that `discover_failure` -- the same step every arm's `main`
runs -- creates and then clears pytest caches in it.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from baseline import run_baseline
from cli import DEFAULT_DESCRIPTION, discover_failure
from harness.agents.tester import PYTEST_TESTS_FAILED
from harness.events import EventLog
from harness.llm import LLMClient, LLMError, LLMResponseError, LLMTransportError
from harness.state import TaskState, make_task_id
from harness.workspace import prepare_run_dir
from harness_arms import ArmEventLog, always_approve, run_arm
from llm_openrouter import OpenRouterClient
from scorer import read_events, score_run

EXPERIMENTS = Path(r"D:\Agentic_Project1\experiments")
BENCH = EXPERIMENTS / "bench"
ARMS = ("A", "B", "C")
#: Model calls per run, best and worst case: Planner once (B, C), then one
#: Implementer call per attempt, plus one Reviewer call per reviewed attempt (C).
CALLS_PER_RUN = {"A": (1, 5), "B": (2, 6), "C": (3, 11)}
INFRA_CAP = 3
#: Seconds between the starts of consecutive model calls. 12 s keeps a session
#: at or under 5 calls a minute.
MIN_CALL_INTERVAL = 12.0
STOPPED_EARLY = 3
BACKENDS = {"google": LLMClient, "openrouter": OpenRouterClient}
#: Non-retryable statuses that mean no further call can succeed this session,
#: with what the driver prints and the incident kind it records: Google's daily
#: quota (429), OpenRouter's exhausted credits (402), and a rejected key (401).
STOP_STATUSES = {
    429: ("daily quota exhausted", "quota"),
    402: ("credits exhausted", "quota"),
    401: ("API key rejected; check the key named in the error", "auth"),
}


@dataclass(frozen=True)
class Paths:
    manifest: Path = BENCH / "hidden" / "manifest.json"
    hidden: Path = BENCH / "hidden"
    tasks: Path = BENCH / "tasks"
    runs: Path = BENCH / "runs"
    logs: Path = BENCH / "logs"
    protected: tuple[Path, ...] = field(default_factory=lambda: (
        EXPERIMENTS / "schema",
        EXPERIMENTS / "xmltodict",
        EXPERIMENTS / "inflection",
        EXPERIMENTS / "git_intact" / "python-slugify",
        BENCH / "hidden",
    ))

    @property
    def bench(self) -> Path:
        """Where the manifest's relative `clone` paths resolve from."""
        return self.manifest.parent.parent


class QuotaExhausted(Exception):
    """The daily allowance is spent. Raised inside the driver only."""


class Pacer:
    """Enforces a minimum gap between the *starts* of consecutive model calls.

    Start to start, because a per-minute rate limit counts requests sent, not
    responses received. One pacer per driver session, so the gap holds across
    runs as well as within one. The clock and sleep are injectable so tests use a
    fake clock instead of waiting.

    What it cannot pace: retries the SDK makes inside one call, and the parse
    repair turn, which is a second request inside one `complete_structured`.
    """

    def __init__(self, interval: float, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.last_start: float | None = None

    def wait(self) -> None:
        if self.last_start is not None and self.interval > 0:
            remaining = self.interval - (self.clock() - self.last_start)
            if remaining > 0:
                self.sleep(remaining)
        self.last_start = self.clock()


class PacedClient:
    """A client wrapper: waits on the pacer, then delegates. Nothing else changes."""

    def __init__(self, inner: Any, pacer: Pacer) -> None:
        self.inner, self.pacer = inner, pacer

    def complete_structured(self, **kwargs: Any) -> Any:
        # The start is stamped before the call, so a call that raises still counts.
        self.pacer.wait()
        return self.inner.complete_structured(**kwargs)


# -- safety ------------------------------------------------------------------


def check_outputs(outputs: list[Path], protected: tuple[Path, ...]) -> None:
    """Refuse to start if anything the driver writes is in a git repo or a protected path."""
    for output in outputs:
        resolved = output.resolve()
        for guard in protected:
            if resolved == guard.resolve() or resolved.is_relative_to(guard.resolve()):
                raise SystemExit(f"refusing to start: {output} is inside protected {guard}")
        if _inside_git(resolved):
            raise SystemExit(f"refusing to start: {output} is inside a git repository")


def _inside_git(path: Path) -> bool:
    existing = next(p for p in (path, *path.parents) if p.exists())
    probe = subprocess.run(["git", "-C", str(existing), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


# -- tasks -------------------------------------------------------------------


def build_task(entry: dict[str, Any], paths: Paths) -> Path:
    """`tasks/<task>/` = the buggy commit plus the fix commit's test files. Built once."""
    target = paths.tasks / entry["task"]
    if target.exists():
        return target

    clone = str(paths.bench / entry["clone"])
    staging = paths.tasks / f".building_{entry['task']}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    archive = subprocess.run(["git", "-C", clone, "archive", "--format=tar", entry["buggy_commit"]],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(staging, filter="data")
    for relative in entry["hidden_tests"]:
        blob = subprocess.run(["git", "-C", clone, "show", f"{entry['fix_commit']}:{relative}"],
                              capture_output=True, check=True).stdout
        (staging / relative).parent.mkdir(parents=True, exist_ok=True)
        (staging / relative).write_bytes(blob)

    # Renamed into place only when complete, so a half-built task is never mistaken for one.
    staging.rename(target)
    return target


def select_tasks(manifest: list[dict], split: str, task_ids: list[str] | None) -> list[dict]:
    """The split's tasks, narrowed to `task_ids` when given, in manifest order.

    Narrowing only removes tasks: `plan` seeds each task's arm order by its own
    id, so a task runs its arms in the same order with or without `--task`, and
    resume keys are unchanged.
    """
    in_split = [t for t in manifest if t["split"] == split]
    if not task_ids:
        return in_split
    valid = [t["task"] for t in in_split]
    unknown = [task_id for task_id in dict.fromkeys(task_ids) if task_id not in valid]
    if unknown:
        raise SystemExit(f"unknown task id(s) for the {split} split: {', '.join(unknown)}. "
                         f"Valid ids: {', '.join(valid)}")
    return [t for t in in_split if t["task"] in task_ids]


def plan(tasks: list[dict], arms: list[str], repeats: int, seed: int) -> Iterator[tuple[dict, str, int]]:
    for repeat in range(repeats):
        for task in tasks:
            order = list(arms)
            random.Random(f"{seed}|{task['task']}|{repeat}").shuffle(order)
            for arm in order:
                yield task, arm, repeat


# -- results and incidents ---------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    return read_events(path)  # same tolerant reader: a bad line is skipped


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def incidents_path(results: Path) -> Path:
    return results.with_name(results.stem + ".incidents.jsonl")


def key_of(row: dict) -> tuple[str, str, int]:
    return row["task"], row["arm"], row["repeat"]


def _requests_in(log_path: Path) -> int:
    return sum(1 for e in read_events(log_path) if e.get("event") == "llm_request")


# -- one run -----------------------------------------------------------------


def run_one(entry: dict, arm: str, repeat: int, client: Any, paths: Paths) -> tuple[dict | None, Path]:
    """Run one arm on one task and score it. Returns (row, run log path).

    Quota and infrastructure errors propagate for the caller to record; a
    genuine response error is scored as crashed.
    """
    task_dir = build_task(entry, paths)
    task_id = f"arm{arm}_{make_task_id(entry['task'])}_{uuid.uuid4().hex[:6]}"
    log = (EventLog(task_id, log_dir=paths.logs) if arm == "A"
           else ArmEventLog(task_id, arm, log_dir=paths.logs))

    discovered = discover_failure(task_dir, log)
    if discovered.exit_code != PYTEST_TESTS_FAILED:
        raise RuntimeError(f"task {entry['task']} is not red (pytest exit {discovered.exit_code})")

    state = TaskState(
        task_id=task_id,
        repo_path=prepare_run_dir(task_id, task_dir, paths.runs),
        task_description=DEFAULT_DESCRIPTION,
        failure_input=discovered.stdout,
    )
    exception = None
    try:
        if arm == "A":
            run_baseline(state, fixture_path=task_dir, client=client, event_log=log,
                         approve=always_approve, runs_root=paths.runs)
        else:
            run_arm(arm, state, fixture_path=task_dir, client=client, event_log=log,
                    runs_root=paths.runs)
    except LLMTransportError as exc:
        exc.run_log = log.path
        raise
    except LLMResponseError as exc:  # truncated and refused are subclasses
        exception = type(exc).__name__

    row = score_run(task=entry, task_dir=task_dir, hidden_dir=paths.hidden / entry["task"],
                    run_dir=Path(state.repo_path), run_log=log.path,
                    score_log_dir=paths.logs / "score")
    row.update(arm=arm, arm_logged=row["arm"], repeat=repeat, exception=exception)
    return row, log.path


# -- the driver --------------------------------------------------------------


def drive(
    *,
    split: str,
    arms: list[str],
    repeats: int,
    seed: int,
    results: Path,
    max_calls: int | None = None,
    infra_cap: int = INFRA_CAP,
    min_call_interval: float = MIN_CALL_INTERVAL,
    pacer: Pacer | None = None,
    task_ids: list[str] | None = None,
    backend: str = "google",
    client_factory: Callable[[dict, str, int], Any] | None = None,
    paths: Paths = Paths(),
    out=print,
) -> int:
    check_outputs([results.parent, paths.tasks, paths.runs, paths.logs], paths.protected)

    tasks = select_tasks(json.loads(paths.manifest.read_text(encoding="utf-8")), split, task_ids)
    if client_factory is None:
        try:
            shared = BACKENDS[backend]()
        except LLMError as exc:  # a missing key: stop before any run, never echoing a key
            raise SystemExit(f"cannot start the {backend} backend: {exc}") from None
        client_factory = lambda task, arm, repeat: shared  # noqa: E731
    pacer = pacer or Pacer(min_call_interval)

    done_rows = read_jsonl(results)
    done = {key_of(row) for row in done_rows}
    infra: dict[tuple, int] = {}
    for incident in read_jsonl(incidents_path(results)):
        if incident["kind"] == "infrastructure":
            k = (incident["task"], incident["arm"], incident["repeat"])
            infra[k] = infra.get(k, 0) + 1

    todo = [(t, a, r) for t, a, r in plan(tasks, arms, repeats, seed)
            if (t["task"], a, r) not in done and infra.get((t["task"], a, r), 0) < infra_cap]
    given_up = sorted(k for k, n in infra.items() if n >= infra_cap and k not in done)
    low = sum(CALLS_PER_RUN[a][0] for _, a, _ in todo)
    high = sum(CALLS_PER_RUN[a][1] for _, a, _ in todo)
    out(f"{len(todo)} run(s) left on the {split} split; estimated {low}-{high} model calls.")
    for k in given_up:
        out(f"  skipped {k}: {infra_cap} infrastructure errors")

    last_versions = next((row["model_versions"] for row in reversed(done_rows)
                          if row.get("model_versions")), None)
    spent = 0
    for index, (entry, arm, repeat) in enumerate(todo):
        key = (entry["task"], arm, repeat)
        if max_calls is not None and spent + CALLS_PER_RUN[arm][1] > max_calls:
            out(f"stopping: the next run could exceed --max-calls {max_calls} ({spent} spent). "
                f"{len(todo) - index} run(s) left.")
            return STOPPED_EARLY

        try:
            client = PacedClient(client_factory(entry, arm, repeat), pacer)
            row, run_log = run_one(entry, arm, repeat, client, paths)
        except LLMTransportError as exc:
            wasted = _requests_in(exc.run_log)
            spent += wasted
            stop = STOP_STATUSES.get(exc.status_code) if not exc.retryable else None
            append_jsonl(incidents_path(results), {
                "task": key[0], "arm": arm, "repeat": repeat,
                "kind": stop[1] if stop else "infrastructure",
                "exception": f"{type(exc).__name__}: {exc}"[:500],
                "wasted_calls": wasted, "run_log": str(exc.run_log),
            })
            if stop:
                left = todo[index:]
                out(f"stopping: {stop[0]} (HTTP {exc.status_code}). "
                    f"{len(left)} run(s) left:")
                for t, a, r in left:
                    out(f"  {t['task']} arm {a} repeat {r}")
                return STOPPED_EARLY
            out(f"infrastructure error on {key}, not counted ({wasted} call(s) wasted): {exc}")
            continue

        spent += row["totals"]["requests"]
        versions = row["model_versions"]
        if versions and last_versions and versions != last_versions:
            out(f"WARNING: model_version changed from {last_versions} to {versions} at {key}")
        if versions:
            last_versions = versions
        append_jsonl(results, row)
        out(f"{key}: {row['status']}, resolved={row['resolved']}, {row['totals']['requests']} call(s)")

    out(f"done: {spent} model call(s) this session.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run benchmark arms over the task set.")
    parser.add_argument("--split", choices=("dev", "eval"), required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--results", type=Path, default=BENCH / "results" / "results.jsonl")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="google",
                        help="google (GEMINI_API_KEY) or openrouter (OPENROUTER_API_KEY)")
    parser.add_argument("--task", action="append", default=[],
                        help="task id from the manifest; repeat or comma-separate to run several")
    parser.add_argument("--min-call-interval", type=float, default=MIN_CALL_INTERVAL,
                        help="seconds between the starts of consecutive model calls (default 12)")
    args = parser.parse_args(argv)

    if args.min_call_interval < 0:
        parser.error("--min-call-interval must be >= 0")
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = sorted(set(arms) - set(ARMS))
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}")
    task_ids = [t.strip() for value in args.task for t in value.split(",") if t.strip()]
    return drive(split=args.split, arms=arms, repeats=args.repeats, seed=args.seed,
                 results=args.results, max_calls=args.max_calls,
                 min_call_interval=args.min_call_interval, task_ids=task_ids or None,
                 backend=args.backend)


if __name__ == "__main__":
    sys.exit(main())
