"""Tests for driver.py. Stub clients only: no API calls and no key.

Each test builds a tiny upstream repo from `fixture_repo_1` -- a buggy commit
without `tests/test_orders.py`, then a fix commit that adds it -- plus a manifest
and a hidden directory, all under `tmp_path`. The arms, the Tester and the scorer
are real; only the model is scripted.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fixture_edits import BUG, DISCOUNTS, FIX, fixing_edits

import driver
from driver import (ARMS, MIN_CALL_INTERVAL, STOPPED_EARLY, PacedClient, Pacer, Paths, build_task,
                    drive, main, plan)
from harness.agents.base import AgentContractError
from harness.agents.implementer import ImplementerResponse
from harness.llm import DEFAULT_MODEL, LLMResult, LLMTransportError, LLMTruncatedError
from harness.state import Plan, ReviewVerdict
from harness.workspace import IGNORED

FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
HIDDEN_TEST = "tests/test_orders.py"
TARGET = "tests/test_orders.py::test_an_order_at_the_bulk_threshold_is_discounted"
PLAN = Plan(summary="Inclusive lower bound.", steps=["Change > to >= in tier_for"],
            target_files=[DISCOUNTS], constraints=[])
APPROVED = ReviewVerdict(reason="hunk 1 is step 1", approved=True, violated_constraints=[])
DAILY = LLMTransportError("HTTP 429 per day", status_code=429, retryable=False)
UNAVAILABLE = LLMTransportError("HTTP 503", status_code=503, retryable=True)


class StubClient:
    """Answers by schema with a correct fix; raises `error` instead when set."""

    def __init__(self, *, model=DEFAULT_MODEL, error=None, error_on=None):
        self.model, self.error, self.error_on = model, error, error_on

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        if self.error is not None and (self.error_on is None or schema is self.error_on):
            raise self.error
        value = {Plan: PLAN, ImplementerResponse: ImplementerResponse(edits=fixing_edits()),
                 ReviewVerdict: APPROVED}[schema]
        return LLMResult(value=value, model=self.model, stop_reason="STOP", parse_attempts=1,
                         usage={"input_tokens": 10, "output_tokens": 5})


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    # The temp root can itself sit inside a git repo (a home directory under
    # version control does it), which the driver would rightly refuse. Stop git's
    # search at the test's own root, so only a repo the test makes is found.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "bench"
    clone = root / "clones" / "fix1"
    shutil.copytree(FIXTURE, clone, ignore=IGNORED)
    test_orders = (clone / HIDDEN_TEST).read_text(encoding="utf-8")
    (clone / HIDDEN_TEST).unlink()
    git(clone, "init", "-q")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "buggy")
    (clone / HIDDEN_TEST).write_text(test_orders, encoding="utf-8")
    discounts = clone / DISCOUNTS
    discounts.write_text(discounts.read_text(encoding="utf-8").replace(BUG, FIX), encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "fix")
    sha = lambda ref: subprocess.run(["git", "rev-parse", ref], cwd=clone, capture_output=True,  # noqa: E731
                                     text=True, check=True).stdout.strip()

    hidden = root / "hidden"
    (hidden / "fix1" / "tests").mkdir(parents=True)
    (hidden / "fix1" / HIDDEN_TEST).write_text(test_orders, encoding="utf-8")
    manifest = hidden / "manifest.json"
    manifest.write_text(json.dumps([{
        "task": "fix1", "split": "dev", "repo": "fix1", "clone": "clones/fix1",
        "buggy_commit": sha("HEAD^"), "fix_commit": sha("HEAD"), "src_files": [DISCOUNTS],
        "src_lines": 2, "hidden_tests": [HIDDEN_TEST], "red_at_task": [TARGET],
    }]), encoding="utf-8")

    paths = Paths(manifest=manifest, hidden=hidden, tasks=root / "tasks", runs=root / "runs",
                  logs=root / "logs", protected=(tmp_path / "staged", hidden))
    return paths, root / "results" / "results.jsonl"


class FakeClock:
    """Time that moves only when something sleeps. Records every sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def go(bench, factory=None, **kwargs):
    paths, results = bench
    lines: list[str] = []
    clock = FakeClock()  # never really sleep in a test
    options = {"split": "dev", "arms": list(ARMS), "repeats": 1, "seed": 0,
               "pacer": Pacer(MIN_CALL_INTERVAL, clock=clock, sleep=clock.sleep)} | kwargs
    code = drive(results=results, paths=paths, out=lines.append,
                 client_factory=factory or (lambda task, arm, repeat: StubClient()), **options)
    return code, lines


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def incidents(results: Path) -> list[dict]:
    return rows(results.with_name(results.stem + ".incidents.jsonl"))


class TestTaskBuild:
    def test_buggy_commit_plus_fix_tests_built_once(self, bench):
        paths, _ = bench
        entry = json.loads(paths.manifest.read_text())[0]

        task = build_task(entry, paths)
        assert BUG in (task / DISCOUNTS).read_text(encoding="utf-8")
        assert (task / HIDDEN_TEST).exists()
        assert not (task / ".git").exists()

        (task / "marker").write_text("x")
        assert build_task(entry, paths) == task
        assert (task / "marker").exists()  # skipped, not rebuilt


class TestOrderAndResume:
    def test_repeats_are_the_outer_loop_and_the_shuffle_is_seeded(self):
        tasks = [{"task": "t1"}, {"task": "t2"}]
        first = list(plan(tasks, list(ARMS), 2, seed=7))

        assert [(t["task"], r) for t, _, r in first] == (
            [("t1", 0)] * 3 + [("t2", 0)] * 3 + [("t1", 1)] * 3 + [("t2", 1)] * 3)
        assert all(sorted(a for t, a, r in first if (t["task"], r) == k) == list(ARMS)
                   for k in [("t1", 0), ("t2", 1)])
        assert first == list(plan(tasks, list(ARMS), 2, seed=7))

    def test_every_arm_runs_then_resume_skips_them(self, bench):
        _, results = bench
        code, lines = go(bench)

        assert code == 0
        assert lines[0] == "3 run(s) left on the dev split; estimated 6-22 model calls."
        written = rows(results)
        assert [r["arm"] for r in written] == [a for _, a, _ in plan([{"task": "fix1"}], list(ARMS), 1, 0)]
        assert all(r["resolved"] and r["repeat"] == 0 and r["exception"] is None for r in written)
        assert {r["arm_logged"] for r in written} == {"baseline", "B", "C"}

        code, lines = go(bench)
        assert code == 0 and lines[0].startswith("0 run(s) left")
        assert len(rows(results)) == 3


class TestQuota:
    def test_daily_quota_stops_cleanly_and_resume_finishes(self, bench):
        _, results = bench
        calls = {"n": 0}

        def factory(task, arm, repeat):
            calls["n"] += 1
            return StubClient(error=DAILY) if calls["n"] == 2 else StubClient()

        code, lines = go(bench, factory)

        assert code == STOPPED_EARLY
        assert len(rows(results)) == 1  # the cut-off run is not written
        assert any("daily quota exhausted (HTTP 429). 2 run(s) left" in line for line in lines)
        [incident] = incidents(results)
        assert incident["kind"] == "quota" and incident["wasted_calls"] == 1

        assert go(bench)[0] == 0
        assert len(rows(results)) == 3


    def test_exhausted_openrouter_credits_stop_like_the_daily_quota(self, bench):
        _, results = bench
        credits = LLMTransportError("HTTP 402", status_code=402, retryable=False)
        code, lines = go(bench, lambda task, arm, repeat: StubClient(error=credits))

        assert code == STOPPED_EARLY and rows(results) == []
        assert [i["kind"] for i in incidents(results)] == ["quota"]
        assert any("(HTTP 402). 3 run(s) left" in line for line in lines)


class TestBadKey:
    SECRET = "sk-or-test-SECRET-401"

    def test_a_rejected_key_stops_cleanly_without_printing_it(self, bench):
        import httpx
        from llm_openrouter import OpenRouterClient

        _, results = bench
        # The fake API even echoes the key back, the worst case for leaking it.
        api = lambda request: httpx.Response(  # noqa: E731
            401, json={"error": {"code": 401, "message": f"bad key {self.SECRET}"}})
        factory = lambda task, arm, repeat: OpenRouterClient(  # noqa: E731
            api_key=self.SECRET, transport=httpx.MockTransport(api))

        code, lines = go(bench, factory)

        assert code == STOPPED_EARLY and rows(results) == []
        assert any("API key rejected; check the key named in the error (HTTP 401). 3 run(s) left"
                   in line for line in lines)
        [incident] = incidents(results)
        assert incident["kind"] == "auth"
        assert "OPENROUTER_API_KEY" in incident["exception"]
        written = "\n".join(lines) + results.with_name(results.stem + ".incidents.jsonl").read_text()
        for log in bench[0].logs.rglob("*.jsonl"):
            written += log.read_text(encoding="utf-8")
        assert self.SECRET not in written

    def test_a_missing_key_stops_before_any_run(self, bench, monkeypatch):
        paths, results = bench
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="cannot start the openrouter backend: .*OPENROUTER_API_KEY"):
            drive(split="dev", arms=["A"], repeats=1, seed=0, results=results, paths=paths,
                  backend="openrouter", out=lambda s: None)
        assert rows(results) == [] and not paths.runs.exists()


class TestBackendFlag:
    def test_backend_picks_the_client_class(self, bench, monkeypatch):
        built = []

        class Fake(StubClient):
            def __init__(self):
                built.append(type(self).__name__)
                super().__init__()

        monkeypatch.setitem(driver.BACKENDS, "openrouter", Fake)
        paths, results = bench
        clock = FakeClock()
        drive(split="dev", arms=["A"], repeats=1, seed=0, results=results, paths=paths,
              backend="openrouter", pacer=Pacer(0, clock=clock, sleep=clock.sleep), out=lambda s: None)
        assert built == ["Fake"] and len(rows(results)) == 1

    def test_the_flag_defaults_to_google(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(driver, "drive", lambda **kw: seen.update(kw) or 0)
        main(["--split", "dev"])
        assert seen["backend"] == "google"
        main(["--split", "dev", "--backend", "openrouter"])
        assert seen["backend"] == "openrouter"
        with pytest.raises(SystemExit):
            main(["--split", "dev", "--backend", "anthropic"])


class TestInfrastructure:
    def test_a_503_is_not_written_and_is_redone_on_resume(self, bench):
        _, results = bench
        first = next(plan([{"task": "fix1"}], list(ARMS), 1, 0))[1]
        factory = lambda task, arm, repeat: StubClient(error=UNAVAILABLE if arm == first else None)  # noqa: E731

        code, lines = go(bench, factory)
        assert code == 0
        assert {r["arm"] for r in rows(results)} == set(ARMS) - {first}
        assert [i["kind"] for i in incidents(results)] == ["infrastructure"]
        assert any("not counted" in line for line in lines)

        go(bench)
        assert {r["arm"] for r in rows(results)} == set(ARMS)

    def test_the_cap_gives_up_on_a_run(self, bench):
        _, results = bench
        always = lambda task, arm, repeat: StubClient(error=UNAVAILABLE)  # noqa: E731

        go(bench, always, arms=["A"], infra_cap=2)
        go(bench, always, arms=["A"], infra_cap=2)
        code, lines = go(bench, always, arms=["A"], infra_cap=2)

        assert code == 0 and rows(results) == []
        assert len(incidents(results)) == 2
        assert lines[0].startswith("0 run(s) left")
        assert "skipped ('fix1', 'A', 0): 2 infrastructure errors" in lines[1]


class TestOutcomesAndBugs:
    def test_a_truncation_is_scored_as_crashed(self, bench):
        _, results = bench
        truncated = LLMTruncatedError("out of budget", stage="response", raw_text="")
        factory = lambda task, arm, repeat: StubClient(error=truncated, error_on=ImplementerResponse)  # noqa: E731

        assert go(bench, factory, arms=["A"])[0] == 0
        [row] = rows(results)
        assert row["status"] == "crashed" and row["resolved"] is False
        assert row["exception"] == "LLMTruncatedError"

    @pytest.mark.parametrize("error", [AgentContractError("implementer", "bad"), RuntimeError("bug")])
    def test_a_harness_bug_stops_with_the_traceback(self, bench, error):
        _, results = bench
        with pytest.raises(type(error)):
            go(bench, lambda task, arm, repeat: StubClient(error=error), arms=["A"])
        assert rows(results) == []


class TestLimitsAndWarnings:
    def test_max_calls_stops_before_a_run_could_exceed_it(self, bench):
        _, results = bench
        code, lines = go(bench, arms=["A"], repeats=3, max_calls=5)

        assert code == STOPPED_EARLY
        assert len(rows(results)) == 1  # 1 spent; another A could reach 6
        assert "2 run(s) left" in lines[-1]

    def test_a_model_version_change_warns_and_continues(self, bench):
        _, results = bench
        models = iter(["model-one", "model-two", "model-two"])
        code, lines = go(bench, lambda task, arm, repeat: StubClient(model=next(models)))

        assert code == 0 and len(rows(results)) == 3
        assert sum("WARNING: model_version changed" in line for line in lines) == 1


class TestPacing:
    def test_the_pacer_waits_only_for_what_is_left_of_the_interval(self):
        clock = FakeClock()
        pacer = Pacer(12, clock=clock, sleep=clock.sleep)

        pacer.wait()  # first call: nothing to wait for
        pacer.wait()  # immediately after: the whole interval
        clock.now += 20
        pacer.wait()  # 20 s later: already past it
        clock.now += 5
        pacer.wait()  # 5 s later: the remaining 7

        assert clock.sleeps == [12, 7]

    def test_a_call_that_raises_still_counts_as_a_start(self):
        clock = FakeClock()
        client = PacedClient(StubClient(error=UNAVAILABLE), Pacer(12, clock=clock, sleep=clock.sleep))
        for _ in range(2):
            with pytest.raises(LLMTransportError):
                client.complete_structured(system="", user="", schema=Plan, max_tokens=1)
        assert clock.sleeps == [12]

    def test_every_call_in_a_session_is_paced_across_runs(self, bench):
        clock = FakeClock()
        # A then C (or C then A): 1 + 3 = 4 calls, so 3 gaps of the full interval.
        go(bench, arms=["A", "C"], pacer=Pacer(12, clock=clock, sleep=clock.sleep))
        assert clock.sleeps == [12, 12, 12]

    def test_zero_interval_never_sleeps(self, bench):
        clock = FakeClock()
        go(bench, arms=["C"], pacer=Pacer(0, clock=clock, sleep=clock.sleep))
        assert clock.sleeps == []

    def test_the_flag_defaults_to_12_and_rejects_negatives(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(driver, "drive", lambda **kw: seen.update(kw) or 0)
        main(["--split", "dev"])
        assert seen["min_call_interval"] == 12
        main(["--split", "dev", "--min-call-interval", "0.5"])
        assert seen["min_call_interval"] == 0.5
        with pytest.raises(SystemExit):
            main(["--split", "dev", "--min-call-interval", "-1"])


def add_tasks(bench, extra: dict[str, str]) -> None:
    """More manifest entries on the same upstream repo: {task_id: split}."""
    paths, _ = bench
    manifest = json.loads(paths.manifest.read_text())
    for task_id, split in extra.items():
        manifest.append({**manifest[0], "task": task_id, "split": split})
        shutil.copytree(paths.hidden / "fix1", paths.hidden / task_id)
    paths.manifest.write_text(json.dumps(manifest))


class TestTaskFlag:
    def test_one_task_only(self, bench):
        _, results = bench
        add_tasks(bench, {"fix2": "dev"})

        code, lines = go(bench, task_ids=["fix2"])

        assert code == 0
        assert lines[0] == "3 run(s) left on the dev split; estimated 6-22 model calls."
        written = rows(results)
        assert {r["task"] for r in written} == {"fix2"}
        # Same arm order as fix2 gets in the unfiltered plan: narrowing changes no seeding.
        full = [a for t, a, _ in plan([{"task": "fix1"}, {"task": "fix2"}], list(ARMS), 1, 0)
                if t["task"] == "fix2"]
        assert [r["arm"] for r in written] == full

    def test_an_unknown_id_stops_and_lists_the_valid_ones(self, bench):
        add_tasks(bench, {"fix2": "dev"})
        called = []

        with pytest.raises(SystemExit, match=r"unknown task id\(s\) for the dev split: nope\. "
                                             r"Valid ids: fix1, fix2"):
            go(bench, lambda *a: called.append(a), task_ids=["fix1", "nope"])
        assert called == []  # stopped before any run

    def test_task_works_with_split(self, bench):
        _, results = bench
        add_tasks(bench, {"fix3": "eval"})

        # An eval task is not a dev task: the split still applies.
        with pytest.raises(SystemExit, match=r"for the dev split: fix3\. Valid ids: fix1$"):
            go(bench, task_ids=["fix3"])

        code, _ = go(bench, split="eval", task_ids=["fix3"], arms=["A"])
        assert code == 0
        assert [(r["task"], r["arm"]) for r in rows(results)] == [("fix3", "A")]

    def test_the_flag_takes_repeats_and_commas(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(driver, "drive", lambda **kw: seen.update(kw) or 0)
        main(["--split", "dev", "--task", "a,b", "--task", "c"])
        assert seen["task_ids"] == ["a", "b", "c"]
        main(["--split", "dev"])
        assert seen["task_ids"] is None


class TestSafety:
    def test_refuses_results_inside_a_git_repo(self, bench, tmp_path):
        paths, _ = bench
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        with pytest.raises(SystemExit, match="inside a git repository"):
            drive(split="dev", arms=["A"], repeats=1, seed=0, results=repo / "out" / "r.jsonl",
                  paths=paths, client_factory=lambda *a: StubClient(), out=lambda s: None)

    def test_refuses_outputs_inside_protected_paths(self, bench, tmp_path):
        paths, results = bench
        with pytest.raises(SystemExit, match="protected"):
            drive(split="dev", arms=["A"], repeats=1, seed=0, results=paths.hidden / "r.jsonl",
                  paths=paths, client_factory=lambda *a: StubClient(), out=lambda s: None)
        staged = Paths(manifest=paths.manifest, hidden=paths.hidden, tasks=tmp_path / "staged" / "t",
                       runs=paths.runs, logs=paths.logs, protected=paths.protected)
        with pytest.raises(SystemExit, match="protected"):
            drive(split="dev", arms=["A"], repeats=1, seed=0, results=results, paths=staged,
                  client_factory=lambda *a: StubClient(), out=lambda s: None)

    def test_hidden_dir_is_unchanged_by_a_run(self, bench):
        paths, _ = bench
        before = {p: p.read_bytes() for p in paths.hidden.rglob("*") if p.is_file()}
        go(bench, arms=["A"])
        assert {p: p.read_bytes() for p in paths.hidden.rglob("*") if p.is_file()} == before

    def test_default_paths_protect_the_staged_clones(self):
        names = {p.as_posix().split("experiments/")[-1] for p in Paths().protected}
        assert names == {"schema", "xmltodict", "inflection", "git_intact/python-slugify", "bench/hidden"}
        assert driver.BENCH.parent == driver.EXPERIMENTS


# -- diff mode -------------------------------------------------------------------

from harness.agents.implementer import DiffImplementerResponse, SearchReplace  # noqa: E402


class DiffStubClient(StubClient):
    """`StubClient`, with the Implementer and the baseline answering in edit blocks."""

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        if schema is DiffImplementerResponse:
            block = SearchReplace(path=DISCOUNTS, search=BUG, replace=FIX)
            return LLMResult(value=DiffImplementerResponse(edits=[block]), model=self.model,
                             stop_reason="STOP", parse_attempts=1,
                             usage={"input_tokens": 10, "output_tokens": 2})
        return super().complete_structured(system=system, user=user, schema=schema,
                                           max_tokens=max_tokens, thinking_level=thinking_level)


class TestEditFormat:
    def test_a_diff_session_runs_every_arm_and_stamps_every_row(self, bench):
        _, results = bench
        code, lines = go(bench, factory=lambda task, arm, repeat: DiffStubClient(), edit_format="diff")

        assert code == 0
        assert lines[0] == "3 run(s) left on the dev split; estimated 6-37 model calls."
        written = rows(results)
        assert len(written) == 3
        assert all(r["resolved"] and r["edit_format"] == "diff" for r in written)
        assert all(r["edit_apply_failures"] == 0 and r["edit_repairs"] == 0 for r in written)

    def test_full_rows_say_full(self, bench):
        _, results = bench
        go(bench, arms=["A"])

        assert [r["edit_format"] for r in rows(results)] == ["full"]

    def test_one_results_file_never_mixes_formats(self, bench):
        _, results = bench
        go(bench, arms=["A"])

        with pytest.raises(SystemExit, match="full-format rows"):
            go(bench, arms=["A"], edit_format="diff")
        assert len(rows(results)) == 1

    def test_a_row_from_before_diff_mode_counts_as_full(self, bench):
        _, results = bench
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text(json.dumps({"task": "x", "arm": "A", "repeat": 0}) + "\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="full-format rows"):
            go(bench, arms=["A"], edit_format="diff")

    def test_max_calls_uses_the_diff_worst_case(self, bench):
        """Full A's worst case is 5, so two would fit in 10; diff A's is 10."""
        _, results = bench
        code, _ = go(bench, factory=lambda task, arm, repeat: DiffStubClient(),
                     arms=["A"], repeats=2, max_calls=10, edit_format="diff")

        assert code == STOPPED_EARLY
        assert len(rows(results)) == 1  # 1 spent; another diff A could reach 11

    def test_the_flag_defaults_to_full(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(driver, "drive", lambda **kw: seen.update(kw) or 0)
        main(["--split", "dev"])
        assert seen["edit_format"] == "full"
        main(["--split", "dev", "--edit-format", "diff"])
        assert seen["edit_format"] == "diff"
        with pytest.raises(SystemExit):
            main(["--split", "dev", "--edit-format", "patch"])
