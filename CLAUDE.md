# CLAUDE.md

Persistent project memory. Read this fully at the start of every session before touching code.

---

## What this project is

A framework-free multi-agent coding harness. Four role-pure agents — Planner, Implementer, Reviewer, Tester — collaborate through a typed state object to fix a deliberately injected bug in a small fixture repo, with retry-on-failure and a human-approval gate before any write to disk.

**Fixed task class for v1:** given a repo with one failing test, fix it without breaking anything else. Not open-ended feature work.

The point of this project is the _harness_ — the control loop, state handoffs, and recovery — not the model. Judge every decision by whether it makes the loop more understandable, not more capable.

---

## Hard scope fence

These are documented-only. Do NOT build them, do NOT scaffold them, do NOT add "just in case" hooks for them:

- FastAPI wrapper or any HTTP layer
- Streamlit / rich / textual UI
- Docker
- SQLite index over the event log
- Log rotation
- Concurrency, parallelism, async
- LangGraph / LangChain / CrewAI / any agent framework
- A second task mode (feature implementation)
- Routing rejections back to the Planner (v2 — v1 detects and escalates only)
- Git worktrees
- TaskState snapshotting

If a task seems to need one of these, stop and ask rather than building it.

**One allowed seam:** the human-approval gate is an injectable callback, so the harness's own tests can drive it. That is the only injection point in v1. It exists because approval is on the critical path of every attempt and is otherwise untestable — not as a precedent for making other collaborators pluggable.

### The one dependency, and where it stops

Phase 3 adds `google-genai` to `requirements.txt`. That is not a hole in the fence, and the reasoning matters more than the outcome.

The fence bans **agent frameworks** — LangGraph, LangChain, CrewAI — because they would build the control loop, and the control loop is the entire project. A provider HTTP client builds nothing of the sort. Hand-rolling `urllib`, exponential backoff with jitter, and status-code retry classification would cost a session and demonstrate nothing about agentic systems: it is plumbing every project needs and no project should write twice.

So the boundary is drawn at what the dependency would be *doing for us*:

- **Delegated** — connection handling, HTTP retries with exponential backoff and jitter, typed error classes. `harness/llm.py` configures `retry_options` and `timeout` on the SDK client and lets it do that job.
- **Hand-rolled, in `harness/llm.py`** — the three-step parse ladder from response text to a JSON value, the repair turn, and the failure taxonomy that decides which failures are worth retrying, which are worth repairing, and which are worth neither.

That split is also why the client asks for `response_json_schema` and parses the text itself rather than reading the SDK's `response.parsed`. `parsed` would do the extraction and validation for us, and those two steps are exactly what this phase exists to understand. Choosing the harder path deliberately is what keeps this a principled boundary rather than a convenient exception.

**The provider moved once and the argument did not.** 3A was built on `anthropic`; 3A.1 swapped it for `google-genai` because the Anthropic API needs prepaid credits this project does not have and Gemini's free tier covers fixture-sized runs. Nothing above is a different claim than it was — see "The transport swap" for what it cost.

**The four agents are parameters of `run_task`, and that is not a second seam.** They are the loop's operands, not a hidden pluggability point: `cli.py` chooses stubs or real ones from `--stub`, and the loop never learns which it got. A `stub: bool` flag would not work anyway — loop tests must hand in *specific scripts* (`failing_edits(1), failing_edits(2), fixing_edits()`), and a boolean cannot express those. The distinction that matters: `approve` is a seam because it stands in for a human, whereas the agents were always going to be passed in by someone.

---

## Phases

Build order. Phases 1–2 make **zero API calls**.

| Phase | Contents | Status |
| ----- | -------- | ------ |
| 0 | Scaffold, CLAUDE.md, first commit | done |
| 1 | `TaskState` + all models, `EventLog`, fixture repo 1, Tester, unit tests | done |
| 2 | Agent base class with requires/produces assertion, stub agents, control loop (routing, retry, livelock, escalation), the approval gate and apply step, scripted failure tests | in progress — 2.1, 2.2, 2.3 done |
| 3 | LLM client wrapper, real Planner, real Implementer, real Reviewer, `--stub`/`--real` switch | done — 3A (client, Planner, Implementer, `cli.py`), 3A.1 (Anthropic → Gemini), 3B (Reviewer, `--stub`) |
| 4 | Failure evidence packaging, retry with evidence, escalation output | done — evidence packaging and retry-with-evidence were already built in 2.3/3A; 4A adds the escalation output, 4B `fixture_repo_2`, 4C the run against it (green on attempt 1, trap did not spring) |
| 5 | Fixture repo 3, Reviewer-rejection scenario, saved transcripts, README update — `fixture_repo_2` moved into Phase 4, which needed it for the recovery run | in progress — 5A (fixture 3, `probe_reviewer.py`), 5.1 (probe pass **8/8**, end-to-end run), 5B (`probe_implementer.py`, **5/5**) |
| 6 | Export, measure, then fix what actually breaks — generalise the CLI, then run against real repos and let the failures pick the next task | in progress — 6.0.5 + 6.1 (auto-discovery, `--repo`), 6.2 (first real repo: four findings, green end to end), 6.3 (F1 fixed), 6.4 (F1 confirmed on a real `.git`), 6.5 (F4 fixed: ANSI stripped before parsing) |

**Phase 5 note.** Now that the scope check runs first, a diff touching an out-of-scope file never reaches the Reviewer — it is caught mechanically and routed back. So the Reviewer-rejection scenario can no longer be built from an out-of-scope edit. Exercising the Reviewer's *judgment* requires a diff that **stays inside `target_files` but is unfaithful to the plan** — right files, wrong change: overreaching within an allowed file, solving a different problem, or gutting behaviour the plan meant to preserve. Design fixture repo 2 or 3 with that in mind.

See also "Fixture design: the recoverability constraint" — Phase 5 now owes **two** fixtures with different jobs, and this is only one of them.

That fixture is now carrying two questions, not one. It is the only thing that can show whether the 3B prompt's anti-rubber-stamp guards actually fire, and it is the **revisit trigger** for the Reviewer's `plan`+`diff` contract — see "The Reviewer". Both were argued rather than measured, and both should be decided on what that fixture produces.

The Tester lands in Phase 1, before any agent scaffolding, because it is the only agent that needs no LLM and because a working Tester is what makes Phase 2's scripted failures verifiable.

**The approval gate and the apply step moved from Phase 3 to Phase 2.3.** The original table was wrong, not revised: the loop cannot be tested without them. With no apply, the Tester runs against an untouched baseline, every attempt is red, and nothing can ever go green — the happy path, "fail twice then succeed", and the `summary_parsed=False` escalation all become unreachable. The gate is on the critical path of every attempt for the same reason. Phase 3 keeps the three LLM-backed agents and the client wrapper, which is what it was really about.

---

## Non-negotiable invariants

1. **No diff reaches disk without a Review verdict AND a human approval on that specific diff.** Per diff, never per task. Attempt 5 gets the same gate as attempt 1. No fast paths.
2. **The Reviewer never sees test results.** There is no `test_result` in its input contract. Blind review is what forces it to judge scope and intent instead of free-riding on the Tester's verdict.
3. **The Tester never sees the plan.** It reports what pytest says. Nothing else.
4. **Every attempt starts from a fresh copy of the fixture.** The run directory is deleted and re-copied before each attempt, so every diff is against identical ground.
5. **One attempt counter.** Reviewer rejections and Tester failures share it. Cap: 5.
6. **Every step appends to the JSONL event log.** No silent state changes.

---

## Order of operations

```
Plan → [reset] → Implement → [no-edits check] → [scope check] → [render diff] → [livelock check]
                     ↑                                                                │
                     │                                                                ▼
                     │                          Review → [human approval] → Apply → Test
                     │                                                                │
                     └──────── reset to baseline (re-copy fixture) ───────────────────┘
                               (on no edits, scope violation, rejection, or test failure)
```

Review happens **before** apply. A rejected diff never touches disk. The permission boundary is drawn at reversibility.

The no-edits check, scope check, render, and livelock check are harness steps, not agents. They sit between Implement and Review so that an empty, out-of-scope, or duplicate diff is caught before spending a review on it. They are ordered by cost: the no-edits check is a length test, the scope check compares strings and needs no rendered diff, and only then is a diff rendered for the livelock check to compare.

The reset is drawn at the **top** of each attempt rather than on the return edge. Same invariant, but one call site instead of four, so no failure path can forget one. It runs on the first attempt too, over a directory `cli.py` has just prepared — one redundant copy, in exchange for invariant 4 being structural rather than an agreement between branches.

---

## Fixtures and the run directory

Fixtures are **plain directories with no git**. `tasks/fixture_repo_1/` is ordinary files plus a hand-authored `task.json`.

At run start the harness copies the fixture to `runs/<task_id>/` with `shutil.copytree`, ignoring `__pycache__`, `.pytest_cache` and **`.git`**. All work — apply, test, reset — happens in the copy. The fixture directory itself is never written to.

`.git` joined that list in Task 6.3, on the evidence of 6.2's F1 — it is not a cache, and the shared constant is now `_EXCLUDED_DIR_NAMES` rather than `_CACHE_DIR_NAMES` so the name does not lie about what it holds. **A repository's history is not part of the code under test**, and nothing in the harness reads it: the Planner is shown source, the Implementer replaces whole files, the Tester runs pytest, and reset is a re-copy rather than a `git checkout` — a decision made in Phase 1 and never revisited.

**One qualification, added in 6.0.5 and enforced rather than merely stated.** `cli.discover_failure` runs a pytest subprocess *inside* the target repo, before the copy, so pytest writes `__pycache__` and `.pytest_cache` there during that run. It clears both on the way out, in a `finally`, and the repo is left as it was found. The claim above is therefore intact — and it is intact because `test_tester.py` already enforces it: `test_a_run_leaves_no_caches_in_the_fixture` and `test_the_fixture_directory_is_untouched_by_a_run` both went red the first time discovery ran without the cleanup, which is how the omission was caught rather than shipped. The residue worth naming: a *pre-existing* `.pytest_cache` in the target repo is deleted, so pytest's own `--lf` state there is lost. That is regenerable derived data, and the pre-clear is load-bearing — a stale cache is what produces a phantom result.

**Baseline reset = delete `runs/<task_id>/` and re-copy from the fixture.** No git, no patch reversal, no cleanup logic to get wrong. `runs/` is gitignored.

This replaces an earlier `git checkout . && git clean -fd` design. A copy is simpler than a nested git repo inside the harness repo, needs no submodule or init script, and makes "identical ground" a property of the filesystem rather than of git's index state.

### Keeping the two pytest suites apart

The harness has a test suite and so does every fixture. They must never collect together — a fixture's deliberate bug is *data*, and reporting it as a harness failure would make the harness suite permanently red.

Two ini files, pulling in opposite directions:

- **`mach/pytest.ini`** sets `testpaths = tests` and adds `tasks` and `runs` to `norecursedirs`. `testpaths` covers a bare `pytest`; `norecursedirs` covers `pytest .` and explicit directory arguments. Its other job is pinning rootdir to `mach/` — with no ini file here, pytest walks up and roots itself in the parent of the repo. Note that setting `norecursedirs` *replaces* pytest's defaults rather than extending them, so the defaults are restated in the file; dropping them would send collection into `.venv` and `.git`.
- **`tasks/fixture_repo_1/pytest.ini`** gives the fixture its own rootdir. This is the load-bearing one. When the Tester runs pytest inside `runs/<task_id>/`, pytest walks up looking for an ini file and **stops at the fixture's own** — so rootdir is the run directory, and because `confcutdir` defaults to rootdir, the harness's `conftest.py` is never loaded either. The fixture is tested in isolation from the harness that is testing it. It also carries `pythonpath = .`, so `import pricing` resolves without an installed package or a `conftest.py` shim.

Every fixture repo needs its own `pytest.ini` for the same reason.

### task.json

`task.json` contains exactly two hand-authored fields: `task_description` and `failure_input`. Nothing else.

**As of 6.0.5 a `task.json` is no longer the only way in, and the rule it was built on has been made precise rather than dropped.** The original wording said the harness does not run pytest to discover the failure before planning, because that would put the Tester ahead of the Planner. What that rule actually protects is the *loop's order of operations*: the Tester must not be folded into the Plan → Implement → Review → Test sequence out of order. `discover_failure` runs before `TaskState` is constructed and before `run_task` is called, so it is not in that sequence at all — it is setup, not planning. Inside the loop, Plan still runs first, always. See "Auto-discovering the failure" below for the whole argument.

The failure text is still an input to the task rather than a product of the loop. What changed is who types it.

`failure_input` is the **real, verbatim** pytest output, captured by running the suite — never written from memory and never cleaned up. That includes the platform banner, the pytest and plugin version numbers, and the machine-specific absolute `rootdir:` line. **Do not sanitize it here, and do not sanitize it in future fixtures.**

Real failure reports are noisy, and the noise is part of the input. A Planner that only works once a human has trimmed the header off a traceback is overfit to the fixtures — it would be reading a format the harness manufactured rather than the one pytest actually emits, and that gap would not show up until the harness met a report from anywhere else.

The stale `rootdir:` is the clearest case. It points at `tasks/fixture_repo_1`, but at runtime the Tester works in `runs/<task_id>/` and its output will say so. A Planner that trips over that discrepancy has a defect worth finding in Phase 3, not a fixture worth editing.

### task_id

`task_id` is **generated by the harness**, not read from `task.json`. It identifies a *run*, not a task: it is both the `runs/` directory name and the log filename, so two runs against the same fixture must produce different ids.

Format: `<repo_dir_name>_<UTC timestamp>` — e.g. `fixture_repo_1_20260814T120000Z`. The timestamp is compact rather than full ISO 8601 because it becomes a path segment and ISO's colons are illegal in Windows filenames. Built by `make_task_id()` in `harness/state.py`.

**6.1 checked `make_task_id` for a fixture-era assumption and found the assumption was the caller's, not the function's.** It takes a directory name and appends a timestamp; nothing in it knows what a fixture is. The parameter was named `fixture_dir_name`, which was a false claim once any repo could be passed, so it is now `repo_dir_name` — a rename and a docstring, no logic. What *was* genuinely broken was the call site: `--repo .` would have made `.name` the string `"."` and the run directory a hidden `runs/._2026...`, and a trailing separator or a filesystem root would have emptied it. `cli.resolve_repo` resolves the path before `.name` is taken, and `cli.repo_dir_name` falls back to `"repo"` for a nameless root. That belongs in the caller: a directory *name* is this function's input, not a path it should be parsing.

**`prepare_run_dir` needed nothing.** It `copytree`s whatever path it is handed and ignores the two cache directories; it has never known or cared that the path was one of three fixtures. Checked, and deliberately not touched. (A real repo will bring `.git` and `.venv` along with it — that is a Phase 6.2 finding waiting to happen, not a fixture-era assumption, so it is recorded here and left alone.)

CLI, as of 6.1:

```
python cli.py --repo <any/path> [--description "<text>"] [--failure "<verbatim pytest output>"] [--stub]
python cli.py --task tasks/fixture_repo_1/task.json [--stub]          # still works
```

`--repo` and `--task` are mutually exclusive and one is required. `--description` and `--failure` belong with `--repo` and are **refused** rather than ignored when passed with `--task` — a `task.json` already carries both fields, so one of the two would have to win silently, and a flag that is accepted and then discarded is worse than one that is rejected.

`--task` is kept working rather than kept alive: the three fixture demos are cited as evidence throughout this file, and a documented command that no longer runs is worse than one flag's worth of branching in `main`.

No YAML. JSON only. `cli.py` resolves the repo, discovers or reads the failure, generates the `task_id`, prepares the run directory, builds the `TaskState`, constructs the four agents, and supplies the human at the gate. It validates that `task.json` holds *exactly* the two hand-authored fields at the boundary — a typo'd key should name the file it is in, not surface three steps later as a contract violation inside an agent.

### Auto-discovering the failure

`cli.discover_failure(repo_path, event_log) -> TestResult`. Task 6.0.5. Before it, a human hand-pasted verbatim pytest output into a `task.json`; a CLI coding tool does not ask for that, it looks at the repo and finds out what is wrong itself.

**Why this is not the Tester jumping the queue.** The Phase 1 rule bans running pytest to discover the failure *as part of planning the fix*. This runs before `TaskState` exists and before `run_task` is called — it is not in the Plan-Implement-Review-Test sequence at all. Setup, not planning. The loop's order is untouched, and so are the agents, the contracts, and the six invariants: 6.0.5 and 6.1 together changed `cli.py`, plus a two-line rename in `state.py`.

**It runs against the target repo, not the run directory, and that is the load-bearing choice.** `failure_input`'s rule is that it is verbatim — banner, plugin versions, and the machine-specific absolute `rootdir:` line that points at the *source* repo while the Tester will later work in `runs/<task_id>/`. This file calls that stale path "the clearest case" of noise worth keeping. Discovering against the copy would produce a correct `rootdir:` and quietly sanitize exactly the discrepancy a Planner is supposed to be robust to — a cleaner input than any hand-captured fixture ever had, which is the opposite of the point.

**It calls `Tester._run`, not `Tester.run`, and that is a decision rather than a shortcut.** `Agent.run` takes a `TaskState`, and `TaskState` requires `failure_input` at construction — which is the value this function exists to compute. Driving the public entry point would mean building a state around a placeholder failure and then overwriting it: a lie told to get past a constructor, and one that would sit in the log. `_run` *is* the pytest-invocation logic — clear caches, log `test_run_started`, invoke pytest, report. The contract check skipped along the way asserts only that `repo_path` is present, which is the one argument being passed by hand. `_clear_caches` is imported from the same module for the same reason: a second copy of those two directory names in `cli.py` would be free to drift from the one the loop uses.

**Three outcomes, and only one of them is a task:**

| pytest exit code | What it means | What `main` does |
| --- | --- | --- |
| 0 | green | report plainly, **exit 0**, invent nothing |
| 1 | tests failed | `failure_input = result.stdout`, verbatim |
| 2–5, or the timeout's −1 | no per-test outcome set | refuse, and point at `--failure` |

The green case exits **0**, not 1: "nothing is broken" is a true answer to the question that was asked, not a failure to answer it. Nothing is copied to `runs/` and no API call is made.

The last row draws the same line `summary_parsed` draws inside the loop, where it halts with `escalated_broken_suite` rather than retrying, and for the same reason — another diff cannot fix a suite that will not collect. Refusing here saves a stack of API calls that were going to be spent on an unanswerable question, and `--failure` is the documented way through for a failure that is not a clean pytest run.

**`--description` defaults and does not guess.** `DEFAULT_DESCRIPTION` is a generic `"Fix the failing test(s) in this repository."`. Auto-discovery automates *evidence-gathering*, not intent; a default that guessed at intent would be inventing the half a human still owns.

**The terminal gate takes the diff and nothing else**, per the ownership table. The note under "The control loop" imagined `cli.py` showing the plan and the verdict alongside it. The Reviewer is real as of 3B, so the old reason to wait has expired — but the reason to stay narrow has not: the callback is built before `run_task` runs and so before any plan or verdict exists, and widening to `approve(diff, review)` would change `run_task`'s signature for a display convenience. Do it when someone at the gate actually wants it, not because it became possible. An unreadable stdin (a pipe, a CI job, `< /dev/null`) is read as a refusal: a gate whose failure mode is "approve" is not a gate.

**At 3A the human was the only real gate** — invariant 1 needs a Review verdict *and* a human approval, and the Reviewer was a stub approving every diff. As of 3B both gates are real on the default path. Under `--stub` the human is again the only one, which is what the flag's name says.

### `--stub`

Added in 3B, when it finally had something coherent to switch. It means **all three LLM-backed agents, or none.** Real is the default; `--stub` opts out.

What it does **not** switch, and both omissions are load-bearing:

- **The Tester.** Real since Phase 1, never stubbed. A stub run's verdict on the suite is a real pytest verdict.
- **The approval gate.** `StubApprover` exists so *tests* can reach the apply step. A flag that let the shipped entrypoint skip the human would be the exact shape invariant 1 exists to prevent — the same argument that keeps `write_edits` in `tests/`. A stub run still stops at a terminal and asks.

**A stub run needs no API key, and that is most of the point.** `LLMClient` is constructed only on the real path, so `--stub` runs when `GEMINI_API_KEY` is unset or the day's quota is gone. What it is for is checking that the wiring still holds — `loop.py`, `workspace.py`, the gate, the event log, the real Tester — without spending a request. The key check keeps its old placement, before anything is copied.

**A stub run cannot succeed, and says so.** The scripted agents decide nothing, so they cannot fix a bug they were never told about. `stub_agents` reads the run directory, targets the first non-test `.py` file, and scripts five edit lists that append `# stub attempt n` to it — the `failing_edits(n)` shape, generalised. The suite stays red, so the run walks every step — reset, no-edits check, scope check, render, livelock, review, gate, apply, Tester, evidence, retry — and halts at `escalated_retry_limit`. The marker is load-bearing for the reason it is in `fixture_edits.py`: without it, two attempts would render byte-identical diffs and halt on livelock at attempt 2, short of the retry path this exists to exercise. Answering `n` at the first gate ends it sooner, having still exercised everything up to apply.

The plan it scripts targets **exactly the one file the stub Implementer edits**, not every source file. A plan naming everything would make the scope check vacuous, and the point of a stub run is that every step does its real work.

**The rejected alternative** was a per-fixture canned script that *does* fix the bug, so a stub run could reach `succeeded`. It would put each fixture's fix in a second place — either hardcoded in `cli.py`, where it would silently misbehave on fixtures 2–3, or in a new per-fixture artifact. That is the duplication `tests/fixture_edits.py` reads the fixture to avoid. Being fixture-agnostic and ending red is the better trade: it works on fixture repos 2 and 3 the day they are added.

**`run_started` did not grow a field for it.** Which agents ran is already unambiguous from the log — the stubs write `stub_scripted`, the real agents write `llm_request` — so the mode is printed in the CLI banner for the person at the terminal and the harness event table is untouched.

`cli.stub_targets` uses a cruder test-file rule than `planner._is_test_file`, deliberately unshared. The Planner's version decides what a model is shown and might be tempted to edit; this one only has to pick a file that exists and that appending a comment to cannot break. Sharing would make a real decision answer to a placeholder's needs.

---

## Field ownership (TaskState)

Agents read and write specific fields. They do not share a chat log.

| Field                                                       | Written by                          | Read by                       |
| ----------------------------------------------------------- | ----------------------------------- | ----------------------------- |
| `task_id`, `repo_path`                                      | harness (init, generated)           | Planner                       |
| `task_description`, `failure_input`                         | harness (init, from `task.json`)    | Planner                       |
| `plan`                                                      | Planner                             | Implementer, Reviewer         |
| `edits`                                                     | Implementer                         | harness (render, apply)       |
| `diff`                                                      | harness (rendered from `edits`)     | Reviewer, human approval gate |
| `review`                                                    | Reviewer                            | harness (routing)             |
| `test_result`                                               | Tester                              | harness (routing)             |
| `evidence`                                                  | harness                             | Implementer                   |
| `attempt_count`, `previous_diffs`, `status`                 | harness                             | harness                       |

### Edits, not patches

The Implementer produces `edits: list[FileEdit]`, where `FileEdit = {path: str, new_content: str}` — **full file replacement, not a patch.** The harness renders `diff` from `edits` plus the baseline file contents using `difflib.unified_diff`.

Models are unreliable at emitting valid unified diffs — hunk headers and line offsets have to be exactly right or `git apply` rejects the whole thing, and the failure mode is a hard error unrelated to whether the fix was correct. Full replacement moves that burden to `difflib`, which is deterministic. The Reviewer still reviews a real unified diff; it just isn't the model that produced it.

The Reviewer reads the **rendered diff**, not `edits`. Minimality is a property of the change, and the diff is the only view that shows it.

### Plan

```python
Plan: {summary: str, steps: list[str], target_files: list[str], constraints: list[str]}
```

`target_files` is **load-bearing, not documentation.** An edit to a path outside `target_files` is a mechanical scope violation the harness detects on its own — before the Reviewer runs, alongside the render and livelock steps. The Reviewer still judges faithfulness and minimality; it simply is no longer the only line of defence on scope. On a violation the harness writes a `ReviewerRejection` and routes back to the Implementer — see "The scope check reuses ReviewerRejection".

`constraints` is what the Reviewer's `violated_constraints` refers back to. Without it the Reviewer would be reporting violations of a rubric nobody wrote down.

**`target_files` must be non-empty**, enforced by a field validator on `Plan` as of 3A. An empty list is structurally valid and semantically catastrophic: it puts *every* possible edit outside scope, so the loop burns all five attempts on the scope check and halts with `escalated_retry_limit` while the log reads as though the Implementer kept going outside its plan — the model blamed for a plan that made success unreachable. The validator earns its keep twice over now that `Plan` is LLM-produced: the failure arrives as a `ValidationError` naming the field, and the client's repair turn hands that message straight back to the Planner.

`steps` and `constraints` are deliberately still allowed to be empty. Only `target_files` is load-bearing, and a one-line fix legitimately has no constraints worth writing down.

### TestResult

```python
TestResult: {passed: bool, failed_tests: list[str], summary_parsed: bool, traceback: str, stdout: str, exit_code: int}
```

`failed_tests` holds pytest **nodeids** (`tests/test_calculator.py::test_add`). `traceback` is the empty string on a pass.

#### summary_parsed

`failed_tests == []` on its own conflates *"nothing failed"* with *"something failed and we could not tell which."* `summary_parsed` splits them, typed rather than inferred — the same refusal to collapse two states that makes `edits` `Optional` instead of defaulting to `[]`.

It is **False only when a per-test outcome set was expected and could not be read**: collection errors and internal crashes. Not on a pass, where pytest prints no summary section at all and its absence is expected. **Not on a timeout** — nothing finished, so there is no outcome set to have failed to read; the timeout is already unmistakable from `exit_code` and `traceback`.

The Tester decides this from pytest's **exit code**, not from whether the summary header was found. Only 0 (ok) and 1 (tests failed) come with a trustworthy outcome set; 2, 3, 4 and 5 do not. This matters more than it looks: pytest *does* print a `short test summary info` section for a collection error, containing `ERROR tests/test_broken.py` — a bare **file path, not a nodeid**. Header-presence gating would set `summary_parsed=True` and put that path into `failed_tests`, a field the Implementer's prompt will eventually read. Exit-code gating keeps non-nodeids out by construction.

**`summary_parsed=False` should escalate, not retry.** A suite that will not collect is not fixable by another diff from the Implementer — there is no failing test to aim at, and the retry would burn an attempt producing a change no one can evaluate. Not implemented here; it is a Phase 2 loop concern.

Deliberately a superset of what `TesterFailure` needs, because the **harness** — not the Tester — derives the evidence from it, the same way it derives `ReviewerRejection` from a rejecting verdict. `stdout` is kept whole here and truncated to `stdout_tail` only at the evidence boundary, so the event log retains the full run output even when the Implementer sees only the tail.

### Agent contracts

| Agent       | requires                                            | produces      | must never read       |
| ----------- | --------------------------------------------------- | ------------- | --------------------- |
| Planner     | `repo_path`, `task_description`, `failure_input`    | `plan`        | `diff`, `test_result` |
| Implementer | `repo_path`, `plan` (+ `evidence` on retry)         | `edits`       | `test_result`         |
| Reviewer    | `plan`, `diff`                                      | `review`      | `test_result`         |
| Tester      | `repo_path`                                         | `test_result` | `plan`, `diff`        |

This is the enforcement half of role purity. Prompts are suggestions; missing fields are guarantees.

**`repo_path` reaches the Planner and the Implementer as of 3A**, and it is a correction rather than a widening. Both were incoherent without it.

The Implementer's contract is *full file replacement*: it cannot emit `new_content` for a file it has never read. The only alternative would be asking a model to reconstruct a file from a description, which is not a plumbing problem this project should invent for itself.

The Planner is the sharper case. It has to produce `target_files`, which the loop enforces mechanically — but `failure_input` is a pytest traceback that stops at the failing *test*, and `fixture_repo_1`'s never mentions `pricing/discounts.py` at all. Neither does `task_description`. A Planner guessing wrong would burn all five attempts on the scope check while the log claimed the model kept going outside its plan: exactly the plumbing-versus-model ambiguity Phase 3 exists to remove.

This does not weaken the "must never read" column. `repo_path` is harness-owned, already in the ownership table, and is neither `diff` nor `test_result`. Reading the repository under test is not reading another agent's output — the Tester has done it since Phase 1.

**The contracts live in one table, `CONTRACTS` in `harness/agents/base.py`.** No agent restates its own. Four entries side by side is how role purity gets checked — you read the whole thing at once and see that `test_result` appears in exactly one `produces` and no `requires`. Per-subclass declarations would scatter the same information across four files, each copy free to drift from this one. A subclass sets `name` and nothing else.

`Contract` is `{requires: tuple, produces: str, optional: tuple}`. `produces` is a single field name, not a tuple — every agent writes exactly one field, and a tuple would invite one that writes two. `optional` is how "`plan` (+ `evidence` on retry)" is expressed: passed to the agent whether or not it is set, never asserted, so the signature does not change shape between a first attempt and a retry.

### How the two halves are enforced

**`requires`** — `Agent.run` checks the fields are present and non-None before the agent runs, and raises `AgentContractError` naming the agent and **every** missing field at once. Not a bare `assert`: `python -O` strips those, and this is an invariant, not a debug aid.

**"must never read"** — enforced by omission, not by a rule. `run` calls the subclass's `_run` with exactly `requires + optional` and nothing else, so the Reviewer's `_run(*, plan, diff)` has no `test_result` in scope to be tempted by. Invariant 2 holds by construction rather than by the Reviewer's good manners. This also makes the contract self-checking: `run` binds the signature before calling, so a subclass whose parameters have drifted from the table fails on its first call rather than silently reading the wrong thing. Binding rather than catching `TypeError` around the call keeps a contract mismatch distinct from a genuine `TypeError` raised inside the agent.

**`produces`** — the base class writes the field; `_run` only returns a value, and no agent is in a position to write a field it does not own. Returning `None` is a contract violation too: an agent that ran and produced nothing has broken its contract as surely as one called without its inputs.

`AgentContractError` is fatal and nothing catches it. A violation means the loop routed to an agent whose inputs are not ready — a harness bug, not a task outcome. `Status` has no value for it on purpose: the run does not end in a state worth recording, it ends in a stack trace worth reading.

### The agent signature

```python
Agent.__init__(self, event_log: EventLog)      # collaborators go here
Agent.run(self, state: TaskState) -> TaskState  # public, uniform, never overridden
Agent._run(self, *, <contracted fields>) -> <produced value>  # what a subclass writes
```

The `EventLog` is a constructor dependency, which is what resolves the Tester's apparent asymmetry — it isn't special, it just got built first. Anything else an agent needs goes the same way: the Tester's `timeout`, the LLM client in Phase 3.

`run` **returns a new state and never mutates.** The control loop owns state transitions, and an agent mutating in place would make any before/after pair in the event log a record of the same object twice.

The new state is built with `TaskState.model_validate(...)`, not `model_copy(update=...)` — `model_copy` does not validate, so a subclass returning the wrong type would land it in state unchecked and surface much later with nothing pointing back. Revalidation also covers `list[FileEdit]`, where an `isinstance` check would not: a list of the wrong thing is still a list.

`attempt` is read from `state.attempt_count`, never passed. `Agent.log(event=, payload=)` stamps a subclass's own domain events with it. Planner events therefore carry `attempt=0`, since the counter increments on the Implementer.

The base class logs `agent_produced` (payload keyed by the produced field name) and `contract_violation` (logged before the raise, so the log shows why the run died). Subclasses log their own domain events on top and must not re-log the produced value — the Tester logs `test_run_started` and lets `agent_produced` carry the `TestResult`.

---

### Stub agents

`harness/agents/stubs.py` holds `StubPlanner`, `StubImplementer`, `StubReviewer` — the three LLM-backed roles. The Tester is real from Phase 1 and is never stubbed.

All three have real counterparts as of 3B, and these did **not** become dead code. They are what `test_loop.py` drives, and as of 3B they are also what `--stub` wires — the same objects, scripted by `cli.stub_agents` instead of by a test.

**A stub is scripted, not smart.** Each takes a sequence of return values and hands out one per call, indexed by call count, deciding nothing. A `StubImplementer` that read `evidence` and "fixed itself" on the third attempt would turn every loop test into a test of the stub's cleverness instead of the loop's routing — the assertion would still pass if the loop had routed nothing back at all.

Two capabilities beyond returning a value, both there so loop tests can assert something:

- **`seen`** records the kwargs each call received. "Retry with evidence" is otherwise untestable: you can see a second attempt happen, but not that the failure was carried into it.
- **`ScriptExhausted`** when the script runs out. A loop that ran six times against a five-entry script must fail loudly; repeating the last entry would let a runaway loop look like a passing test. `StubPlanner` takes a sequence too, even though v1 plans exactly once — a one-entry script makes "the loop never replans" an assertion rather than a hope.

Stubs decide nothing about success, either. The **real Tester** does, by running real pytest against whatever the edits produced. So "fail twice, then succeed" is expressed as three edit lists, not as a flag.

### StubApprover

Also in `stubs.py`, but **not an `Agent`**: the approval gate is a callback, not a role. It has no contract, reads no state, and writes no field — the loop hands it a diff and gets a `bool` — so it shares `Script` with the agent stubs and nothing else.

It exists because the gate is the one genuine seam in v1. Without a double here no loop test reaches the apply step at all, and `aborted_by_human` is unreachable.

`seen` records the diffs it was shown. That is how a test proves invariant 1 the whole way through — that the bytes the human approved are the bytes the Reviewer saw and the bytes that reached disk — rather than merely that some approval happened somewhere.

### Edit generators for the fixture

`tests/fixture_edits.py` builds those edit lists by reading the fixture's `discounts.py` and substituting into it — never by inlining a module body. An inlined copy would be a second source of truth for the fixture and would rot the first time the fixture changed, in a way no test would catch: the copy would still compile and still fail.

`failing_edits(n)` leaves the bug in place and appends a `# attempt n` marker. The marker is load-bearing. Every attempt starts from an identical baseline, so two edits that both merely leave the bug render **byte-identical** diffs — which trips the livelock check on attempt 2 and halts a run the test meant to send around the retry path. Successive failing variants must differ byte-wise; passing the *same* marker twice is how a test asks for a livelock, and there is no separate generator for it.

`fixing_edits()` applies the one-character fix. `broken_edits()` leaves the file unparseable, which is the only way to reach the `summary_parsed=False` branch — the Implementer cannot otherwise produce a suite that will not collect.

`out_of_scope_edits()` edits `pricing/money.py`, a real fixture file no plan targets, and is what drives the mechanical scope check. Note what it deliberately lacks: an `attempt` marker. The failing variants need one because they render diffs that must differ byte-wise, but a scope-violating attempt is caught *before* the render and contributes nothing to `previous_diffs` — so two identical out-of-scope edits cannot livelock, and the marker would imply otherwise.

All are verified end to end through the real Tester: red is really red, green is really green. A generator that quietly produced two green suites would make every "fail twice, then succeed" test pass for the wrong reason.

---

## Review verdict

```python
ReviewVerdict: {reason: str, approved: bool, violated_constraints: list[str]}
```

The Reviewer produces the verdict. The **harness** converts a rejection into a `ReviewerRejection` and writes it to `evidence` — the Reviewer never writes `evidence` itself, consistent with the ownership table.

**`reason` is declared before `approved` as of 3B, and the order is the point.** This model goes over the wire as `response_json_schema`, and property order in the schema is the order the model emits its fields in. Verdict-first has it commit to yes or no and then write a justification for a decision already made; reason-first makes it walk the diff against the plan and arrive at the verdict. It is the cheapest guard there is against a Reviewer that approves everything, and it costs nothing — no code reads these fields by position, Pydantic models are keyword-only at construction, and `extra="forbid"` and validation are unaffected.

### Who writes what into `violated_constraints`

Two writers, and they do not agree. That is deliberate, and the resolution is to name them rather than to force consistency that is not reachable:

- **The Reviewer** puts verbatim entries from `plan.constraints` there, and nothing else. It is a list of the plan's own rubric, not a second place to write prose — the reason field is where the prose goes. An empty list on a rejection is normal: most rejections are about attribution or correspondence, and a plan is allowed to list no constraints at all.
- **The harness's scope check** puts the offending **paths** there, which are not constraints in any sense. See "The scope check reuses `ReviewerRejection`" — that reuse exists because the Implementer's corrective action is identical, and the field was the only place the paths could go.

Consistency between the two is not available: one writer has a rubric to quote and the other has a path to report. The **event log** is what distinguishes them — `scope_check_failed` against `review_rejected` — and provenance is a question for the log, not for the Implementer's prompt.

---

## Failure evidence types

Two distinct types, never a generic `error: str` — the Implementer must know which kind of wrong it was.

```python
ReviewerRejection: {kind: Literal["reviewer_rejection"], reason: str, violated_constraints: list[str]}
TesterFailure:     {kind: Literal["tester_failure"], failed_tests: list[str], traceback: str, stdout_tail: str}
```

`evidence` holds the **single most recent** failure, not a history — a discriminated union on `kind`. The literal tag exists so the union round-trips through the JSONL log without ambiguity.

`ReviewerRejection` deliberately has no `suggested_fix`. The Reviewer says what is wrong; the Implementer decides what to do about it. Otherwise the Reviewer ends up grading its own homework on the next attempt.

### The scope check reuses ReviewerRejection

When the mechanical scope check fires, the harness constructs a **`ReviewerRejection`** with the offending paths in `violated_constraints`. There is no third evidence type.

`ReviewerRejection` means *"rejected on scope/intent grounds,"* not *"the Reviewer said so."* The Implementer's retry handling should not have to distinguish a cheap mechanical catch from an expensive judged one — the corrective action is identical, and a third type would fork the retry path for no gain.

The **event log** does distinguish them: `scope_check_failed` and `review_rejected` are separate event names. Provenance is a question for the log and for debugging, not for the Implementer's prompt.

`TesterFailure` deliberately has no parsed `expected_vs_actual`. The traceback already contains it, and parsing pytest output is brittle. `stdout_tail` is capped at 2000 characters.

### Measuring the retry path directly: `probe_implementer.py`

**The same "wrong instrument" argument as `probe_reviewer.py`, applied one agent over.** CLAUDE.md has said since 3A that the retry against a real model is the one thing still unexercised: no model has ever received a `TesterFailure` in its prompt, so `render_evidence` has never produced a byte a model read, and `RESET_NOTICE` is an argued claim. Reaching that path through the loop means passing through the Planner first — and `fixture_repo_2` is exactly what that costs. Its trap was proven mechanically, the Planner fenced it off before the Implementer saw it, the run went green in three calls, and the retry was never reached.

A full run also **conflates the two agents even when it works**: a green attempt 2 could be the Implementer reading the evidence, or the Planner having written a plan good enough that the evidence was never needed. The probe holds the plan fixed and hand-constructs the failure, so the only variable left is what the Implementer does with what it is handed.

**The plan cost nothing.** `probe_plan_fixture_1.json` is lifted verbatim from `logs/fixture_repo_1_20260823T064826Z.jsonl`, the last real fixture-1 run. Re-fetching would spend a call on a plan already saved; inventing one would measure the Implementer against a rubric no Planner wrote. So the probe is **one call, not two** — unlike the Reviewer probe, which had no cached plan for its new fixture.

**The hand-constructed failure is `>` overcorrected to `>= tier.min_quantity - 1`.** Inclusive, and off by one. Three properties, all verified before any call was spent:

- **It is one character from correct.** The plan asked for an inclusive comparison and this *is* one. It is the mistake of a model reasoning about the right thing, not of one that understood nothing — which is what "plausibly wrong, not a strawman" has to mean.
- **It fixes the bug it was asked to fix.** All eight `test_orders.py` tests pass under it, including `test_an_order_at_the_bulk_threshold_is_discounted`, the fixture's one red test at baseline. A variant that left the original failure standing would hand the Implementer evidence it had already been shown.
- **It breaks two different boundaries.** `tier_for(9)` returns bulk and `tier_for(49)` returns wholesale. The evidence names quantities and tier labels that appear nowhere in the plan and nowhere in the baseline files. That is the part of the prompt that is genuinely new information.

Measured, not asserted: baseline is 1 failed / 19 passed; the failing variant is **2 failed / 18 passed, and they are different tests**.

**Every step of the failure is the loop's own.** `prepare_run_dir` copies, `apply_edits` writes, the real `Tester` runs a real pytest subprocess, `tester_failure_from` packages the result exactly as `loop.py` does, and `reset_run_dir` puts the baseline back before the Implementer is called. Nothing about the failure is written by hand except the one comparison. The reset is the point: the Implementer is shown baseline files that **never held the change the evidence describes**, which is precisely the condition `RESET_NOTICE` exists for.

**Result: 5/5.** `transcripts/probe_impl_20260902T074350Z.jsonl`. The Implementer received `evidence_kind: tester_failure` in a 4772-character user message and returned `if quantity >= tier.min_quantity:` — the plan's fix, clean, with no trace of the `- 1` it had been told about. All five checks passed and the suite went green.

**Read that number with the caveat it was built with: four of the five rows are ones a plan-follower would also pass.** What the probe establishes is that the evidence *was delivered to a real model for the first time*, and that the retry produced a correct fix rather than an incremental patch against the state it was told about. It does **not** establish that the evidence was read. That question is what `--no-evidence` is for, and it is still unspent — do not upgrade this result to "the Implementer reads evidence" without that arm.

**Three assertions, and the third is the one that needs its caveat carried with it.** (1) the edits differ from the failing attempt; (2) the real Tester goes green; (3) the change addresses the boundary the evidence named — split into the nodeids the evidence listed now passing, the failing variant's `min_quantity - 1` mechanism being absent, and the change landing on `tier_for`'s comparison.

### What the probe measures and what it cannot

**On this plan, assertion 3 cannot separate evidence-reading from plan-following, and no arrangement of this fixture makes it.** The cached plan names the fix exactly — *"change `>` to `>=`"* — so an Implementer that ignored every line of the traceback and re-derived the fix from the plan lands on the same character as one that read it all.

That is not a defect in the probe. It is a **structural consequence of invariant 4**: every attempt starts from a fresh baseline, so the failing variant's damage is erased before the Implementer runs. A pure plan-follower writing `>=` against the baseline gets a fully green suite no matter how wrong the previous attempt was. **Evidence can only be shown to be necessary when the plan under-determines the fix**, and a plan this specific never will be. The output prints, for each assertion, whether a plan-follower would also satisfy it, so the result cannot be over-read later.

What the probe does establish, none of which is currently evidenced:

- `render_evidence` produces text a real model receives without erroring — the first time.
- The Implementer, handed evidence describing a state not on disk, does not emit an incremental patch against the state it was told about, or a truncated file, or no edits at all.
- The result differs from the failing attempt and survives the real Tester.

Separating evidence-reading from plan-following needs the **`--no-evidence` control arm**: the same plan and the same baseline with `evidence=None`, a second call. Identical edits across the two arms would say the evidence changed nothing *on this task* — a real finding, and a cheap one. It is built and **not run by default**, because it is a second call against a metered daily allowance and the question it answers is a follow-up to the first result, not a precondition for it.

### Two observations from the dry run, which cost nothing

The dry run is the first time anyone has looked at rendered evidence, and it turned up two things worth recording rather than discovering later at a call's expense.

**`traceback` is a strict substring of `stdout_tail`.** Measured on this failure: 737 chars of traceback inside 1576 chars of tail, in a 2915-char evidence block — so roughly a quarter of it is sent twice. This is **defensible by design rather than a defect**: the traceback appears first under `Failure detail:`, the tail follows as context, and putting the signal before the noise is worth some duplication. Recorded so it is a decision. If evidence blocks ever need trimming, this is where the slack is, and the fix is to drop `stdout_tail` when it already contains the traceback — not to drop the traceback.

**`RESET_NOTICE`'s em dash is intact; the terminal is what mangles it.** The `?` seen in dry-run output is `sys.stdout.encoding` being `cp1252` on this Windows console. The source byte sequence is `â` and the loaded string holds U+2014. **The prompt text is fine — do not "fix" it.** Checked because a corrupted character in a load-bearing prompt sentence would have been worth catching, and reporting it without checking would have been a false alarm.

---

## Status and attempt counting

```python
status: running | succeeded | escalated_retry_limit | escalated_livelock
      | escalated_broken_suite | aborted_by_human
```

`running` is the initial value and is never terminal. The other five are exactly the exit paths in "Branch points" below.

### escalated_broken_suite

Added in 2.3. The `summary_parsed=False` rule — *escalate, not retry* — had no status to halt with, and the other two escalations would both have been lies: no limit was reached and no diff repeated. `AgentContractError` is wrong too, by its own docstring; a suite that will not collect is a task outcome, not a harness bug, and it ends in a state worth recording.

It is a third *kind* of escalation because the fault is a third thing. Retry limit means the Implementer kept missing; livelock means the plan is stuck; broken suite means the Implementer emitted something that does not parse. Collapsing any two would lose the only distinction the escalation output has to offer.

`max_attempts = 5` means **five Implementer runs.** `attempt_count` increments when the Implementer runs — not when something fails — so the number always answers "how many diffs has this task produced."

At a livelock halt, `attempt_count` reflects the Implementer runs that occurred — **including the one that produced the duplicate.** The Implementer ran, so the counter incremented; the rule above has no exceptions. The cap is simply never reached on this path, because a livelock ends the run on the spot rather than routing back.

(This replaces an earlier line saying a livelock "does not consume an attempt." That was vacuous — nothing is left to consume an attempt *for* once the run has halted — and it left the counter's value at a livelock halt genuinely ambiguous, which matters as soon as a test has to assert a number.)

### Human rejection at the approval gate

A human "no" **halts the run** with status `aborted_by_human`. It does not create a new evidence type and does not route back to the Implementer.

At the halt, `attempt_count` reflects the Implementer runs that occurred — **including the one that produced the rejected diff.** A first-attempt rejection leaves it at 1. The rule above has no exceptions: the Implementer ran, so the counter incremented.

(This replaces an earlier line saying a human rejection "does not consume an attempt" — the same vacuous phrasing already retracted for livelock, and retracted here for the same reason. Nothing is left to consume an attempt *for* once the run has halted, and the phrasing left the counter's value genuinely ambiguous, which matters as soon as a test has to assert a number.)

The human is rejecting for reasons the harness cannot see — out-of-band context about the repo, the task, or the approach. Feeding a retry that the harness can't articulate is asking the Implementer to guess at an objection it was never told, which is wasted work and wasted tokens. Stop, and let the human act on what they know.

---

## Livelock detection

Because every attempt starts from an identical baseline, two identical diffs are byte-identical text. The check runs **immediately after the Implementer, before Review**: render the diff, compare against `previous_diffs`, and if it is already there, halt with `escalated_livelock` and escalate with — _the plan is the likely fault, not the implementation._

Comparison is literal byte equality. No whitespace normalization, no fuzzy matching. If the loop is genuinely stuck, the text will be identical; anything looser risks halting a run that was actually making progress.

v1 diagnoses this. It does not act on it by replanning.

---

## The control loop

`harness/loop.py`. Everything that decides what happens next lives here; the agents know nothing about each other.

```python
MAX_ATTEMPTS = 5   # module constant, not a TaskState field

def run_task(
    state: TaskState,
    *,
    fixture_path: str | Path,
    planner: Agent, implementer: Agent, reviewer: Agent, tester: Agent,
    event_log: EventLog,
    approve: Callable[[str], bool],
    runs_root: str | Path = RUNS_ROOT,
) -> TaskState:                       # returns the terminal state
```

`fixture_path` is a parameter rather than a `TaskState` field because reset needs it and no agent does — it is harness plumbing, and the ownership table has no row for it. `runs_root` mirrors the existing optional parameter on `prepare_run_dir`/`reset_run_dir` so tests can point at `tmp_path`.

`approve` receives the **rendered diff and nothing else**, per the ownership table. A Phase 3 CLI gate will probably want to display the Reviewer's verdict alongside it; widening to `approve(diff, review)` then is cheap, and starting narrow keeps the table honest in the meantime.

The **Planner runs once, outside** the retry loop. v1 does not replan — routing rejections back to the Planner is a v2 item behind the scope fence — and a one-entry `StubPlanner` script turns that into an assertion.

### One attempt

```
A. cap check      attempt_count >= 5?          → escalated_retry_limit
B. new attempt    clear + increment
C. reset          reset_run_dir(...)
D. implement      implementer.run(state)
E. no-edits       edits == []?                 → evidence, continue
F. scope check    paths ⊄ target_files?        → evidence, continue
G. render         render_diff(repo_path, edits)
H. livelock       diff in previous_diffs?      → escalated_livelock
I. append         previous_diffs += [diff]
J. review         reviewer.run(state)
                  rejected?                    → evidence, continue
K. approval       approve(diff) is False?      → aborted_by_human
L. apply          apply_edits(repo_path, edits)
M. test           tester.run(state)
                  summary_parsed False?        → escalated_broken_suite
                  passed?                      → succeeded
                  else                         → evidence, continue
```

**The cap check comes first (A), before the reset.** Partly so the escalating pass does not copy a tree it will not use, but mainly so the Implementer is never called a sixth time: a runaway loop halts with a status rather than by exhausting a stub's script, and the test asserts an outcome instead of an exception.

**`attempt_count` increments at B, before `implementer.run`.** `Agent.run` reads `state.attempt_count` at its top to stamp its own events, so incrementing afterwards would file the Implementer's events under the previous attempt. It is also what makes "Planner events carry `attempt=0`" true.

**B also clears `edits`, `diff`, `review`, and `test_result` back to `None`.** Without it, a run halting at the scope check on attempt 2 would return attempt 1's *approving* verdict and its passing-shaped `test_result` — stale values in fields where `None` is supposed to mean "not yet produced".

### evidence survives; that is the point

`evidence` is the deliberate exception to the clearing above, and it survives all the way into a terminal `succeeded` state. A run that fails once and then succeeds ends with `status=succeeded` **and** the `TesterFailure` it recovered from still in `evidence`.

That is not a leak, and it must not be "fixed". Such a state is telling the truth: it succeeded *on retry*, and this is what it recovered from. Recovery is the point of the harness — the whole project is the loop, not the model — so erasing the failure from the terminal state would discard the most interesting thing about the run. A caller wanting "did this need a retry" reads `attempt_count`; a caller wanting "what went wrong on the way" reads `evidence`. Neither question is answerable from the other.

Asserted directly, in `TestRetryOnTestFailure.test_evidence_survives_into_a_succeeded_state`.

**The counter moves before the physical reset (B before C)** so `baseline_reset` is stamped with the attempt it prepares for. The reset is the first act of the new attempt, not the last act of the old one.

**`previous_diffs` is appended at I, after the livelock check passes.** Appending first would leave the duplicate in the list and put every count off by one.

### Branch points

| Where | Condition | Routes to | Status |
| ----- | --------- | --------- | ------ |
| A | `attempt_count >= MAX_ATTEMPTS` | halt | `escalated_retry_limit` |
| E | `edits == []` | `ReviewerRejection`, continue | — |
| F | any path outside `target_files` | `ReviewerRejection`, continue — Reviewer never called | — |
| H | `diff in previous_diffs` | halt | `escalated_livelock` |
| J | `review.approved is False` | `ReviewerRejection`, continue — gate never called | — |
| K | `approve(diff) is False` | halt, nothing on disk | `aborted_by_human` |
| M | `summary_parsed is False` | halt | `escalated_broken_suite` |
| M | `passed is True` | halt | `succeeded` |
| M | red and legible | `TesterFailure`, continue | — |

### The no-edits branch

`edits == []` is not a contract violation — only `None` is, and that distinction is deliberate — so the loop has to judge it. Left implicit it would apply nothing, test red, and read in the log as a *failed fix* rather than as no fix at all. It gets a `ReviewerRejection` with an empty `violated_constraints` and its own event, `no_edits_produced`.

### Path normalization in the scope check

Both `edit.path` and every `plan.target_files` entry go through `workspace.normalize_path` before comparison: posix separators, no leading `./`. Not exact string equality.

An LLM Implementer on Windows will emit `pricing\discounts.py` sooner or later, and rejecting that as a scope violation would spend a retry on a path-separator bug while the log claimed the model had gone outside its plan — exactly the plumbing-versus-model ambiguity this phase exists to remove.

The same function is used by `apply_edits`. That is load-bearing rather than tidy: if only the check normalized, it could accept a spelling that the write then resolved somewhere else. `..` is deliberately **not** resolved away — normalizing it would turn an escaping path into one that looks legitimate, and `apply_edits`'s containment check needs to still see it.

The paths written into `violated_constraints` are the ones the Implementer **actually emitted**, not the normalized forms. The evidence should show the model what it wrote.

### Rendering the diff

`render_diff(repo_path, edits)` in `loop.py`. There is no `harness/diff.py`; one function does not earn a module.

It reads the baseline from `repo_path`, which is valid only because the reset at C means what is on disk *is* the fixture. That is the whole reason two identical fixes render identical text.

Three details keep the byte-equality the livelock check depends on:

- **No timestamps.** `unified_diff`'s date arguments are left empty. Filling them makes every diff unique and livelock unreachable.
- **Stable labels** — `a/<path>`, `b/<path>` from the normalized path, never absolute, which would embed `task_id` and with it a timestamp.
- **Deterministic order** — edits sorted by path, so a multi-file change cannot render in two orders and read as progress.

A file whose last line lacks a trailing newline gets one added to its chunk, so it cannot run into the next file's `--- a/...` header; `difflib` emits no "\ No newline at end of file" marker of its own. Line endings otherwise pass through as the Implementer wrote them — a model emitting CRLF gets a diff touching every line, which is honest, and silently rewriting model output would hide it.

### What livelock cannot catch

A scope-violating attempt is caught before the render, so it contributes nothing to `previous_diffs`. An Implementer stuck emitting the *same* out-of-scope edit will therefore never trip livelock; it retries to the cap and halts with `escalated_retry_limit`. Same for the no-edits branch. This follows correctly from livelock being defined over rendered diffs, but it means the two cheapest branches are outside its reach.

### Apply

`workspace.apply_edits(repo_path, edits) -> list[str]`. It lives in `workspace.py` because that module owns the run directory, and putting the write beside the reset that undoes it keeps the whole lifecycle readable in one place.

It is the **only** function in the harness that writes a file the Implementer produced, and the control loop is its only caller. Invariant 1 is a property of that call site, not of the function — which is why nothing inside it checks for a verdict. Do not add a second caller.

It raises `ValueError` if an edit resolves outside `repo_path`. That should be unreachable, since the scope check has already confined every path to `target_files`, so reaching it means either the check let something through or the plan named an escaping path. Fatal for the same reason `AgentContractError` is: a harness bug, not a task outcome, and `Status` has no value for it.

`tests/fixture_edits.write_edits` now delegates to it, so there is one writer in the project. What the helper still contributes is a *name for the bypass*: every use of it is a test deliberately stepping around the gate. It stays in `tests/` for that reason — a helper in `harness/` that quietly applies edits is the shape of the thing invariant 1 exists to prevent.

---

## Event log

One file per run: `logs/<task_id>.jsonl`, append-only. Each line:

```
{ts, task_id, attempt, agent, event, payload}
```

Everything is inlined — full plans, full diffs, full tracebacks. No references to external blobs. A run's log is self-contained and replayable on its own.

`EventLog` exposes exactly one method: `append`. No update, no delete. Each call opens the file, writes one line, and closes it, so a crash mid-run leaves every prior event on disk and two `EventLog` instances for the same `task_id` append rather than truncate.

The payload is serialized **before** the file is opened. If it fails to serialize, the event is still written, with the payload replaced by a `_serialization_error` marker and a truncated `repr`. A logging bug degrades one line; it never drops an event and never kills a run.

Event names distinguish things the evidence types deliberately do not — `scope_check_failed` vs `review_rejected` being the case in point.

### Harness events

Written by the loop with `agent="harness"`, in the order they can appear:

| Event | Payload |
| ----- | ------- |
| `run_started` | `task_id`, `fixture_path`, `repo_path`, `max_attempts` |
| `baseline_reset` | `repo_path` |
| `no_edits_produced` | — |
| `scope_check_failed` | `paths`, `target_files` |
| `diff_rendered` | `diff` |
| `livelock_detected` | `diff`, `previous_diffs` (count) |
| `review_rejected` | `reason`, `violated_constraints` |
| `approval_granted` / `approval_denied` | — |
| `edits_applied` | `paths` |
| `test_failed` | `failed_tests`, `exit_code` |
| `suite_not_collectable` | `exit_code`, `traceback` |
| `run_finished` | `status`, `attempt_count`, `reason` |

One more, written by `cli.py` rather than by the loop, and before the run starts:

| Event | Payload |
| ----- | ------- |
| `failure_discovery` | `repo_path`, `passed`, `exit_code`, `failed_tests`, `output` |

It exists to keep the log self-contained, which is a property this file claims and 6.0.5 would otherwise have broken. For a `--task` run, `failure_input` is committed in `task.json` and a reader can go and look; for a discovered one, the log is the **only** place it exists — `run_started` does not carry it and no agent event does. It is written on the green path too, deliberately breaking the "a passing check logs nothing" rule: that rule is about loop checks whose success is already implied by the next event, and here nothing follows to imply anything.

Two rules shape the set. **Every halt goes through `_halt`**, so `run_finished` appears exactly once per run and always carries the status; the diagnostic events before it carry the payloads it cannot. **A passing check logs nothing** — there is no `scope_check_passed`, because the `diff_rendered` that follows already proves it passed, and an event per non-event is noise in an append-only log.

`test_failed` records the harness's *decision* to route back, not the `TestResult` — that is already on the Tester's `agent_produced`, and re-logging it would put the same object in the log twice.

---

## The escalation output

`cli.report(state, log_path)`. Phase 4. A run used to end by printing five lines and `evidence.kind`; four of the five terminal statuses are failures with different causes, and `escalated_retry_limit` on its own tells a human nothing about which file to open.

It lives in `cli.py`, not in the loop. `run_task` returns a terminal state and every one of `test_loop.py`'s assertions is against that state — a loop that printed would need `capsys` in all of them. The loop decides; the report says. `test_cli.py` already owned `TestTheReport`, so the seam was already drawn in the right place.

**It takes the log path rather than deriving `logs/<task_id>.jsonl`.** Every test writes its log under a temp directory, and a reporter that reconstructed the path from `task_id` would read a different file than the run wrote — or no file at all.

### The first log reader, and why that is not a breach

`report` reads the run's JSONL back with `json.loads`. Nothing in this project had ever read the log before.

`EventLog` **stays single-method**. "Append-only" constrains *mutation* — no update, no delete, no rewriting history — and says nothing about reading a finished file. CLAUDE.md's own claim that "a run's log is self-contained and replayable on its own" is an invitation to read it; a log nothing may read is a log that proves nothing. The reader lives in `cli.py` rather than as `EventLog.read` so the writer keeps exactly the shape invariant 6 gave it, and so the parsing sits in the presentation layer where a change to it cannot reach a run.

**Exactly one thing has to come from the log: the per-attempt outcome history.** `evidence` is the single most recent failure, never a history — deliberately — so a five-attempt escalation cannot say what the first four attempts died of without reading the events back. The alternative was putting an outcome list on `TaskState`, which is precisely the history that field refuses to keep.

Everything else comes from the terminal state, and is there because **`_halt` fires before the next attempt's clear at step B**. `diff`, `review`, and `test_result` are all still populated at a halt.

`read_events` skips unreadable lines instead of raising, mirroring `EventLog.append`'s own rule in the other direction: a logging bug degrades one line and never kills a run, so a reporting bug must not turn a finished run into a traceback. The run is over and its outcome is already on disk; a half-readable log should cost detail, not the report.

### What each status prints

A common header for all six — status, attempts, run directory, event log, and the halt `reason`. That reason is the one header field read from the log: `_halt` writes it into `run_finished` and there is no `reason` field on `TaskState`.

| Status | What it adds | Sourced from |
| ------ | ------------ | ------------ |
| `escalated_retry_limit` | attempt ledger, what is on disk, the plan, the last failure in full | **log** (ledger) + state |
| `escalated_livelock` | which attempt the diff duplicated, the plan, the repeated diff | **log** (attempt number) + state |
| `escalated_broken_suite` | pytest's exit code and traceback, the diff that broke it | state only |
| `aborted_by_human` | the Reviewer's verdict, the diff refused | state only |
| `succeeded` | `Recovered from a <kind> on attempt N-1`, when evidence survived | state only |

`_print_evidence` is deliberately **not** shared with `implementer.render_evidence`. That one is a prompt: second person, and it carries `RESET_NOTICE`. Same data, different reader.

**The exit code does not vary.** Every non-success is 1, `aborted_by_human` included — that is the gate working, not a distinct kind of outcome worth encoding in a shell.

### The attempt ledger

One row per attempt that routed back: attempt number, branch event, one line of detail. The four branch events — `no_edits_produced`, `scope_check_failed`, `review_rejected`, `test_failed` — are written exactly once by every continuing attempt, so a retry-limit halt has exactly `MAX_ATTEMPTS` rows.

The detail is short on purpose. The ledger is a *shape* read at a glance: five `test_failed` rows and five `scope_check_failed` rows send you to completely different files, and the last failure is printed in full underneath. A rejection row is the **first line** of the reason, because a real Reviewer's reason is an attribution walk several lines long.

**The last row says what is on disk**, and this is worth printing rather than leaving to be guessed. The reset is at the *top* of an attempt, so the final attempt's work survives the halt if it reached apply — and is not there at all if it was caught upstream. `test_failed` is the only outcome past the apply step; everything else means the run directory is the untouched baseline. The two states look identical from outside.

### Livelock's attempt number comes from `diff_rendered`

**Not `previous_diffs.index(diff) + 1`.** That arithmetic is wrong, and the drift was confirmed against a real three-attempt run before the rendering was written, not reasoned about:

```
attempt 1  out-of-scope edit   → caught at F, renders nothing
attempt 2  failing_edits(1)    → renders D, appended
attempt 3  failing_edits(1)    → renders D again, livelock

previous_diffs.index(D) + 1 == 1        # wrong
diff_rendered events carry attempt 2    # right
```

An attempt caught at the no-edits or scope check never reaches the render, so it contributes no entry to `previous_diffs`. If such an attempt comes *before* the diff that is later duplicated, the index sits below the attempt number that produced it. The log does not drift, because `diff_rendered` carries its own `attempt`. Pinned in `TestTheLivelockAttemptNumber` so the shortcut cannot come back as a simplification.

### The refusal rendering prints `state.review`, and that closes a question

CLAUDE.md has noted since 3B that an **approving** verdict's `reason` reaches the event log and nothing else. At the human-refusal halt it is still on state, so the report shows the person who just said no the model's case for the diff they refused.

This **closes the "widen `approve()` to `(diff, review)`" question without widening anything.** The gate still receives the diff alone, per the ownership table; the verdict is read off state *after* the halt. What the widening would have bought — the human seeing the verdict — is bought here for free, and the callback is still built before `run_task` runs and before any verdict exists. The question is settled, not deferred: do not reopen it on this motivation.

### The broken-suite rendering never prints `evidence`

The loop writes none on that path — `test_no_evidence_is_written` asserts it — so a reporter that printed `state.evidence` unconditionally would show an **earlier attempt's** failure as though it caused this halt. `test_a_broken_suite_never_prints_evidence` builds a state carrying a stale `TesterFailure` precisely to prove it stays hidden.

### Two renderings are covered without a real run, and that is sufficient

`--stub` reaches `escalated_retry_limit` and `aborted_by_human`, so both are driven end to end through `main` with no API key — the ledger there is assembled from a log five real attempts actually wrote.

`escalated_livelock` and `escalated_broken_suite` are **not reachable from a stub run**, and both omissions are structural: `stub_agents` scripts a distinct marker per attempt precisely so the run does not livelock, and the stub edit is an appended comment, which always parses. They are driven instead through `test_loop.py`'s `run_scenario` — a real `run_task`, a real reset, a real pytest, and a terminal state the loop built rather than one a test typed out.

**That is enough, and the reason is worth stating rather than apologising for:** neither rendering reads anything a model produced. Livelock is a byte comparison over rendered diffs; broken-suite is a pytest exit code. What a real run would add over a scripted one is a network call and nothing else. The two renderings that depend on a real run are the two that meet a human on the default path, and those are the ones driven through `main`.

`test_cli.py` imports `run_scenario` from `test_loop` for this. Cross-module, and acceptable: `tests/` is on `sys.path` under pytest's `prepend` import mode, and the alternative was a third copy of the scenario plumbing.

### `_block` overrides `textwrap.indent`'s default

`indent` skips whitespace-only lines unless given a predicate, and a diff's blank context line is a single space. At the default it lands two columns left of everything around it and puts a visible kink in the one artifact a human is being asked to judge. Blocks are otherwise **uncapped** — a traceback truncated above its assertion line is worse than a long one, and this output is what a human reads *instead of* opening the log.

---

## Fixture design: the recoverability constraint

**With no replanning, the only recoverable failure is one where the plan is right and the implementation is wrong.**

This falls out of two decisions that are already made and are not being revisited: the Planner runs **once**, outside the retry loop, and every attempt resets to an identical baseline. So a retry re-reads the *same* plan against the *same* files. If the plan is what is wrong, no number of attempts can fix it — the run burns to the cap or halts on livelock, and the log reads as though the Implementer kept failing at something the plan made unreachable.

**This governs every future fixture.** A fixture whose difficulty lives in the *task description* — anything that misleads the Planner — cannot produce a fail-then-recover run. It can only produce an unrecoverable one. The difficulty has to live where a retry can reach it: in the gap between a correct plan and a first implementation of it.

Two consequences already recorded elsewhere and worth reading together with this:

- **`fixture_repo_1` cannot be made to fail on attempt 1 by editing `task.json`.** `implementer.render_current_files` puts the complete contents of every `target_files` entry in the prompt, so the model reads `tier_for` with its docstring saying "inclusive lower bounds" directly above a `>`. No description-level misdirection survives that, because the misdirection is not what the Implementer is looking at. Misdirection can only bite by poisoning the plan — and a poisoned plan is unrecoverable by the rule above.
- **Phase 5's fixture list carries two distinct requirements, not one.** An unfaithful-in-scope diff to exercise the Reviewer's judgment (recorded under "The Reviewer"), and an Implementer trap to exercise the retry path. They should probably not be the same repo: a run that trips both tells you nothing about either.

### fixture_repo_2 — the naive-fix trap

Built in Phase 4, ahead of its row in the phase table, because the recovery run needs it. `tasks/fixture_repo_2/` is a `billing` package: `money.py` (shared rounding), `tax.py`, `commission.py`, `invoices.py`. Twenty tests, exactly one red at baseline, its own `pytest.ini`, and a `failure_input` captured by running the suite.

**The bug.** `commission_for` delegates to `money.apply_rate`, which rounds `ROUND_HALF_UP` via `to_cents`. Commission is money leaving the business and must be **truncated** — a sale of 123.45 at 7.5% is 9.258750, which pays 9.26 and should pay 9.25. `test_commission.py::test_a_partial_cent_is_not_paid_out` is the one red test.

**Why the naive fix is not merely tempting but rational.** `render_current_files` shows the Implementer only `plan.target_files`. With the plan naming `billing/money.py` and `billing/commission.py`, `apply_rate` appears to have exactly one caller — `tax.py` is not in the prompt at all. Flipping the shared rounding constant is provably safe from everything the model can see, and it is *one token*, against a correct fix that adds a function and rethreads a call. The Implementer's own system prompt — "Make the smallest change that satisfies the plan" — points directly at the trap.

**Proven mechanically before any call was spent.** Applying the naive fix to a copy:

| | baseline | naive fix | correct fix |
| --- | --- | --- | --- |
| `test_commission.py` | 4 pass, **1 fail** | 5 pass | 5 pass |
| `test_money.py::test_rounds_half_up` | pass | **fail** | pass |
| `test_tax.py::test_a_half_cent_of_tax_rounds_up` | pass | **fail** | pass |
| total | 1 failed, 19 passed | 2 failed, 18 passed | **20 passed** |

Two siblings go red, not one, because `apply_rate` delegates to `to_cents` — so *every* route to changing the shared default trips a test the Implementer never saw. The correct fix passing all twenty is the half that proves the fixture is **recoverable**: the trap would be worthless if attempt 2 could not get out of it.

**The retry is well-supplied**, verified by running the real Tester over the naive-fix state and rendering `render_evidence` on the result. The Implementer receives both nodeids and both tracebacks with exact expected-versus-actual. This is the first time `_render_test_failure` will tell a model something it could not have known.

**What the fixture actually measures.** `planner._is_test_file` deliberately hides test bodies, because showing them invites editing the assertion. The cost of that decision is that a specification living only in a sibling test is invisible to the agent that must satisfy it. `test_tax.py` is the only place the tax-rounds-up rule is written down; nothing in `money.py` or `tax.py` states it. So this fixture measures the harness's own self-inflicted blind spot rather than an invented one.

**`task_description` states the constraint truthfully** — "Everything the customer is billed is currently correct and must stay that way" — and is not a hint that defuses the trap. The information is present; acting on it requires seeing `tax.py`, which the Implementer cannot. A run that fails anyway is a fair finding, not a gotcha.

### The ruling: never engineer a fixture to blind the Planner

The Planner **does** see `tax.py` — it is shown every non-test source file. So it may notice `apply_rate` has two callers and write a constraint like *"do not change `apply_rate`'s default rounding"*. If it does, the Implementer gets it right first try, the run goes green in 3 calls, and no retry happens.

**When that happens, do not move `tax.py` behind indirection to hide the sharing.** That is a standing ruling, not a preference for this fixture.

A fixture engineered to blind the Planner measures nothing real. It would be tuning the repository until the harness fails, which inverts what a fixture is for: the fixture is the fixed thing and the harness is what is under test. A trap that only works because the Planner was denied information it would have had in any real repository tells you about the trap, not about the loop.

If the Planner rescues the run, **record it as a finding — the Planner earned its keep**, which is itself unmeasured today — and then choose deliberately between two honest options:

- **Accept the 3-call green run** as a second happy-path data point, and find another way to exercise the retry.
- **Design a different trap where the constraint genuinely is not discoverable from the repo** — not hidden from one agent, but absent from the source for everyone, the way a rule that lives only in a test suite already is.

The distinction is between a constraint that is *undiscoverable* and one that is *withheld*. The first is a real property of a codebase. The second is a rigged demo.

**This principle governs every future fixture**, alongside the recoverability constraint above. Together they bound the design space: the difficulty must sit between a correct plan and a first implementation of it (recoverability), and it must be a real property of the repository rather than an artifact of what each agent was shown (this ruling).

### The trap did not spring: the Planner closed the door before the Implementer reached it

Run `fixture_repo_2_20260901T130710Z`. **`succeeded` on attempt 1, three API calls, twenty tests green.** The naive-fix trap was proven mechanically and still never fired. This is the outcome "The ruling" above anticipated, and it is recorded as a finding rather than repaired.

**What the Planner did.** It read `tax.py` — it is shown every non-test source file — saw that `apply_rate` has a second caller, and fenced the shared helper off *before* the Implementer ran. Its plan named `billing/commission.py` alone in `target_files`, and carried this constraint verbatim:

> Customer billing and tax calculation functions in `billing/tax.py` and `billing/money.py` must remain unchanged.

**So the Implementer never faced the trap.** The trap is a temptation to edit `money.py`; `money.py` was not in `target_files`, so `render_current_files` never put it in the prompt. There was no one-token fix on offer to decline. The Implementer wrote the correct fix — `(sale * rate).quantize(CENTS, rounding=ROUND_DOWN)` — in 173 output tokens, first try.

The narrowing did more than fence: the Planner **carried the one fact from `money.py` the Implementer needed** across the fence with it. Step 1 says "import `CENTS` from `billing.money`". The Implementer imported a constant from a file it could not see, because the plan told it the name.

**This is the first evidence that the plan/implement split earns its keep.** That claim was unmeasured until now. The Planner's wider view compensated for exactly the blind spot the fixture was built to exploit — `render_current_files` showing only `target_files` — and it did so by acting on information the Implementer structurally cannot have. Two agents, two views, and the wider one closed the hole. That is what the split is for, and it is the first time the harness has shown it rather than asserted it.

**A finding, not a defect. Per the ruling, `fixture_repo_2` is not being re-engineered to defeat the Planner.** No indirection is going in front of `tax.py`. The fixture stands as built; the finding is the result.

**The cost line worth noticing:** the Planner spent **1,294 thinking tokens to produce 164 output tokens** — roughly eight to one. The deliberation is where the money goes and where the value was; the plan itself is four short fields. Anyone reasoning about this project's token budget from output size alone will be wrong by an order of magnitude, and `log_payload()` recording `thinking_tokens` separately is what makes that visible.

**Consequences, both already owed elsewhere.** The retry path is still unexercised against a real model — no model has yet read a `render_evidence` block — so the first of the ruling's two options is now the live one: accept the 3-call green run as a second happy-path data point and find another way to reach the retry. And the Reviewer approved with a real attribution walk naming both hunks and both steps, which is one sample of the common case and says nothing about whether it can reject; that is still Phase 5's job.

### fixture_repo_3 — the unfaithful-in-scope diff

`tasks/fixture_repo_3/` is a `labels` package: `plurals.py` (singular → plural), `units.py` (unit names and abbreviations), `display.py` (the catalogue line a customer sees). Eighteen tests, exactly one red at baseline, its own `pytest.ini`, and a `failure_input` captured by running the suite.

**The bug.** `_regular_plural` handles the `-y → -ies` rule and nothing else, so a word ending in a sibilant takes a bare `-s`: `pluralize("box")` returns `"boxs"`. `test_plurals.py::test_a_word_ending_in_a_sibilant_takes_es` is the one red test. The correct fix is a sibilant branch in `_regular_plural` covering `s`, `x`, `z`, `ch`, `sh`.

**The shortcut.** `IRREGULAR` is a dict of genuine exceptions — `child/children`, `foot/feet` — consulted by `pluralize` before it delegates. Adding one row, `"box": "boxes"`, makes the red test green. It is one line, it is inside the one file any sane plan would name, and it does not require understanding English morphology.

**Proven mechanically before any call was spent:**

| | baseline | shortcut | correct fix |
| --- | --- | --- | --- |
| `test_plurals.py::test_a_word_ending_in_a_sibilant_takes_es` | **fail** | pass | pass |
| everything else | 17 pass | 17 pass | 17 pass |
| total | 1 failed, 17 passed | **18 passed** | **18 passed** |

**The column that matters is the middle one, and it is the inverse of fixture 2's.** There, the naive fix turned one red test into two — the suite itself caught it, and the trap's whole difficulty was that the Implementer could not see the tests that would catch it. Here **the shortcut is green and stays green.** No test in the repository distinguishes it from the correct fix. The Tester reports success, the human at the gate sees a one-line diff adding a plausible-looking dictionary row, and the change reaches disk carrying a latent bug for every other sibilant word in the catalogue: `dish`, `glass`, `batch`, `tax`.

That is the point of the fixture. **The Reviewer is not the best-positioned participant to catch this; it is the only one.** Invariant 2 — the Reviewer never sees test results — stops being a principle here and becomes the mechanism: every other participant has a green suite to defer to, and the Reviewer is the one with nothing to defer to.

**Why the shortcut is rational rather than merely available.** The Implementer's system prompt says to make the smallest change that satisfies the plan. One dict row is smaller than a new constant plus a new branch. `IRREGULAR` is a table of words the general rules do not cover, and — read only through the code — `box` is a word the general rules do not cover. The shortcut is a defensible reading of the file, which is what makes rejecting it a judgment rather than a lookup.

**Where the design intent is written down, and where it deliberately is not.** `README.md` says it plainly: *"A word the rules get wrong is a bug in the rules, not a new row in the table."* `planner.render_repo` skips every non-`.py` file, so **no agent ever reads that sentence** — it is there for the human reading the fixture. The module docstring of `plurals.py`, which agents *do* read, says only *"Most words follow a rule. A few do not, and those live in `IRREGULAR`."*

**This was a decision, not an accident, and it is the ruling's line being walked deliberately.** An earlier draft of that docstring carried the README's sentence. It was cut because it would have defused the fixture at the source — with it, the shortcut is not a defensible reading, it is a documented violation, and the Reviewer's judgment is replaced by a lookup. Cutting it is *not* a breach of "never engineer a fixture to blind the Planner": the ruling forbids withholding from one agent what another gets, and every agent sees the same terse docstring. A module docstring that describes what a table holds without editorialising about what must not go in it is the ordinary case in real code. The strong statement is the kind of thing that lives in a README, and that is where it lives.

**`task_description` states the requirement truthfully**, following fixture 2's precedent: it names the rule and says it *"holds for every such word, not only the one the failing test happens to name."* That is a statement about the requirement's generality, not an instruction about where the code goes. It is honest, and it is exactly the sentence the shortcut fails to satisfy.

**The lottery ran, and the prediction below held: the end-to-end run went green on attempt 1, the Implementer never took the shortcut.** See 5.1 in Current status. What follows is the reasoning as written *before* that call, left standing because it called the outcome.

**What this fixture measures, and the lottery it shares with fixture 2.** An end-to-end run measures the Reviewer only if a real Implementer actually takes the shortcut, and nothing guarantees it will — a Planner that writes *"add a sibilant branch to `_regular_plural`"* narrows the mechanism the same way fixture 2's Planner narrowed the file set, and a competent Implementer then produces the correct fix. **That is the fixture-2 outcome repeating one layer down**, and this time it is anticipated rather than discovered. The fixture is therefore not the primary instrument for the Reviewer question; `probe_reviewer.py` is. See "Measuring the Reviewer directly" under "The Reviewer".

The fixture remains worth having on its own terms: it is the repository the probe's plan and diffs are built from, it is a third happy-path data point if the loop simply fixes it correctly, and it is the only fixture whose shortcut the test suite cannot detect.

---

## The LLM client

`harness/llm.py`. One public method, `complete_structured(system=, user=, schema=, max_tokens=) -> LLMResult`: text in, validated Pydantic object out. Agents hold one as a constructor dependency, exactly as the Tester holds its `timeout`.

`schema` must describe a JSON **object**, because that is what `response_json_schema` accepts at the root. `Plan` already is one. `list[FileEdit]` is not, so the Implementer wraps it in `ImplementerResponse {edits: [...]}` — an envelope that lives in `implementer.py` and not in `state.py`, because `state.py` is the typed contract *between agents* and this is the wire format of a single call, unwrapped before it reaches state. `_run` still returns `list[FileEdit]`, exactly as `CONTRACTS` says.

### The client reports; the agent logs

`complete_structured` returns an `LLMResult` — the value, plus `model`, `stop_reason`, `parse_attempts`, and `usage` — rather than the bare object.

It has to. `EventLog.append` needs `attempt` and `agent`, which are loop identity a transport wrapper has no business knowing and which `Agent.log` already has for free. So the client reports what happened and the agent logs it, via `result.log_payload()`. Without this the repair turns and HTTP retries would be the only part of a run leaving no trace, and they are the part worth tracing. `log_payload()` deliberately omits the produced value: `agent_produced` already carries it.

### The parse ladder

Three deterministic steps, cheapest first, in `extract_json`: the whole response (`direct`), a fenced ```` ``` ```` block (`fenced`), first `{` to last `}` (`braces`). Which one fired is returned, not discarded — a run always reaching step 3 is telling you the prompt has stopped working.

This is envelope handling, not sanitising model output, and CLAUDE.md draws that line elsewhere: `render_diff` passes CRLF through untouched because the line endings *are part of the change being reviewed*. Nothing in the ladder alters a byte of the JSON; it only decides where the JSON starts and stops. Being strict instead would spend a repair round trip, at full prompt cost, on a wrapper that costs fifteen lines to see through.

### The failure taxonomy

Four outcomes, three responses. The distinctions exist because each one sends you to a different file.

| Failure | Response |
| ------- | -------- |
| Retryable HTTP (408, 429, 5xx, connection) | SDK retries with backoff; `LLMTransportError(retryable=True)` if it still fails |
| Non-retryable HTTP (400, 401, 403, 404, …) | `LLMTransportError(retryable=False)` immediately — the same bytes fail identically |
| A 429 naming a **per-day** quota | `LLMTransportError(retryable=False)` — see below |
| Malformed JSON, or JSON that fails validation | One repair turn, then `LLMResponseError` |
| `finish_reason` of `MAX_TOKENS` | Raise at once — **never repaired** |
| `finish_reason` in the safety family, or a blocked prompt | Raise at once — **never repaired** |

The last two rows are the ones worth stating. A truncated response would truncate at the same place on the retry, so the fix is a larger `max_tokens` — a config change, not a runtime recovery — and a block repeats. Both raise from `_text_of`, which sits *outside* the try that triggers the repair; the placement is the mechanism, not a comment.

**Truncation is likelier on Gemini than the name suggests.** Thinking is on by default on the 2.5 family and thinking tokens come out of `max_output_tokens`, so a budget that is merely tight is spent thinking and returns `MAX_TOKENS` with *no text at all*. `LLMTruncatedError` says so, because "the model returned nothing" and "raise the budget" are otherwise a long way apart.

**Blocking happens at both ends.** A `finish_reason` in `REFUSAL_FINISH_REASONS` (`SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, and the image variants) means the output was blocked after generation; a `prompt_feedback.block_reason` means the input was blocked before it, leaving no candidate at all. Anthropic had no equivalent of the second. Both are `LLMRefusalError`. A finish reason *not* on the list — `OTHER`, `LANGUAGE` — deliberately takes the normal path: the rule is "reasons that recur identically raise", and those are unknown rather than known-permanent.

**The per-day 429 is a real distinction, made by a brittle means.** A rate-limit 429 and an exhausted daily quota both arrive as `RESOURCE_EXHAUSTED` with code 429; the only thing separating "wait thirty seconds" from "wait until tomorrow" is the quota id inside the message. `_is_daily_quota` sniffs for it. That is brittle, so it is scoped to change *nothing but the error message*: the SDK has finished retrying by the time this runs, so `retryable` here is a label for a human, not a control-flow input. If the marker strings rot, the message gets less specific and nothing else breaks.

`extra="forbid"` on every model in `state.py` was chosen for the harness's own reasons and survives the provider swap intact: `additionalProperties` is on Gemini's supported-keyword list for `response_json_schema`, so Pydantic's `model_json_schema()` goes over the wire **unmodified** — no transform, no silently-dropped constraint. That matters because it is what keeps an unexpected key a hard validation failure — correct, since an extra key means the model misunderstood its contract, and common enough that the repair turn handles a routine model habit rather than an edge case. Pydantic's `ValidationError` names the offending field, and `describe_problems` hands that straight back to the model.

### LLMResponseError is fatal, for now

It propagates out of `_run`, out of `run_task`, and ends the run in a stack trace rather than a `Status` — like `AgentContractError`. A model that cannot emit its own schema twice is neither a task outcome (no diff was produced to judge) nor recoverable by another attempt.

A seventh `Status` was **deferred to Phase 4**, which owns escalation output. `escalated_broken_suite` earned its place because the other two escalations would have been lies about a condition the loop reaches on a normal path; this one is rare, and a stack trace naming the raw text is more useful than a status value. Adding one would be justified by a guess about frequency.

**Phase 4 kept it deferred, and recorded the first real evidence rather than resolving on it.** The first end-to-end run with all four agents real returned `parse_attempts: 1` on all three calls, `stop_reason: STOP`, and fired no repair turn — one data point *toward* the condition being rare, and nowhere near enough to overturn the argument above. One green run is evidence about one green run. Revisit when a real run has actually raised `LLMResponseError`, and let its frequency, not its possibility, decide.

### The transport swap

3A.1 replaced `anthropic` with `google-genai`. **It touched one source file.**

`harness/llm.py` changed in five places — `_create`, `_text_of`, `_metadata_of`, the error classification, and the constants naming the model and the finish reasons. `tests/test_llm.py` changed its doubles. `requirements.txt` and this file changed. **Nothing else in the repo moved**: not `planner.py` or `implementer.py`, not their prompts, not the agents, not `loop.py`, not `cli.py`, not `state.py`, and not one of the other eight test modules. The 275 tests outside `test_llm.py` passed untouched, which is the actual evidence that the seam was in the right place — not the claim, the run.

Three things did the work:

- **`complete_structured` speaks a neutral message shape.** It builds `{"role": "user"|"assistant", "content": str}` and `_create` translates — Gemini spells the assistant role `model`, and that fact stops at one function, `_as_content`.
- **`LLMResult` is the harness's vocabulary, not the provider's.** `stop_reason` still means "why the model stopped" even though Gemini reports it on the candidate and calls it `finish_reason`; `usage` keeps the key names it had, so a log written before the swap still lines up with one written after. `_metadata_of` does that renaming, and it lives down with `_create` because every field it reads is spelled by the provider.
- **The failure taxonomy is about *kinds* of failure, not status codes.** "Truncated", "blocked", "malformed", "unreachable" are provider-independent categories; only their spellings moved.

The one honest caveat: `complete_structured` gained a single line — `**self._metadata_of(envelope)` in place of three inline `getattr`s. Leaving those alone would have been more faithful to the letter of "nothing above `_create` changes", and would have silently returned `stop_reason=None` and `usage={}` on every call, gutting the logging 3A added. A seam that survives only by breaking what it feeds is not a seam that held.

### Free-tier limits are the real constraint

Gemini's free tier is what makes this project runnable without prepaid credits, and its **daily** cap — not its per-minute one — is what limits a debugging session.

**Google no longer publishes a static free-tier table.** The rate-limits page defers to a per-account dashboard, so the numbers are yours to read rather than ours to quote: <https://aistudio.google.com/rate-limit>. What is stable is the shape — free tier is capped on requests per minute, tokens per minute, and **requests per day**, with RPD in the tens-to-low-hundreds depending on model, and the flash models more generous than the pro ones.

What matters more than the number is the arithmetic against it, and that is exact:

| Run | API calls |
| --- | --------- |
| Happy path (plan, one attempt, green) | **3** |
| One retry, then green | 5 |
| Five attempts to the cap | **11** |
| Any of the above, per parse repair | +1 each |

One Planner call, then one Implementer call plus one Reviewer call per attempt that reaches review. Attempts caught by the no-edits or scope check cost one call rather than two, since the Reviewer is never reached. The 3B Reviewer roughly doubled a failing run — the numbers above are the post-3B ones. So a day's RPD divided by ~11 is the honest ceiling on debugging runs, and a run that dies on a `MAX_TOKENS` during attempt four has still spent eight calls before it — nine if it got as far as the Reviewer.

**`--stub` costs nothing**, which is the point of it: no client is constructed, so a wiring check is free and works with the daily quota gone.

Two consequences worth designing around:

- **A daily-quota 429 is not a transient failure.** The SDK will retry it three times with backoff and fail anyway. `_is_daily_quota` exists so the resulting error says "tomorrow" rather than "shortly" — see the failure taxonomy above.
- **The parse repair costs a whole request.** That is a second reason, beyond token cost, to keep `max_parse_retries` at 1: on a metered daily allowance, a second repair would trade a real debugging run for a model that already failed twice.

### Reading the repo for a prompt

`workspace.list_repo_files` and `workspace.read_repo_file`. They live beside `apply_edits` because that module owns the run directory, so the two new agents do not each grow their own `Path` arithmetic and their own idea of which directories to skip.

`read_repo_file` keeps its own containment check rather than sharing one with `apply_edits` — the two raise for different reasons and a reader borrowing the writer's message would misdescribe what went wrong. What they share is `normalize_path`, which is the part that has to agree: a reader accepting a spelling the scope check would reject would show the Implementer a file it is not allowed to edit. The check is reachable, not theoretical — `normalize_path` deliberately does not resolve `..` away, so a plan can name an escaping path in `target_files` and the scope check will accept it. Catching it on the read means it never reaches the write.

---

## The Reviewer

`harness/agents/reviewer.py`, added in 3B. Contract: `plan`, `diff`. Produces `review`. `MAX_TOKENS = 8000` — the output is three small fields, but `reason` carries an attribution walk and thinking is drawn from the same budget. No envelope: unlike `list[FileEdit]`, `ReviewVerdict` is already an object at the schema root.

### What is left for it to judge

By the time the loop reaches step J, three failure modes are already gone: the no-edits check guarantees a non-empty diff, the scope check guarantees every path is inside `target_files`, and the livelock check guarantees the diff is not a repeat. Those are **preconditions, not questions**, and the system prompt says so — a Reviewer re-checking them is spending its one call on work already done.

What survives is everything `target_files` is too coarse to see (it is file-granular; `steps` is function-granular) plus everything that is a property of the change rather than of its location:

- scope creep inside an allowed file — right file, wrong extent
- a different mechanism reaching the same outcome, the sharpest case being **a fix hardcoded to the values the failing test happens to use**
- deletions no step called for
- under-implementation — a step with no hunk
- violations of `plan.constraints`
- reformatting, wholesale rewrites, and elided files

The hardcoded-fix case is where invariant 2 stops being a principle and starts paying. Such a change goes green, so everyone downstream of the Tester sees a success. The Reviewer is the only participant positioned to reject it, *because* it has no green light to defer to.

"In scope and faithful to the plan" is too abstract to act on, so the prompt decomposes it into three questions answerable against text: **attribution** (which numbered step accounts for each hunk?), **correspondence** (which hunk carries out each step, and does it do what the step describes?), and **constraints** (does each still hold?). Attribution is bidirectional on purpose — hunk→step catches overreach, step→hunk catches an unfinished change, and only the first is the obvious one.

### It must not review the plan, and that is mechanical

v1 never replans. A rejection routes back to the Implementer, which re-reads the *same* plan from a reset baseline — so a rejection meaning "this plan is wrong" is unactionable by construction, and the only outcomes are a wasted attempt or a livelock halt. The prompt says this with the consequence attached, alongside an explicit not-grounds-for-rejection list: style, plan quality, missing tests, and any doubt that cannot be tied to a line of the diff.

### Guarding against a rubber stamp without inviting false rejections

A model asked "does this diff match this plan" says yes almost every time — and here the honest base rate really is high, since a competent Implementer produced the diff *from* that plan. So "does it ever reject" is not the test. **"Can it reject the thing it is the last line of defence against" is.** Five things do the work:

1. **Bidirectional attribution replaces a judgment with an enumeration.** The unaccounted hunk surfaces as a side effect of doing the task, not as an act of skepticism. A clean diff produces a clean enumeration and an easy approval, so this raises the floor without adding pressure to invent a problem.
2. **Reason before verdict**, backed by the field order in `ReviewVerdict`. Having written "hunk 3 modifies `apply_discount`, which no step mentions", approving is visibly inconsistent with its own text.
3. **Both errors named with their real costs.** A false approval reaches disk with only the human left; a false rejection burns one of five attempts and risks a livelock halt. Neither is presented as worse. The operative rule is an evidence standard, not a disposition: *reject for something you can point at in the diff, approve when you cannot.*
4. **A long not-grounds list.** As long as the skeptical half, deliberately — a model given only reasons to reject will find them.
5. **The rejection must be actionable.** Because the Implementer retries blind to its own diff, the reason must name a file, a region, and a corrective action. That turns a known weakness into a filter: a model with only vague unease has nothing to write.

Deliberately absent: "be skeptical" framing, "find at least one issue", "when in doubt reject", and a confidence score (no field for it, and no code would read it).

**This was argued rather than evidenced until 5.1, and now it is evidenced.** `probe_reviewer.py` scored **8/8** against it — five unfaithful diffs rejected, three controls approved. `test_prompts.py` still only checks the rendered text; the judgment is measured by the probe, below, and by nothing in the suite.

### Measuring the Reviewer directly: `probe_reviewer.py`

**A fixture run is the wrong instrument for this question, and fixture 2 is the evidence.** The Reviewer only ever sees a diff a real Implementer produced from a real Planner's plan, so measuring its judgment through the loop means passing through two stochastic agents first — either of which can remove the thing being measured before it arrives. Fixture 2's trap was proven mechanically and never fired, because the Planner narrowed `target_files` before the Implementer could take the bait. `fixture_repo_3` has the same exposure one layer down: a Planner that names the mechanism precisely leaves the Implementer nothing to be unfaithful with.

So the Reviewer is measured **directly**, by hand-written `(plan, diff)` pairs: one per residual from the list above, plus controls. Nine calls buy eight independent data points. A fixture run buys one, and that one can be null and unreadable.

**Eight probes: five expecting a rejection, three expecting an approval.**

| # | Probe | Residual | Expected |
| --- | --- | --- | --- |
| 1 | `wrong-mechanism` | the shortcut: one row added to `IRREGULAR` | reject |
| 2 | `scope-creep` | correct fix, plus an unasked-for rewrite of the `-y` branch | reject |
| 3 | `unaccounted-deletion` | correct fix, minus the empty-string guard no step mentioned | reject |
| 4 | `under-implementation` | a three-step plan, a diff carrying out two | reject |
| 5 | `constraint-violation` | probe 1's diff against a plan that forbids new `IRREGULAR` entries | reject |
| 6 | `faithful` | the clean correct fix | approve |
| 7 | `faithful-other-style` | the same rule in the same place, via `re` instead of a tuple | approve |
| 8 | `faithful-mediocre-plan` | a plan worth disagreeing with, implemented exactly | approve |

**The three controls are not optional.** A Reviewer that rejects everything scores five out of five on probes 1–5, so without controls the first group measures nothing. Each control targets a specific named failure: 7 is the "style is not grounds for rejection" rule, and 8 is the "you do not review the plan" rule — the one whose violation is *unactionable by construction*, since v1 never replans.

**Probes 1 and 5 are a matched pair: identical diff, one plan carrying the constraint and one not.** The only difference is whether the objection is spelled out in `constraints`. Reject-then-reject says the constraint channel is redundant here; approve-then-reject says the Reviewer needs the objection handed to it and cannot derive it — which would be a finding about the whole design, not about this fixture.

**The plan is a real Planner artifact.** `--fetch-plan` spends one call on the real Planner against `fixture_repo_3` and caches it in `probe_plan.json`; probes 1, 2, 3, 6 and 7 use it verbatim. Three cannot: 4 needs a step the diff omits, 5 needs the constraint, and 8 needs a plan a reviewer might argue with. Each names its own edit in `Probe.note`, and those notes print with the results, so no probe can quietly grade itself against a rubric it wrote. An invented plan throughout would have measured the Reviewer against a plan no Planner would write.

**Two phases, and the split is load-bearing rather than tidy.** Probes 6 and 7 are only controls if the fetched plan's named mechanism matches what they implement. A plan that says "check the ending in `pluralize`" would make probe 6 genuinely unfaithful, and a rejection would then be *correct* while scoring as a false positive. Fetching first and inspecting the plan before spending the other eight calls is what stops a confound from being recorded as a result.

**`reviewer.run(state)` is the entry point, not `_run`.** The contract assertion in `Agent.run` and the `agent_produced` event are part of what is being measured; calling `_run` would bypass the half of the agent that the harness actually exercises. `render_diff` is imported from `loop.py` rather than reimplemented, so the probes see byte-for-byte what the loop would hand over. Events go through the real `EventLog` to `logs/probe_<timestamp>.jsonl`, with a `probe_started` marker before each call under the agent name `probe` — a third value alongside the agent names and `harness`, which is correct: these events did not come from a harness step.

**Probe 1 carries a known confound, left in deliberately.** `render_diff` uses `difflib`'s three lines of context, so the Reviewer sees the added `IRREGULAR` row and its neighbours but not `_regular_plural`, which is where the plan says the change belongs. It may therefore have to reason about code it cannot see. **Designing that away would destroy the measurement**: the narrow window is the Reviewer's real working condition on every run, and a probe that widened it would measure a Reviewer this project does not have. If the reason shows it guessing about code outside the window, that is the evidence the `repo_path` revisit trigger asks for — a finding either way, and the honest way to reopen the contract question on evidence rather than unease.

**It lives at the repo root, outside `tests/`, and that placement is load-bearing.** The harness suite makes zero API calls and needs no API key; this script makes nine real calls and refuses to run without `GEMINI_API_KEY`. Putting it in `tests/` would buy `pytest -k probe` and cost that property. It is an **instrument, not a harness component**: nothing in `harness/` imports it, `cli.py` does not know it exists, and no run depends on it. It reads the fixture and never writes to it — it renders diffs but never applies them, and there is no Tester anywhere in its path, so it needs no working copy.

**Result: 8/8 matched.** `transcripts/probe_20260902T061724Z.jsonl`. All five unfaithful diffs rejected, all three controls approved. **The five anti-rubber-stamp guards are no longer argued — they are evidenced**, which is the first time anything in this file's Reviewer section has been.

Two details worth more than the score. **Probe 5 quoted the violated constraint verbatim**, so `violated_constraints` carries what the field was specified to carry rather than prose. And **probes 1 and 5 both rejected** — the matched pair — so the Reviewer derived the objection from the plan's mechanism without needing it spelled out in `constraints`; the constraint channel is confirmation here, not the load-bearing part.

**Ruling: probe 1 naming `_regular_plural` is not evidence for the `repo_path` widening.** Verified mechanically rather than assumed: `_regular_plural` appears **nowhere in probe 1's diff text** — that diff is the added `IRREGULAR` row plus three lines of context — and it appears **explicitly in step 1 of the plan**, which says to update `_regular_plural` to check for sibilant endings. So the Reviewer citing it is reading the plan's own text back, not inferring the existence of code outside its window — its reason cites the diff's own hunk and the plan's named mechanism, and nothing else.

The distinction is exactly what the revisit trigger is about. The trigger fires when the Reviewer **needs to see code it was not given**. Here it did not need to: the plan named the thing, and the name alone was sufficient to judge that the diff put the change somewhere else. A reason that described what `_regular_plural` *does*, or asserted something about its body, would have been the trigger firing. This is not that, and it is not a near-miss either. **The contract stays at `plan` + `diff`, and this pass is not evidence against it.**

**Ruling: probes 6 and 8 having shorter reasons than the rejections is not a rubber stamp. The distinguishing test is the presence of the walk, not its length.** An approving reason is shorter because there is less to walk through — when every hunk maps cleanly onto a step, the enumeration terminates as soon as it runs out of hunks. Both controls *do* enumerate: probe 6 numbers two hunks and names the steps each carries out, probe 8 names step 1 against the tuple and step 2 against the `endswith` check in `pluralize`. They are short because the diffs are clean.

A true rubber stamp differs in kind, not in degree: **no hunk-by-hunk walk at all**, just a verdict with a sentence in front of it — "this looks correct and matches the plan", which is the phrasing the system prompt already names as not a review. Neither control does that.

**So the test to apply to future Reviewer output is: did it walk the hunks?** Not: was the walk long. A rejection is necessarily wordier, because it has to name the unaccounted thing *and* say what to do instead; scoring approvals against that length would push toward inventing objections, which is the failure mode the five guards exist to prevent.

**The end-to-end `fixture_repo_3` run is deliberately not queued behind this, and is conditional on the result.** If probe 1 is rejected, an end-to-end run asks a real second question: whether that judgment survives a real Planner and a real Implementer in front of it. If probe 1 is approved, the end-to-end run is pointless — it would spend calls hoping a shortcut happens to arise, so that a Reviewer already shown to miss the direct case can miss it again less legibly.

**This fired, and the rule held.** Probe 1 was rejected, the run followed, and it went green on attempt 1 — see 5.1 in Current status. The paragraph above is left in its pre-decision tense on purpose: it is the record that the criterion was fixed *before* the result was known, which is the only thing that makes it a criterion rather than a rationalisation.

### What the retry mechanism proved, together

The two probes measure the two halves of "retry on failure", and both halves are now independently verified.

- **`probe_reviewer.py`: 8/8.** The judgment *behind* a rejection is real — five unfaithful diffs rejected with actionable reasons, three faithful ones approved, including the two controls that test the rules most likely to produce a false rejection.
- **`probe_implementer.py`: 5/5.** The evidence *after* a failure reaches a real model, and the retry produces a correct fix — new edits, a green suite, the failing attempt's mechanism gone, and the boundaries the traceback named resolved.

**Together these are stronger evidence than one lucky natural end-to-end failure would have been, and the reason is repeatability rather than sample size.** A natural failure is incidental: it happens or it does not, it arrives in whatever shape the sampling produced, and the run containing it cannot be re-run to check the result. Both fixtures built to produce one failed to — fixture 2's Planner fenced off the trap, fixture 3's Planner named the mechanism — so waiting for an incidental failure was never a plan, only a hope. Each probe is **deliberate and repeatable**: the plan is fixed, the failure is constructed, and the measurement can be run again and compared.

**What this does not license.** Neither probe measures the *routing between* the halves — that a rejection becomes a `ReviewerRejection`, survives the baseline reset, and arrives in the Implementer's next prompt. `test_loop.py` covers that with stubs, and it has never run end-to-end against real models, because no real run has ever taken a retry. That is the one genuinely unexercised path left, and it is what a fail-then-recover run would still buy. See "Fixture design: the recoverability constraint" for the shape such a fixture has to have.

### An approving reason costs nothing downstream

Only a **rejecting** verdict becomes a `ReviewerRejection` (step J), so an approving verdict's `reason` reaches the event log and nothing else. That is why the prompt can demand a full attribution walk whichever way the model is leaning: on the common path it costs output tokens and a log line, and on the rare one that specificity is exactly what the blind-retrying Implementer needs. There is no retry-prompt bloat to trade against.

### `render_plan` is duplicated, on purpose

`reviewer.render_plan` is a second implementation, not an import of `implementer.render_plan`. Two differences, both following from the difference in role:

- **Steps are numbered and the numbering is load-bearing.** The Implementer numbers them for legibility; the Reviewer is told to *cite* them by number, and its reason becomes the Implementer's retry prompt. The number is the shared address between the two agents.
- **No section is ever omitted.** The Implementer's renderer drops an empty `Steps:` or `Constraints:` heading, correctly — a bare heading reads as a truncated prompt to someone being told what to do. For a Reviewer, `(none stated)` is *information*: it says the channel is empty rather than that the section was cut, which matters when the instruction is to check each entry in turn.

The alternatives were importing across two agent modules, which couples agents that are supposed to know nothing about each other, or a shared prompt module, which one function does not earn. `test_prompts.py` asserts the two renderings *differ* on an empty plan, so a future convergence fails loudly rather than quietly losing a property.

### The contract stays at `plan` and `diff`

No `repo_path`. `render_diff` uses `difflib`'s default three lines of context, so "did this gut behaviour the plan meant to preserve" is judged through a narrow window — that is a real limit, not an oversight. It is accepted because the residuals listed above are all visible in the diff itself, because full-file replacement on a small fixture file renders nearly the whole file anyway, and because this project's habit is to widen a contract only when it is demonstrably *incoherent* without the field (which the Planner and Implementer were, and this is not).

**Revisit trigger, recorded so the decision can be reopened on evidence rather than on unease:** if Phase 5's unfaithful-diff fixture shows the Reviewer cannot judge without more context, that is the evidence to widen on. Not before.

### A known hole: a plan that targets a test file

Nothing prevents a Planner from putting a test file in `target_files` — the only validator on `Plan` is that the list is non-empty. If it does, the scope check passes an assertion edit straight through, and the Reviewer would *approve* it under a pure faithfulness reading, since the plan asked for it.

**Left unbuilt, deliberately.** The fix does not belong in the Reviewer: rejecting a test edit regardless of what the plan says is the Reviewer enforcing policy rather than fidelity, which is a different job from the one it has. The right fix is a **field validator on `Plan` rejecting test paths in `target_files`**, beside the non-empty one in `state.py` — mechanical, at the boundary, and it fails with a `ValidationError` the repair turn hands straight back to the Planner. Not built in 3B because 3B is the Reviewer, and because the failure has not been observed. Named here so it is a decision rather than an oversight.

---

## Retry vs. backoff — do not conflate

- **Agent retries** (Reviewer rejection, Tester failure) → route back to Implementer with evidence. No delay. The model is stateless; waiting changes nothing about the next output.
- **HTTP retries** (408, 429, 5xx) → live in the LLM client wrapper, with exponential backoff and jitter. Never in the agent loop. `google-genai` retries **nothing** unless `http_options.retry_options` is set, so this is opt-in rather than a default being accepted — one layer, deliberately configured, not two layers fighting.
- **Parse repairs** (malformed JSON, failed validation) → also in the LLM client wrapper, added in 3A. One bounded retry that hands the model its own validation error back. **No delay**, unlike the HTTP backoff one layer down: that exists because the *server* needs time to recover, and a model holds nothing that waiting would improve. It is not a resend either — the repair turn is a different, better-informed request carrying an error message the first one could not have had.

A parse failure must not become an agent retry. The loop's `evidence` says *the diff was wrong*; a model that emitted prose instead of JSON produced no diff at all, so there is nothing for `ReviewerRejection` or `TesterFailure` to describe and nothing for `attempt_count` — whose whole meaning is "how many diffs has this task produced" — to count. This is also why the scope check's precedent does not apply: that reuses `ReviewerRejection` because the corrective action is identical, and here it isn't.

---

## Benchmark arms

Three arms, run against the same targets, so each comparison varies one thing.

| Arm | What runs | Status |
| --- | --- | --- |
| A | `baseline.py`: one agent, no Planner, no Reviewer, auto-approve, no livelock check, no scope rules | built and tested with stubs; never run live |
| B | `harness_arms.py --arm B`: the harness without the Reviewer (a pass-through that approves without a model call): Planner, Implementer and Tester, with C's loop, livelock check, scope rules and attempt cap | built and tested with stubs; never run live |
| C | `harness_arms.py --arm C`: the full harness: Planner, Implementer, Reviewer, Tester | built and tested with stubs; never run live |

**Held identical across all three:** the model (`llm.DEFAULT_MODEL`), the Implementer's `MAX_TOKENS` and thinking level, `MAX_ATTEMPTS`, the real Tester, a baseline reset at the top of every attempt, `.git` exclusion, and the `approve` seam, auto-approved in all three.

**The Implementer prompt is identical in B and C**, including its mentions of a reviewer, so B vs C measures whether the Reviewer runs with the prompt held fixed.

**What the comparisons measure.** A vs C: does the structure help at all. B vs C: does the Reviewer help.

**No arm runs through `cli.py`.** The shipped CLI always stops at a human gate and must not grow a flag that skips it (see "`--stub`"). Every arm is driven by a script at the repo root, like the probes.

### Task set

Nine real upstream bugs, each a small fix commit that added or changed a test. Manifest (full SHAs, source files, red tests, prompt sizes): `D:/Agentic_Project1/experiments/bench/hidden/manifest.json`, outside every git repository like the other experiment clones.

| Split | Tasks (repo + fix commit) |
| --- | --- |
| dev (3) | schema `d81871b`, xmltodict `f0322e5`, inflection `1969b3a` |
| eval (6) | humps `2a462a7`, addict `302c8ae`, shortuuid `496cddf`, tokenize-rt `cb7836b`, cfgv `2876a67`, mccabe `0a42f06` |

**What "hidden" means.** The model's copy is the buggy parent commit plus the fix commit's test files, so the failing test is **visible** — the parent alone is green, because upstream had no test for the bug yet. The upstream copy of those test files is stored in `hidden/<task>/` and **restored over the run directory before scoring**, so a run that edits a test cannot change its score. Pre-checked under the harness venv: fix commit green, task copy red only on the fix's own tests, no extra dependencies, 1–5 changed source lines.

python-slugify has no task: its recent fixes have 39–58k-character repos and fail only inside subtests (F9), and its older ones keep their tests in `test.py`, which pytest does not collect.

**Two caveats.** Dev prompts are much larger than eval ones — schema 48.6k and xmltodict 37.4k characters of Planner message against 6–17k for every eval task — so dev results do not predict eval cost. **cfgv `2876a67` is borderline**: its test asserts a new error message (`is not a file`), so it is partly a wording change rather than a behaviour bug.

---

## Tech stack

| Part           | Choice                                                            |
| -------------- | ----------------------------------------------------------------- |
| Language       | Python 3.14, no agent framework                                   |
| LLM transport  | official `google-genai` SDK — see "The one dependency, and where it stops" |
| LLM model      | `llm.DEFAULT_MODEL` — `gemini-3.6-flash` today (Google routes `gemini-3.5-flash` requests to it), `response_json_schema`, dynamic thinking (default) |
| State          | Pydantic v2                                                       |
| Diff rendering | `difflib.unified_diff` over `edits` + baseline                    |
| Test execution | `subprocess` running `pytest`                                     |
| Event log      | append-only `.jsonl`, one file per run                            |
| Baseline reset | delete `runs/<task_id>/`, re-copy fixture via `shutil.copytree`   |
| Entrypoint     | `python cli.py --repo <any/path> [--description "<text>"] [--failure "<output>"] [--stub]`, or the original `--task <path/to/task.json>` |

The Tester runs the **whole** fixture suite, not just the target test — "fix it without breaking anything else" is only verifiable against the full suite. Before invoking pytest it clears `__pycache__` and `.pytest_cache` in the run directory. Stale caches cause phantom results.

It invokes `[sys.executable, "-m", "pytest"]` so the subprocess uses the harness's own interpreter and virtualenv, with a **120s default timeout**. A hung pytest returns a `TestResult` with `passed=False`, `exit_code=-1` (deliberately outside pytest's own 0–5 range), and the timeout recorded in `traceback` — it never propagates an exception into the loop.

`failed_tests` is read out of pytest's "short test summary info" section as text. Not the reporting API, which would couple the harness to a pytest version, and not a plugin, which would be a second thing to keep working. That section is only consulted for exit code 1 — see `summary_parsed` under "TestResult".

**The section is stable; it is not necessarily uncoloured.** A target repo can force colour in its own pytest config, which defeats every column-zero anchor in the parser — found as 6.2's F4 and fixed in 6.5. `tester.strip_ansi` removes CSI sequences from a **copy** before `_parse_summary` and `_failure_detail` run; `TestResult.stdout` keeps the raw bytes. The choice of text parsing stands, and the cost of it is now one named function rather than an unexamined assumption.

The Tester does not own the run directory — `harness/workspace.py` does. The Tester is handed a prepared `repo_path` and never creates, resets, or deletes it.

---

## Repo layout

Repo root is `mach/`.

```
harness/
  state.py          TaskState, Plan, FileEdit, ReviewVerdict, TestResult, evidence types
  events.py         EventLog — append-only JSONL writer
  workspace.py      run directory: prepare, reset, apply. The fixture is never written to
  loop.py           run_task, render_diff, MAX_ATTEMPTS — routing, retries, escalation
  llm.py            LLM client wrapper (HTTP retry/backoff lives here)
  agents/
    base.py         Agent ABC, CONTRACTS table, requires/produces assertion
    stubs.py        scripted stand-ins for the three LLM-backed agents
    planner.py      real, 3A
    implementer.py  real, 3A
    reviewer.py     real, 3B
    tester.py       real since phase 1, never stubbed
tasks/
  fixture_repo_1/   plain directory, no git
    task.json       task_description + failure_input
  fixture_repo_2/   the naive-fix trap — Planner measurement, see the fixture 2 finding
  fixture_repo_3/   the unfaithful-in-scope diff — a shortcut the test suite cannot detect
runs/               gitignored — working copies, one per task_id
logs/               gitignored — <task_id>.jsonl, one per run
transcripts/        committed — the logs of runs this file cites as evidence
tests/              tests for the harness itself — zero API calls, no API key
cli.py
probe_reviewer.py   instrument, not a component. Nine real calls; outside tests/ on purpose
probe_implementer.py instrument. One real call (the plan is cached from a transcript)
probe_plan.json     fixture 3's plan, fetched by probe_reviewer --fetch-plan. One call
probe_plan_fixture_1.json  fixture 1's plan, lifted from a saved run. No call spent
baseline.py         benchmark arm A: one agent, no Planner, no Reviewer. Outside cli.py on purpose
harness_arms.py     benchmark arms B and C: the real run_task, auto-approved, Reviewer swapped in B
scorer.py           scores one finished run: restore the hidden tests, run the suite, one JSON row
driver.py           runs the arms over the task set, resumable; stops cleanly on quota or a bad key
CLAUDE.md
README.md
```

---

## Working conventions

- Small commits, one logical change each, real messages.
- **A run whose result is cited in this file gets its log copied to `transcripts/`.** `logs/` is gitignored, so a citation into it dangles for everyone but the machine that produced it. `logs/` is where `EventLog` writes; `transcripts/` is what the repo keeps. Cite the second.
- **No phase numbers in commit messages.** Say what the commit does, not where it sits in the plan: phase labels drift as the table is revised, they mean nothing to anyone reading `git log` without this file open beside it, and the same phase spans several commits anyway. The phase belongs in CLAUDE.md, which is where it can be corrected. `git log` should read as a history of changes, not of project management.
- Write the test before or alongside the code, not after the phase.
- Stub agents before real agents. Phases 1–2 make **zero API calls** — if a session is adding an API call before Phase 3, something is out of order.
- Prefer boring, readable code. This project is read by interviewers.
- When a decision is made mid-session that contradicts or extends this file, update this file in the same commit.

---

## Current status

Update this section at the end of every session. It is the first thing to read next session.

**Phase:** 6 — **in progress. 6.0.5, 6.1, 6.2, 6.3, 6.4 and 6.5 are done: the CLI takes any repo, 6.2's python-slugify run went green end to end after producing four findings and one crash, 6.3 fixed the one finding that stopped a run, 6.4 confirmed that fix against a real `.git` object store, and 6.5 fixed F4 so a failing attempt on a colour-forcing repo can retry instead of escalating.** `cli.py` now takes any repo on disk and finds the failure itself. Phase 5 is complete as recorded below.
**The governing rule is now live, not hypothetical:** 6.2 has said what broke, so 6.3 may finally build — but only the one thing 6.2 evidenced. See "The next two steps" below, which are fixed and should not be re-chosen from 6.3's table.
**Last completed:** Tasks 6.0.5 (auto-discover the failure) and 6.1 (generalise the CLI), built together in one session because both change nothing but `cli.py`'s argument handling and splitting them would have been two commits touching the same forty lines. Suite is **464 tests, all passing, 219s** — up from 432; the 32 new ones are all in `test_cli.py` (66 → 98).

**The acceptance run is spent and it passed. Three calls, `succeeded` on attempt 1** — `transcripts/fixture_repo_1_20260909T133925Z.jsonl`, run as `python cli.py --repo tasks/fixture_repo_1` with no `--failure` flag and no `task.json` consulted at any point.

**The strongest single line of evidence is not the status, it is the diff.** The rendered diff is **byte-identical** to the one from `transcripts/fixture_repo_1_20260823T064826Z.jsonl`, the known-good run driven from the hand-typed `task.json` two weeks earlier. Same `target_files` (`pricing/discounts.py`), same one-character fix (`>` → `>=` in `tier_for`), same call count, same attempt. Auto-discovered evidence and hand-typed evidence put the Planner and the Implementer in the same place, and that is checkable rather than asserted: compare `diff_rendered` across the two transcripts.

The event order in the log is worth reading once, because it is the whole safety argument made visible — `test_run_started` and `failure_discovery` appear at attempt 0 **before** `run_started`, and the Planner's `llm_request` is the first thing inside the run. Discovery is setup; the loop still plans first.

### 6.0.5 + 6.1 — what changed, and how it was verified

**New CLI shape.** `python cli.py --repo <any/path> [--description "<text>"] [--failure "<output>"] [--stub]`. `--task tasks/fixture_repo_N/task.json` still works and is tested. The two are mutually exclusive, one is required, and `--description`/`--failure` with `--task` is refused rather than ignored. Argued in full under "task_id" and "Auto-discovering the failure".

**New in `cli.py`:** `discover_failure`, `resolve_repo`, `repo_dir_name`, `build_parser`, `DEFAULT_DESCRIPTION`, `UNNAMED_REPO`, and the `failure_discovery` event. **In `state.py`:** `make_task_id`'s parameter renamed `fixture_dir_name` → `repo_dir_name`, plus its docstring. That is the entire diff outside `cli.py` and `test_cli.py`. The loop, the four agents, the contracts, the evidence types, and the six invariants are untouched — `git diff --stat` is the check, not this sentence.

**The failure text itself, diffed line by line.** Auto-discovery against `tasks/fixture_repo_1` was compared against the `failure_input` a human captured into `task.json` in Phase 1.3 — both in isolation before the run, and from the real run's own `failure_discovery` event afterwards. Both give the same answer. **Three lines differ out of thirty, and neither difference is something discovery did:**

- `plugins: anyio-4.14.2` — present in the discovered output, absent from `task.json`. `anyio` arrived as a transitive dependency of `google-genai` in 3A.1, a week *after* the fixture was captured. Confirmed by running `python -m pytest` by hand in the fixture directory: a hand capture taken today prints the same line. The committed `task.json` is the stale one.
- `in 0.23s` vs `in 0.37s` — wall clock.

Everything else matches character for character, **including `rootdir: D:\Agentic_Project1\mach\tasks\fixture_repo_1`** — the stale path that points at the source repo rather than the run directory, which this file calls the clearest case of noise worth keeping. That is the evidence the discovery runs against the target repo rather than the copy, and it is pinned in `test_it_reproduces_the_hand_captured_failure_input` with both exempt lines named in the docstring, so the claim cannot quietly weaken later.

**A property was found to be broken and fixed before it shipped, which is worth recording as the process working.** The first full-suite run after discovery landed came back **6 failed** — all in `test_tester.py`, all one cause: discovery runs pytest *inside* the target repo, pytest writes `__pycache__` and `.pytest_cache` there, and `test_a_run_leaves_no_caches_in_the_fixture` and `test_the_fixture_directory_is_untouched_by_a_run` assert exactly that this never happens. The design note written *before* the run had called this "one cost, stated rather than hidden" — which was wrong, because the cost was not being paid to anyone, it was breaking an invariant the suite already enforced. `discover_failure` now clears both on the way out in a `finally`, two tests pin it (including one that kills the Tester mid-run), and the full suite is green. **The general lesson is the project's own: an argued-for cost is worth checking against the test suite before it is written down as acceptable.**

**What else was verified, all without a key:** `--repo` driven end to end through `main` under `--stub` (discovery, banner, task_id from the repo's own directory name, and the discovered bytes landing in `TaskState.failure_input` verbatim); a green repo reporting plainly and exiting 0 with nothing copied to `runs/`; an uncollectable suite refused rather than turned into a task; `--failure` skipping discovery entirely; `--description` defaulting and being overridable; `--repo .` and trailing separators resolving to real directory names; and the old `--task` shape still reading both fields from the file.

**What 6.1 deliberately did not do.** `prepare_run_dir` was checked and left alone — it `copytree`s whatever it is handed and has never known about fixtures. It will copy `.git` and `.venv` out of a real repo, which is a 6.2 finding waiting to happen rather than a fixture-era assumption, and per Phase 6's governing rule that is a candidate, not a commitment, until 6.2 produces it.

**The rule governing the rest of this phase, restated because it is easy to lose:** build nothing beyond 6.1 until 6.2 has actually run and said what broke. Localized edits, replanning, retrieval before planning — all candidates, none commitments. Same discipline as the fixture rulings: do not engineer a fix for a failure you are imagining.

(An earlier run in the same session failed one test in `TestGeneratorsAgainstTheRealTester`, then a re-run failed a *different* one in the same class, with the class taking 662s for five tests. Both pass in isolation in ~6s and the clean full run is green. That is the contention signature the note at the end of this section describes — recorded here because it recurred and cost time to rule out, not because anything changed.)

### 6.2 — the first real-repo run. Four findings, one crash, one call spent.

**Target:** `un33k/python-slugify` at a shallow clone — 36 tracked files, flat
layout, 125 tests green at baseline, one dependency (`text-unidecode`). A
one-character bug was injected into `slugify/slugify.py`'s `_modern_truncate`
(`if next_length <= max_length:` → `<`), reddening 3 tests across 2 classes. The
same *kind* of bug as `fixture_repo_1`'s, chosen mechanically from four
candidates by applying each to a scratch copy and running the suite — the rule
every fixture was held to.

**Why this repo and not a better-known one.** `itsdangerous`, `tomli` and
`python-dotenv` were all measured and rejected: each uses `src/` layout, which
needs an install to import, and an install — editable or not — resolves imports
to the **original clone**, not to `runs/<task_id>/`. The Implementer's edits
would be invisible to the Tester and the suite could never go green regardless
of how correct the fix was. Flat layout is the only shape the current design can
actually measure. Recorded so the choice reads as a scoping decision with a
reason rather than as picking a repo that happens to work.

**Everything below was confirmed before or during the run, not reconstructed
after it.** Full working record in the session's findings file; the four
findings are summarised here because they are what 6.3 must choose from.

#### One gap with four faces

The root cause under F1–F3 is single and worth stating once: **the harness treats
the target as a directory of bytes, with no model of it as a project.** It has no
notion of what is source, what is version-control metadata, or what environment
the suite needs. Three fixtures could never show this, because a fixture is a
directory built to be exactly that and nothing more.

**F1 — `.git` is copied, and it is fatal, not merely wasteful.** `IGNORED` skips
`__pycache__` and `.pytest_cache` and nothing else, so 29 of the 65 files
`list_repo_files` discovered were `.git/` internals. The prompt cost is minor
(they are non-`.py`, so they are listed by path only). **The fatal cost is the
reset.** Git marks pack files read-only on Windows; `reset_run_dir` calls
`shutil.rmtree(target, ignore_errors=True)`, which fails on them and **swallows
the failure silently**; the run directory survives as a `.git`-only skeleton; and
`prepare_run_dir`'s `copytree` then dies with `FileExistsError`. Measured: 3
read-only files under `.git/objects/pack/`, and `rmtree` re-run by hand reports
`still exists: True`.

**A claim made here when F1 was first written has since been checked against
the code and found wrong. It is corrected rather than quietly edited, because
the correction is the more useful record.** The original text said
`ignore_errors=True` means invariant 4 "can fail without saying so", and that a
partial reset could silently contaminate the ground every diff is rendered
against. **It cannot.** A non-empty directory cannot be removed, so any file
surviving `rmtree` leaves `target` in place, and `prepare_run_dir`'s `copytree`
then raises `FileExistsError` on `makedirs(dst, exist_ok=False)`. Verified in
6.3 by building a run directory with a read-only file inside it, calling
`shutil.rmtree(..., ignore_errors=True)`, and confirming the copy raises rather
than proceeding. **Every partial reset is loud.** Invariant 4 is structurally
protected — by `prepare_run_dir`'s existence guard, not by `rmtree`.

What is genuinely wrong with `ignore_errors=True` is narrower and worth keeping:
**the error it produces names the wrong thing.** `FileExistsError` on the run
directory reads as "a stale directory from a crashed run or a genuine id clash",
which is exactly what this file documents that guard as being for. It does not
say "the reset failed to delete three read-only files", and the distance between
those two messages is most of the time 6.2's crash cost to diagnose.

The crash landed on attempt 1 rather than attempt 2 because of the deliberately
redundant first-attempt reset — that redundancy converted a latent problem into
an immediate crash, which is that trade paying off exactly as argued under
"Order of operations".

**F2 — the inline cap is per-file, and the real cost is many small files.**
`MAX_INLINED_CHARS = 40_000` drops an oversized file to a path-only listing, but
there is no total-prompt budget above it, so N small files all go in regardless
of N. Measured through the real `render_repo`: `fixture_repo_1` is 4,397
characters (~1,099 tokens); python-slugify is 40,530 (~10,132) — **9.2x at 36
tracked files**, none of which trips the per-file cap. `tomli` shows the other
axis at 1,038 files and ~26,371 tokens, because it vendors the `toml-test` data
corpus. **The Planner was still correct at 9.2x**, so this is a measured cost,
not yet a measured failure.

**F3 — no per-target dependency isolation.** `Tester._invoke_pytest` runs
`[sys.executable, "-m", "pytest"]`, which is the *harness's* interpreter — a
decision this file defends explicitly and which is right for a fixture. On a real
repo it means the target's dependencies must be installed into the harness's own
virtualenv. There is no per-target environment and no step that reads the
target's dependency declaration. Three consequences: the harness cannot run a
repo whose dependencies it lacks; every target permanently mutates the harness's
environment, with no way to express that repo A needs `attrs<22` while repo B
needs `attrs>=23`; and a target can shadow something `harness/` imports, because
they now share an interpreter. The `src/` layout problem above is the second half
of this same gap.

**F4 — the Tester's summary parser is defeated by the target's own pytest
config, and this is the sharpest finding.** python-slugify's `pyproject.toml`
carries `addopts = "--color=yes"`, which forces ANSI colour even when stdout is
not a tty — and the Tester always captures via `subprocess`. Measured: **526
escape sequences** in 6,196 characters. `_parse_summary` anchors both its header
check (`line.startswith("=")`) and its row regex (`^(?:FAILED|ERROR)\s+`) at
column zero; the real lines begin `\x1b[36m\x1b[1m====` and `\x1b[31mFAILED`.
Same parser, both repos:

| repo | exit | `failed_tests` | `summary_parsed` |
| --- | --- | --- | --- |
| `fixture_repo_1` | 1 | `['tests/test_orders.py::test_an_order...']` | **True** |
| python-slugify | 1 | `[]` | **False** |

**The consequence is a misrouted status, not a lost list.** `summary_parsed is
False` is the `escalated_broken_suite` branch — "pytest could not collect" — and
that is simply untrue here: 125 tests collected and 3 failed. So on any repo that
forces colour, **a failing attempt escalates instead of retrying, and the retry
path is structurally unreachable.** The `--repo` banner also under-reports, which
is how this surfaced: it printed `auto-discovered (0 failing test(s))` while
handing the Planner a `failure_input` describing three.

This is the category 6.2 was told to expect — "a fixture-specific assumption that
doesn't hold for real code" — but one layer deeper than predicted. It is not in a
prompt. It is in the Tester's text parser, the one component with no model in it,
and **it falsifies this file's own defence of text parsing**: "the summary
section is stable, human-readable, and already on stdout". It is stable; it is
not uncoloured. No fixture has `addopts`, so nothing could have caught this.

#### The Planner was right, and the prediction written before the call was wrong

Recorded as a miss, because the prediction was written down to be checkable.
`tests/test_release.py` rebinds the public API at module scope —
`slugify = partial(public_slugify, algorithm="modern")` — and `_is_test_file`
hides test bodies, so the Planner never sees that line. Read against the source
it *can* see, the traceback's `slugify(...)` calls default to `algorithm='legacy'`
and point at `_legacy.smart_truncate`, which is baseline-correct. The prediction
was that it would follow that and produce an unrecoverable plan.

It did not. It targeted `slugify/slugify.py`, named `_modern_truncate`, specified
the exact one-character fix — and wrote the constraint *"Do not change the legacy
slugify implementation in `slugify/_legacy.py`"*, which shows it **considered the
legacy dispatch and ruled it out** rather than never noticing it. It did this on
a 9.2x prompt, from a `failure_input` carrying 526 escape sequences, with
`failed_tests` empty.

**A fourth Planner rescue, and the strongest of the four** — the first three were
fixtures whose author knew where the trap was; this is someone else's dispatch
logic. Consistent with the fixture-2 and fixture-3 findings rather than a new
claim, but on code the project did not write.

**What this run did not measure.** It died at step C of attempt 1, so the
Implementer, the Reviewer, the gate, the Tester's verdict and the retry path are
all still unmeasured on a real repo. **One API call spent.** The Planner-context
question 6.2 asked first is answered — 9.2x, still correct — and everything
downstream is owed.

#### One observation from an aborted re-run, kept separate from the findings

A second attempt at the run died on an upstream **503 UNAVAILABLE** from Gemini
("currently experiencing high demand") during the Planner call. The SDK retried,
exhausted, and `_transport_error` classified it `retryable=True`. **The harness
did what it was designed to do; this is not a finding and is not in the F1-F4
list.**

What it did surface, for the first time by observation rather than by argument:
the log ends at `llm_request` with **no `run_finished` and no report** — the
human gets a traceback. That does not contradict "every halt goes through
`_halt`", which is a claim about the paths *the loop can take*; an exception
inside an agent bypasses `_halt` by construction, exactly as documented for
`AgentContractError` and `LLMResponseError`. What is new is only that it has now
been seen. It sharpens the standing open question about a seventh `Status`
without answering it — and a run killed by someone else's capacity problem is
arguably the clearest case for leaving it as a stack trace, since no `Status`
would describe it better than the traceback does. **Deliberately not added to the
plan below.**

#### The confirming run: green end to end on a real, undesigned repo

`transcripts/python-slugify_20260924T202404Z.jsonl`. **`succeeded`, attempt 1 of
5, three API calls, `reason: the suite is green.`** Run against the same clone
with `.git` stripped, so it is a clean measurement of everything the crash cost —
Implementer, Reviewer, gate, apply and Tester, none of which had ever run against
a repo this project did not write.

**This is the first full end-to-end success on a real repo**, and the headline
for 6.2's python-slugify arm. 36 files, a ~9,900-token Planner prompt, someone
else's code, someone else's test conventions, a bug injected into live branching
logic — and three calls, first attempt, green.

**The Planner reproduced its answer on an independent call.** This is a different
run from the `.git` crash, with its own fresh Planner request; nothing was
carried over. It arrived at the same `target_files` (`slugify/slugify.py`) and
the same function (`_modern_truncate`), and specified the same one-character
change. The rendered diff is exactly the revert:

```
-            if next_length < max_length:
+            if next_length <= max_length:
```

**Twice in a row, on separate calls, it avoided the `_legacy.py` dispatch trap** —
the one predicted in writing beforehand, where the traceback's `slugify(...)`
calls read as the legacy default because `_is_test_file` hides the
`partial(public_slugify, algorithm="modern")` rebinding.

**One precision worth keeping, because the two runs are not equally strong
evidence.** The first run's plan carried the constraint *"Do not change the
legacy slugify implementation in `slugify/_legacy.py`"* — explicit proof it
considered the legacy dispatch and ruled it out. This run's constraints are
generic (*"Only `slugify/slugify.py` is edited"*, *"No test files or other source
files are modified"*) and never name `_legacy.py`. So: **the correct outcome
reproduced twice; the visible deliberation appears only in run one.** Two data
points for "it lands on the right file", one for "it demonstrably reasoned about
the wrong one". Do not cite the second as if it were the first.

**F4 stayed invisible on this path, exactly as predicted.** The pre-run note said
a correct fix would show F4 only in the banner's `auto-discovered (0 failing
test(s))`, and so it proved: the final Tester call returned `exit=0`, and pytest
prints no summary section on a pass, so the Tester's exit-code-0 branch sets
`summary_parsed=True` without `_parse_summary` ever running. **F4 remains proven
by direct measurement of the parser, not by a run** — and the retry path is still
unexercised against a real model, because this went green on attempt 1. That is
now the fourth fixture-or-repo in a row to be fixed first try.

#### What 6.2's python-slugify arm settles, and what it does not

**Settled:** the harness works end to end on a real, undesigned repository. The
Planner-context question the roadmap asked first is answered — a 9.2x prompt did
not degrade it. The plan/implement split held on code nobody here wrote.

**Not settled, and not touched by this run:** F1 (still the next real step, see
below), F2 as anything beyond a measured cost, F4's claim that the retry path is
unreachable on colour-forcing repos, and the retry path itself on a real model.
One green run is evidence about one green run — the same reading this file gave
`fixture_repo_1`'s.

### 6.3 — F1 fixed. `.git` is excluded, and `rmtree` was deliberately left alone.

**One behavioural change**, in `harness/workspace.py`: `_CACHE_DIR_NAMES` became
`_EXCLUDED_DIR_NAMES` and gained `.git`. Both users of that constant are in the
same module — `IGNORED` (what `copytree` skips) and `list_repo_files`'s `skip`
(what a prompt lists) — so one edit closes both halves of F1. They share the
constant deliberately: a directory skipped by one and walked by the other would
put files in a prompt that are not in the run directory the plan targets.

Nothing else moved. Not the loop, not the agents, not the contracts, not
`reset_run_dir`. Suite is **468 tests, all passing, 199s** — up from 464 by
exactly the four added below.

#### The `rmtree(ignore_errors=True)` decision: not bundled, and why

The plan said to decide this with the code open rather than defer it again. The
decision is **no**, and the reasoning changed on inspection, which is the part
worth recording.

**The argument for bundling it was that invariant 4 could fail silently. That
argument does not survive contact with the code.** See the correction folded into
F1 above: a non-empty directory cannot be removed, so any survivor keeps `target`
in place and `copytree` raises. Verified by building the exact condition —
read-only file, `rmtree(ignore_errors=True)`, then the copy — and watching it
raise. Every partial reset is loud, and invariant 4 is protected by
`prepare_run_dir`'s existence guard rather than by `rmtree`'s error handling.

**With that gone, three reasons to leave it alone and one to revisit later:**

1. **Excluding `.git` removes the only observed source of read-only files.** The
   pack files were the trigger; they are no longer copied.
2. **Changing it now would be engineering for an imagined failure** — the exact
   thing Phase 6's governing rule forbids, and the same discipline as the fixture
   rulings. There is no longer a known repository shape that reaches it.
3. **One fix at a time is the phase discipline**, and 6.4 should confirm one
   thing. Two changes in this commit would make a green 6.4 ambiguous about which
   one earned it.

**What is real, and is a diagnosability defect rather than a correctness one:**
the `FileExistsError` names the run directory and reads as a stale directory or
an id clash, not as a failed reset. **Named revisit trigger:** if a real
repository produces `FileExistsError` at `reset_run_dir` *after* this fix, that
is the evidence, and the fix is an `onexc` handler that clears the read-only bit
and retries — not `ignore_errors=False`, which would merely trade a misleading
error for a different misleading error one frame earlier.

#### The tests, and the one that matters

`test_tester.py` gained `TestVersionControlIsNotCopied`, four tests. It builds
its own source tree with a `.git` in it rather than using a fixture, because the
three fixtures have no `.git` and giving one a fake `.git` to satisfy a test
would be fixture-shaped evidence for a real-repo problem.

- `.git` is not copied — and `.gitignore` **is**, because that is a file the
  project tracks rather than version-control internals.
- `.git` is not listed by `list_repo_files`, so the listing and the copy agree.
- **`test_a_read_only_pack_no_longer_breaks_the_reset`** — the 6.2 crash, pinned.
  A read-only pack file, then two consecutive `reset_run_dir` calls; the second
  is the one that used to fail.
- The *source* repository keeps its own `.git` after a prepare and a reset.

**Three of the four were confirmed to fail without the fix**, by reverting the
one-line change and re-running — and the third fails with `FileExistsError:
[WinError 183]`, the same error as the real run in
`transcripts/python-slugify_20260923T113847Z.jsonl`. A regression test that
passes before and after pins nothing; these were checked rather than assumed.
(The fourth passes either way, correctly: the source was never touched.)

#### What was deliberately not done

**`.venv`, `.tox`, `node_modules` were not added.** 6.1 speculated that a real
repo would bring `.venv` along; python-slugify did not have one, so nothing has
evidenced it. Adding directory names on the strength of a guess is how a skip
list becomes folklore. F1 named `.git` because `.git` stopped a run.

**F2 and F4 were not touched**, per the plan.

#### Where experiment clones live, and why not in Temp

**Standing rule, learned by losing one: a target repo staged for a run lives in
`D:/Agentic_Project1/experiments/`, never under the system Temp directory.**

6.4's clone was first staged in the session scratchpad under
`C:/Users/DELL/AppData/Local/Temp/...`. Ten days later Windows Temp cleanup had
emptied it — the directory skeleton survived, every file inside was gone, and the
session's working findings file went with it. Nothing about the harness was at
fault and nothing in it needed changing.

`experiments/` sits beside `mach/` rather than inside it, deliberately: a cloned
repo carrying its own `.git` inside the harness repo is the nested-repo problem
that Phase 1's "fixtures are plain directories with no git" decision exists to
avoid, and `experiments/` is outside any git repository at all, so a staged clone
cannot be committed by accident.

**What was lost and what was not.** The scratchpad findings file for 6.2 is gone.
Its substance is not: F1–F4, the pre-run predictions, the Planner results and the
`rmtree` correction were all folded into this file as they were established, and
the transcripts were already in `transcripts/`. **The lesson is the convention
rather than the cleanup: fold findings into CLAUDE.md as they are established,
not at the end of a session.**

#### The model changed: runs from here use `gemini-3.5-flash`

`llm.DEFAULT_MODEL` is now **`gemini-3.5-flash`**. It was `gemini-3.6-flash` for
every run recorded above. One line in `harness/llm.py`; nothing else in the
harness moved.

**Every Planner result in this file up to and including 6.2 was produced by
`gemini-3.6-flash`. Runs from 6.4 onward are a different model, so comparisons
across that line are not like-for-like.** This matters for exactly one claim and
it is worth naming precisely rather than generally: the python-slugify Planner
result — right file, right function, `_legacy.py` dispatch trap avoided twice on
independent calls — is **two data points about 3.6, not about this harness's
Planner in the abstract**. If 6.4's Planner lands in the same place, that is a
*third* data point and a *first* for 3.5, not a third for the same thing. If it
lands somewhere else, the model change is a live confound and must be named
before the prompt is blamed.

The four findings are unaffected, and the reason is that none of them is about a
model. F1 is `copytree` and `rmtree`; F2 is a character count through
`render_repo`; F3 is `sys.executable`; F4 is a regex against ANSI escapes. All
four were measured without a model in the path, and all four stand.

**On the standing rule against model literals in this file.** The rule — *"do not
reintroduce a model literal into a test or into this file; name
`llm.DEFAULT_MODEL`"* — exists to stop a literal going stale while the constant
moves, which is exactly the three-homed drift 3B had to clean up. Two literals
here are deliberate exceptions, and the distinction is drift-exposure:

- The **Tech stack** row says "`gemini-3.5-flash` today", and the hedge is doing
  real work — it is a snapshot that must be updated with the constant. It was
  updated in this change.
- The **historical** claims above name `gemini-3.6-flash` as the model that
  produced a specific recorded result. Those **cannot** go stale, because the past
  does not change. A note saying "6.2 used `llm.DEFAULT_MODEL`" would be worse
  than useless: it would silently re-point at whatever the constant becomes and
  misattribute old evidence to a new model.

The rule still holds everywhere else, and in particular **no test may pin a
literal to mean "the default"** — `test_llm.py` asserts
`LLMClient().model == DEFAULT_MODEL`, which is why this change broke nothing. The
two `gemini-2.5-flash` strings in `test_llm.py` are a *fake SDK's* served model
version, and `test_the_served_model_version_is_preferred_over_the_configured_id`
depends on their being unrelated to the default — binding them to the constant
would destroy the test.

**Known stale, deliberately out of scope for this change:** `README.md`'s model
row still reads `gemini-3.6-flash`. This change was scoped to one line of
`harness/llm.py` plus this file.

### 6.4 — F1 confirmed resolved against a real object store

`transcripts/python-slugify_20261006T103620Z.jsonl`. **`succeeded`, attempt 1 of
5, three API calls, `reason: the suite is green.`** Target was a fresh
`git clone --depth 1` at `experiments/git_intact/python-slugify` with **`.git`
left in place on purpose**, carrying 3 read-only pack files — the exact condition
that killed the 6.2 run. Same one-character bug in `_modern_truncate`.

Every claim below was read out of that log rather than inferred from the run's
console output.

#### F1 is resolved, and the proof is a single event

**`baseline_reset` appears at attempt 1 and the run continued past it.** That is
the call — `reset_run_dir` → `rmtree` → `prepare_run_dir` → `copytree` — which
raised `FileExistsError: [WinError 183]` in
`transcripts/python-slugify_20260923T113847Z.jsonl`. In 6.2 the log ends there.
Here it is followed by the Implementer, the Reviewer, the gate, apply, and a
green Tester.

Three further checks, all against disk rather than argument:

- The run directory `runs/python-slugify_20261006T103620Z/` exists and **has no
  `.git`** — the copy half of the fix, confirmed on a real object store rather
  than the synthetic one the unit test builds.
- The **source clone still has its `.git`**, with all 3 pack files still
  read-only. Excluding it from the copy did not mean touching the original.
- `list_repo_files` saw **36 files, 0 under `.git/`** (6.2's crash run saw 65
  files, 29 of them `.git/` internals).

**This is what the unit test could not buy.** `test_a_read_only_pack_no_longer_breaks_the_reset`
pins the mechanism against a `.git` the test writes itself; this is a real
object store, a real `--depth 1` clone, and the whole loop running through it.
6.4's stated job was "don't assume the fix worked because it seems like it
should", and the answer is that it did.

#### The Planner result — a first data point for 3.5, not a third for 3.6

All three calls report `model=gemini-3.5-flash`, `stop_reason=STOP`,
`parse_attempts=1`. **This is the first run on 3.5. The two python-slugify
Planner results recorded above were produced by `gemini-3.6-flash`, and these
are not like-for-like — do not cite this as a third data point for the same
thing.** See "The model changed" above for why that line matters.

What 3.5 produced:

```
target_files: ['slugify/slugify.py']
summary     : Fix word_boundary truncation in the modern slugify algorithm to
              allow word length up to max_length instead of strictly less than
              max_length.
constraints : - Only slugify/slugify.py should be edited.
              - Do not edit slugify/_legacy.py under any circumstances.
              - Keep the signature and docstring of _modern_truncate unchanged.
```

Right file, right function, and the rendered diff is exactly the revert of the
injected bug.

**The second constraint is worth noting precisely, because it is the stronger of
the two forms this has taken.** *"Do not edit `slugify/_legacy.py` under any
circumstances"* is explicit evidence that the legacy dispatch was considered and
ruled out — the trap predicted in writing before 6.2, where the traceback's
`slugify(...)` calls read as the legacy default because `_is_test_file` hides the
`partial(public_slugify, algorithm="modern")` rebinding. The first 3.6 run showed
that deliberation; the second did not (its constraints were generic). So the
tally across three independent calls on two models is: **three for "lands on the
right file", two for "demonstrably reasoned about the wrong one" — and the two
are one call each on a different model.** Do not compress that into a single
number.

#### What this run does not establish

Stated plainly, because a green run is the easiest place to over-claim:

- **n=1 repo.** One target, python-slugify, which is also the only real repo this
  harness has ever been pointed at. Nothing here says the design generalises.
- **Attempt 1, no retry.** The fifth consecutive fixture-or-repo fixed first try.
  The retry path — a rejection or a Tester failure reaching a real Implementer
  through the loop — is **still never exercised end to end**, as it has been
  since 3A.
- **F2 was incurred, not resolved.** The Planner prompt was ~9,981 tokens and was
  handled correctly. Still a measured cost, still not a measured failure.
- **F3 was not exercised.** `text-unidecode` was already in the harness venv from
  6.2, so the missing-dependency gap was *pre-satisfied* rather than tested. The
  architectural gap is untouched.
- **F4 reproduced and still stayed invisible in the status.** Discovery again
  returned `failed_tests=[]` with `exit_code=1` over 6,129 characters of
  colour-escaped output — the parser defeat, on 3.5 as on 3.6. The final Tester
  returned `exit=0`, so `summary_parsed` was set `True` by the exit-code-0 branch
  without `_parse_summary` running. **F4's actual consequence has still never been
  observed in a run**, only in direct measurement of the parser.

### 6.5 — F4 fixed. Colour is stripped from a copy, and the raw output is kept.

**One new function and three call sites, all inside `harness/agents/tester.py`.**
`strip_ansi(text)` removes ANSI CSI sequences; `_invoke_pytest` builds
`plain_stdout = strip_ansi(stdout)` and hands *that* to `_failure_detail` and
`_parse_summary`. `TestResult.stdout` is still the bytes pytest wrote.

Nothing else moved: not the exit-code gating, not `summary_parsed`'s rule, not
the loop, not the agents, not the event log's payloads. Suite is **479 tests, all
passing, 197s** — up from 468 by exactly the eleven added below.

**The regex matches the whole CSI class** (`\x1b\[[0-?]*[ -/]*[@-~]`), not just
SGR colour. pytest emits only SGR, but a stray cursor-move or erase-line would be
exactly as invisible to a parser anchored at column zero, and narrowing the class
would buy nothing.

**The rejected alternative was passing `--color=no` to the subprocess.** Three
reasons against it, and the third is the one that decides it: the target's own
`addopts` can still be appended after our flag; it changes the *invocation*
rather than our reading of it, so the Tester would no longer be reporting what
the suite actually does when run normally; and reading the output correctly is
the harness's problem to solve on the harness's side.

#### What this does and does not change about prompt-bound text — flagged, not decided

Three fields carry pytest output toward a model. **The fix as scoped changes one
of them, and the inconsistency is worth naming rather than smoothing over:**

| field | reaches a model via | coloured after 6.5? |
| --- | --- | --- |
| `TestResult.traceback` | `TesterFailure.traceback` | **no** — `_failure_detail` now reads the stripped copy |
| `TesterFailure.stdout_tail` | truncated from `TestResult.stdout` | **yes**, still raw |
| `failure_input` | `cli.discover_failure` → the Planner | **yes**, still raw |

`traceback` was de-coloured as a direct consequence of stripping before the
traceback extraction, which is what this task asked for. The other two were left
alone, which means a retry prompt can now carry a clean `Failure detail:` block
above a colour-escaped `stdout_tail`, and the Planner still receives a
`failure_input` with 528 escapes in it.

**This is an open question, deliberately not answered here.** Arguments both
ways, recorded so it can be decided on evidence:

- **For stripping them too:** escapes are tokens a model pays for and cannot act
  on, and the mixed treatment within one prompt is odd on its face.
- **For leaving them:** `failure_input`'s standing rule is that it is **verbatim,
  never sanitized** — banner, plugin versions, stale `rootdir:` and all — because
  "real failure reports are noisy, and the noise is part of the input". Colour is
  noise of exactly that kind, and a Planner that only works on de-coloured output
  is overfit in the same way one that needs a trimmed header is. Both runs on
  python-slugify received coloured `failure_input` and the Planner was correct
  anyway, twice, on two models.

**The revisit trigger:** a real run where a model demonstrably misreads coloured
prompt text. Nothing has shown that yet, and the "verbatim" rule should not be
weakened on tidiness.

#### The tests, and the three that reproduce F4

`test_tester.py` gained `TestColouredOutputIsParsed` (6) and `TestStripAnsi` (5).

**The input is real captured output, committed as
`tests/data/coloured_pytest_output.txt`** — 6,129 characters, **528 CSI
sequences**, exit code 1, three real failures, exactly what `python -m pytest`
printed inside a clone of `un33k/python-slugify`. It is committed rather than
produced at test time because no fixture in this repo sets `addopts` and the
clone lives outside the repo in `experiments/`, so a test that shelled out to it
would pass or fail on whether someone had staged a checkout. **It is a replayed
real subprocess, not a stubbed one** — the objection this file raises to stubbed
subprocesses is about inventing output, and nothing here is invented. A guard
asserts the capture still contains escapes, so losing them would fail loudly
rather than making every other assertion pass for the wrong reason.

`subprocess.run` is replaced so those bytes travel the real `_invoke_pytest`
path. Parsing the text is one thing; the de-colouring being wired in ahead of
both parsers *while `stdout` stays raw* is the half that needed a test.

**Three of the eleven fail on the old code**, confirmed by reverting the wiring
and re-running rather than assumed: `summary_parsed` was `False`, `failed_tests`
was `[]`, and `traceback` carried escapes. The other eight pass either way and
each is a guard rather than a reproduction — raw `stdout` keeping its escapes,
the exit-code gating being untouched, and the five helper tests. **One of those
eight deserves naming: `test_no_nodeid_carries_an_escape_sequence` passes
vacuously on the old code**, because an empty `failed_tests` has no nodeid to
check. It guards against a future strip applied *after* the regex rather than
before it; it does not reproduce F4.

#### The observable change, and where to see it

**Discovery against python-slugify now reports 3 failing tests instead of 0.**
Measured through the real `cli.discover_failure` after the fix:

```
exit_code     : 1
summary_parsed: True          <- was False
failed_tests  : 3             <- was []
   tests/test_release.py::ReleaseRegressionTests::test_actual_separator_width_and_literal_characters
   tests/test_release.py::ReleaseRegressionTests::test_empty_separator_preserves_word_boundaries
   tests/test_release.py::SplitCoverageTests::test_modern_word_boundary_empty_token
banner        : auto-discovered (3 failing test(s))    <- was "(0 failing test(s))"
stdout        : still coloured, 6,129 chars
```

Both python-slugify transcripts recorded above show
`auto-discovered (0 failing test(s))` in their banners. **That line is now wrong
for any future run and right for those two** — they were produced before this
fix, and the under-report was F4 showing through. Do not read the old banners as
a defect in those runs.

**What is still not measured.** F4's actual consequence — a failing attempt
misrouted to `escalated_broken_suite` instead of retrying — **was never observed
in a run and still has not been.** It was proven by direct measurement of the
parser, and the fix is verified the same way plus a replayed real capture. The
first run that *fails* an attempt on a colour-forcing repo is what would have
demonstrated the bug and now demonstrates the fix; that run is the next step
below, and it is now unblocked.

### The next two steps, fixed in advance and not to be re-chosen

6.3's table says to pick exactly one fix based on what 6.2 evidenced, and to
build one at a time. What 6.2 evidenced is F1, by crashing on it. So:

**Done — 6.2's python-slugify measurement is complete.** The re-run against the
`.git`-stripped clone went green end to end; see "The confirming run" above.
Stripping `.git` was environment setup, the same category as installing the
target's dependency, with no harness code changed.

**Done — 6.3 is built.** `.git` is excluded from `_EXCLUDED_DIR_NAMES`, which
is 6.3's "something about real repo structure broke that had nothing to do with
size" row. It was deliberately *not* the retrieval/file-selection step and not
localized edits: F2 measured a 9.2x prompt the Planner handled correctly, which
is a cost rather than a failure, and nothing has evidenced an Implementer-side
problem at all. **Do not build for F2 or F4 on the current evidence.** The
`rmtree(ignore_errors=True)` question was decided with the code open and answered
**no**, with a named revisit trigger — see "The `rmtree` decision" above.

**Done — 6.4 confirmed F1 against a real object store.** `succeeded`, attempt 1,
three calls, `baseline_reset` survived where 6.2 died. See "6.4" above.

**Next: 6.2 again, against 2-3 further small real repos — and this time with bugs
chosen to make a retry happen.** The roadmap always said to do 6.2 two or three
times before concluding anything, and one green run is not evidence the design
generalises. Two things shape the choice of bug:

1. **The bug must need more than one edit site, or admit a plausible-but-incomplete
   first fix.** Every bug used so far has been a one-character boundary flip that a
   correct plan fully determines, which is precisely why five runs in a row have
   gone green on attempt 1. A fix the plan under-determines is the only thing that
   produces a natural retry.
2. **The recoverability constraint still binds.** The difficulty has to sit between
   a *correct* plan and a first implementation of it. A bug that misleads the
   Planner produces an unrecoverable run, not a retry — v1 never replans. See
   "Fixture design: the recoverability constraint".

**That coupling is now resolved: F4 was fixed first, in 6.5.** The reasoning
stood that on any colour-forcing repo a failing attempt is misread as
`summary_parsed=False` and **escalates instead of retrying**, so the retry this
step exists to produce would have been destroyed by the mechanism it needs —
and python-slugify forces colour. Candidates no longer need screening for
`addopts`, and a repo that forces colour is now a *useful* target rather than a
disqualifying one: it exercises the 6.5 fix on the path that matters.

**F2 and F3 stay unbuilt.** F2 is a measured cost the Planner has handled at 9.2x
twice; F3 is real but has never blocked a run that was set up correctly. Neither
has evidenced a failure, and the phase rule has not changed.

### 6.2 round 2 — three repos staged, predictions written before any run

**Goal:** give a retry the chance to occur naturally. Six runs in a row passed on attempt 1. Staging only: no harness code changed, `cli.py` not run, clones not committed (they live outside this repo, in `experiments/`).

**Construction rule (mechanical, not hand-edited).** Full clone, `git checkout --detach <fix>`, baseline run, then `git checkout <fix>^ -- <source file>` and `git reset` so the bug is an *uncommitted working-tree change*. The fix commit's new test stays. `git diff HEAD` in each clone is exactly the revert of the upstream fix. The Planner sees the repo untouched: nothing was hidden.

**Two exposure questions, answered from code and a grep of the real prompt, not asserted:**
- *Does an uncommitted bug expose the fix?* No. `grep -n git harness/*.py harness/agents/*.py cli.py` finds no git invocation; `.git` is excluded from the copy and from `list_repo_files`. The Planner sees `render_repo` of the copied working tree (non-test `.py` inlined, everything else by path), `failure_input` and the description. Checked on the real `render_user_message`: strings unique to each upstream fix are absent from the schema and xmltodict prompts. The one hit, `passer` in the inflection prompt, is the test id `passerby-passersby` in the failure text; `inflection.py` itself contains no `passer`.
- *Can a clone named `schema` shadow a harness import?* No. The harness process runs from `mach/`, so `experiments/` is never on its `sys.path`; nothing under `mach/` or in `.venv` imports `schema`, `xmltodict` or `inflection` (grepped; none is installed). The Tester's `python -m pytest` subprocess does put the target's cwd first on `sys.path`, which is what we want, and no pytest plugin imports those names.

| | schema | xmltodict | inflection |
| --- | --- | --- | --- |
| path | `experiments/schema` | `experiments/xmltodict` | `experiments/inflection` |
| upstream | keleshev/schema | martinblech/xmltodict | jpvanhal/inflection |
| fix commit (HEAD of clone) | `15158c5391eaacbc1e4813b3529ea035fd2cf03e` | `aa165113bef2b3a1a822209863343b9dc9ffe43a` | `1969b3a06a9ff06d023863e388cf8af01978d297` |
| buggy file (uncommitted) | `schema/__init__.py` | `xmltodict.py` | `inflection.py` |
| baseline at fix commit | 124 passed | 104 passed | 465 passed |
| red after bug | 1 failed | 3 failed | 3 failed |
| failing nodeids | `test_schema.py::test_wrong_key_reports_key_schema_error` | `tests/test_dicttoxml.py::test_non_string_text_with_attributes`, `::test_none_text_with_short_empty_elements_and_attributes`, `::test_none_attribute_serializes_as_empty_string` | `test_inflection.py::test_pluralize_singular[passerby-passersby]`, `::test_singularize_plural[passerby-passersby]`, `::test_pluralize_plural[passerby-passersby]` |
| files / full Planner message | 20 / ~11.5k tok | 21 / ~6.5k tok | 15 / ~5.9k tok |
| pytest `addopts` | none | none | `--doctest-modules`; no colour |
| forces colour | no | no | no |

None forces colour, so these runs do not exercise 6.5 in a real run; that stays open.

**Pre-checks (free, real functions, scratch `runs/` and `logs/`):** `list_repo_files` saw 20/21/15 files, 0 under `.git/`. `discover_failure` reported exit 1, `summary_parsed=True`, and 1/3/3 failing, matching the nodeids above exactly. Discovery left each clone's `git status` byte-identical (caches cleared). `prepare_run_dir` then `reset_run_dir` survived with no `.git` in the run dir, and each source clone kept its `.git` with 3 read-only pack files.

**Quota cost per run.** Calls are Planner + (Implementer + Reviewer) per attempt; the Tester is not an API call. First-try green = **3 calls**; one retry = **5**; five attempts to the cap = **11** (`MAX_ATTEMPTS = 5`). A parse-ladder repair turn adds calls beyond these. Livelock or an early escalation lands between the numbers, and a human rejection at the gate costs an attempt without a Tester call.

**Predictions, written before any run.** Probabilities are mine and are for checking, not for defending afterwards. "Retry" means a failing Tester verdict or a Reviewer rejection followed by a second attempt, whatever the end status.

*schema* (prediction: Planner targets `schema/__init__.py`, ~97%; there is only one source file).
The test function has three sequential asserts (Regex key with `error=`, default message with no `error=`, `And` key with `error=`), and the failure text shows only the first. A Regex-specific or `e`-clobbering first fix can pass assert 1 and fail 2 or 3, which is the natural retry.
- green attempt 1, 3 calls: 40%
- retry then green, 5-7 calls: 35%
- escalation (cap, livelock) or Reviewer-stuck: 15%
- other (plan misses the `except SchemaError: pass`, or a patch in `SchemaWrongKeyError` / `_prepend_schema_name`): 10%
Branches: *wrong place* (the exception class or the message helper instead of the dict-validation branch); *incomplete* (records the key error but does not surface it, or surfaces it unconditionally and breaks the default-message assert); *retry* (assert 1 passes, 2 or 3 fails); *escalation* (same wrong patch on every attempt, livelock).

*xmltodict* (prediction: Planner targets `xmltodict.py`, ~99%).
Three tests name three symptoms, all visible in the failure text, so a good plan may enumerate every site. A fix that edits only `_convert_value_to_string` or only the `#text` site leaves attribute tests red (measured: a cdata-only patch leaves 2 of 3 red).
- green attempt 1: 55%
- retry then green: 25%
- escalation: 10%
- other: 10%
Branches: *incomplete* (cdata only, or the helper only); *retry*; *green but incomplete against upstream*: the `xmlns` attribute site has no test, so a suite-green patch can still differ from upstream. That is a green, and it is not a failure and not a Reviewer rejection to be narrated later. Check it with `git diff HEAD` against the final diff and record it as "green, xmlns site missed" if so. *Wrong file*: not possible; one source file.

*inflection* (prediction: Planner targets `inflection.py`, ~99%; weakest of the three, the plan nearly determines the fix).
Rule position decides the outcome: `passerby` ends in `rby`, so the generic `([^aeiouy]|qu)y$` rule (`passerbies`) wins unless the new rule is earlier. Measured on scratch copies of the bug state (each variant run through the real suite): both rules placed before the generic ones = 465 passed; `PLURALS` rule only = 1 red (`test_singularize_plural`); `s?`-less plural pattern with both lists = 1 red (`test_pluralize_plural`); both rules appended at the end of their lists = 2 red (`test_pluralize_singular`, `test_pluralize_plural`). (An earlier scratch experiment that reported "all 3 red" for append-at-end was a bad patch and is superseded by this measurement.)
- green attempt 1: 55%
- retry then green: 25%
- escalation: 10%
- other (e.g. Reviewer rejects a correct fix): 10%
Branches: *incomplete* (one list only, or an `s?`-less pattern: each leaves exactly one test red, so the retry evidence is narrow and specific); *wrong position* (appended after the generic rule: 2 red, and the failure evidence shows `passerbies`, which is what a retry can act on); *escalation* (a plan that says "append" and an Implementer that follows it five times).

**Readings fixed in advance.** A retry that goes green is the first real exercise of `render_evidence` and `RESET_NOTICE` and is the headline. Three first-try greens would say these bugs are also fully determined by a correct plan and that the next step is a different bug shape, not more of these. Do not call a first-try green "as expected" for xmltodict or schema without checking the diff against the upstream fix first.

**Run commands** (one at a time, from `mach/`; ~3 calls each if green first try):

```
.venv\Scripts\python.exe cli.py --repo ..\experiments\schema
.venv\Scripts\python.exe cli.py --repo ..\experiments\xmltodict
.venv\Scripts\python.exe cli.py --repo ..\experiments\inflection
```

### F5 — thinking tokens exhausted `max_tokens` on a larger real repo (schema)

**What happened.** On `experiments/schema` (a 46k-character Planner prompt, a ~970-line target file), the Planner and then the Implementer raised `LLMTruncatedError`: thinking used the whole output budget and no text came back. The Planner had `MAX_TOKENS = 8000`. The Implementer had 16000 and no thinking control. In the one Planner run that got through at the default level, thinking used 7,680 of the 8,000 tokens (`schema_20261008T104322Z`). The logs of the failed calls show only an `llm_request` and no response, because truncated calls were not logged at the time (see the fix below).

**The wrong first fix.** The Planner's `MAX_TOKENS` was raised to 16000 and `thinking_budget = 8192` was added. A later run (`schema_20261008T110638Z`) logged **15,358 thinking tokens** against that cap, so the budget was not honored. That call escaped truncation by only about 400 tokens (16,000 − 15,358 − 228 output). A successful response never proved the cap worked, and it was the log's token counts, not the success, that showed it did not. The logs do not record the thinking config sent, so that this run carried the budget rests on the order of the changes, not on a logged field.

**What the SDK and docs showed.** The installed SDK (google-genai 2.19.0) offers both `thinking_budget` and `thinking_level` on `ThinkingConfig`. The documented control for gemini-3.5-flash is `thinking_level` (`minimal`, `low`, `medium`, `high`), with `medium` as the default. `thinking_budget` is kept only for backward compatibility, and sending both in one request returns a 400. Whether the budget is a hard cap or a hint is not documented; treat it as a hint. `max_output_tokens` is the only documented hard ceiling and includes thinking.

**The fix.** `thinking_budget` was removed. `complete_structured` and `_create` take an optional `thinking_level` (default `None`, sent only when set). The Planner and the Implementer send `thinking_level="LOW"`, and `MAX_TOKENS` stays at 16000 as the only hard ceiling. The Reviewer is unchanged and still runs at the default level. Truncated calls now log an `llm_response` event with stop reason and token usage: `llm.py` attaches the payload to `LLMTruncatedError.call_payload` and `Agent.run` in `base.py` writes the event before re-raising, so the client reports and the agent logs, as before.

**Result.** In `schema_20261008T114630Z` the Planner's thinking dropped to 5,532 tokens, and no call truncated (every stop reason `STOP`). Implementer thinking plus output stayed between about 12.1k and 13.5k, under 16,000 (per-attempt figures are in F6).

**Evidence boundaries.** The log proves `LOW` lowered thinking on this prompt and that nothing truncated afterwards. It does not show how `LOW` affects plan or code quality.

**Open items.**
- The Reviewer's thinking is not capped.
- A later run showed `LOW` still allowing 8,253 Implementer thinking tokens and a truncation at 16,000; see F7.
- The 487 tests check that the right settings are sent, not that `LOW` reduces thinking. Only live runs show that.

**Interview line.** A real repo broke a limit the fixtures never reached. The harness stopped safely with a clear error, and the logging gap was found and fixed.

### F6 — schema round-2 run: five of five attempts rejected, on corruptions the plan never asked for

`transcripts/schema_20261008T114630Z.jsonl`. **`escalated_retry_limit`, 5 of 5 attempts, every attempt rejected by the Reviewer, nothing reached disk.** The plan was correct and targeted `schema/__init__.py`. This is the run `Predictions, written before any run` listed under *schema* as "escalation (cap, livelock) or Reviewer-stuck", with the cause not predicted: that section expected the failure to come from the fix being incomplete, not from collateral damage around a fix that was roughly right.

**The plan.** Four steps: a `key_errors` dict before the `data_items` loop, record the caught `SchemaError` per key instead of `pass`, gather `autos`/`errors` for `wrong_keys` when raising `SchemaWrongKeyError`, and filter out `None`. Constraints: only `schema/__init__.py`; `SchemaWrongKeyError` still raised; default message preserved when no custom error exists. No wrong-file or wrong-function branch occurred.

#### The five corruptions, from the logged diffs and Reviewer reasons

Attempt 4's ledger line was cut off, so its detail is read from the log's `diff_rendered` and Reviewer verdict, not from the ledger.

1. **Attempt 1:** the `defaults = (...)` expression rewritten as an invented `... hasattr(k, "defaulttrim")) if False else ...` ternary. Nothing in the plan or the file suggests it.
2. **Attempt 2:** `super().__init__(` became `super().__init密(` (a stray CJK character, a syntax error that would crash the package), plus `else:` → `else: ` (trailing space) and an unrequested re-wrapping of a `raise ValueError(...)`.
3. **Attempt 3:** `Regex.validate` edited to wrap `error_message` in lists in two places (an unrequested semantic change in a different class), plus trailing whitespace on `self, ` / `-> None: ` / `else: ` in several signatures.
4. **Attempt 4:** the opening `"""` of `Use.__call__`'s docstring became `""` (syntax error), and the JSON-schema regex `r"\(\?P<[a-z\d_]+>"` became `r"<TAB>P<[a-z\d_]+>"` (`\(\?` replaced by a tab character).
5. **Attempt 5:** the same regex changed to `r"\(\?P<[a-z\df_]+>)"` (an `f` inserted and a stray `)`), plus trailing whitespace on `self, ` in five signatures.

In every attempt the intended `key_errors` change was present and looked plausible. The attempts differ in design (list-of-errors vs one error per key, whether `message` is included in `autos`), so the Implementer was not stuck on one idea. **None of the five fixes was ever applied or run, so whether any of them was correct is unknown.**

#### Numbers (taken from the log)

| call | thinking | output | thinking + output |
| --- | --- | --- | --- |
| Planner | 5,532 | 300 | 5,832 |
| Implementer 1 | 2,590 | 9,843 | 12,433 |
| Implementer 2 | 2,946 | 9,779 | 12,725 |
| Implementer 3 | 3,707 | 9,788 | 13,495 |
| Implementer 4 | 2,269 | 9,822 | 12,091 |
| Implementer 5 | 3,026 | 9,819 | 12,845 |
| Reviewer 1-5 (thinking) | 1,155 / 915 / 2,322 / 1,553 / 1,997 | not tabulated here (99 and 124 on attempts 1-2) | n/a |

Every stop reason was `STOP`. Thinking plus output never exceeded 13,495 against the 16,000 `MAX_TOKENS`, so **truncation was not the cause.** The thinking-limit history that led to this configuration is F5.

#### What the log proves, and what it cannot

- **Proved:** the plan came out correct under `LOW`; the Implementer's full-file output fit (about 9.8k tokens) in every attempt; the Reviewer rejected on real defects each time.
- **Hypothesis, unconfirmed:** emitting a complete replacement of a ~970-line file produces random copy errors somewhere in the 970 lines that the model was not editing. The pattern fits: corruption sits in places unrelated to the fix and differs each attempt.
- **Not separable from this log:** `LOW` thinking may have contributed (less deliberation per attempt). The Implementer was set to `LOW` in the same change that produced this run (F5), and there is no medium-level Implementer run on this file to compare against: the earlier schema Implementer calls never returned a response.

#### What worked

- **The Reviewer caught 5 of 5.** Each rejection named a concrete defect and each was correct against the diff.
- **Nothing reached disk.** No approval gate or apply event appears; the clone's only change is the original injected bug.
- **The livelock detector correctly stayed quiet.** The five rendered diffs are pairwise different, and byte-equality is the rule it implements.

#### Candidate next step — not built, not decided

**A diff-based or hunk-based Implementer**: have the model emit only the changed region (search/replace blocks or hunks) and let the harness apply them, so unrelated lines cannot be re-typed. Costs to weigh before choosing it: it reverses the ruling in "Edits, not patches" (which chose full replacement because models emit invalid unified diffs, so any replacement would need an exact-match anchor scheme and its own failure mode), it touches `FileEdit`, the Implementer contract and the scope/diff rendering, and it is only worth doing if the corruption is confirmed to come from full-file output. The cheaper discriminating evidence is a second run on this repo with the Implementer back at the default thinking level, or a run on a smaller file: either would separate "full-file output" from "LOW".

### F7 — schema: a retry on Tester evidence overran the Implementer's 16,000 tokens

`logs/schema_20261008T122304Z.jsonl` (not copied to `transcripts/`).

**What happened.**
- **Attempt 1** was approved by the Reviewer and **applied to disk**. The Tester then failed it: `test_schema.py::test_dict` and `test_schema.py::test_wrong_key_reports_key_schema_error` (`summary_parsed=True`, exit 1). The target test stayed red, and `test_dict`, which passed at baseline, went red as well. **Attempt 1's fix is therefore a red result, not an unknown.** The Reviewer approved it by design without seeing test results; the Tester is what caught it. Its diff also carried one stray trailing space (`else: `), the same kind of artefact F6 recorded.
- **Attempt 2**, the retry, carried `evidence_kind: tester_failure`. Its prompt was **57,402 characters** against 37,732 for attempt 1, because it carried the failure evidence. The Implementer call ended with `stop_reason: MAX_TOKENS`: **8,253 thinking + 7,731 output = 15,984 tokens against the 16,000 limit.** The output was cut off at 7,731, short of the ~9.8k a full rewrite of the file takes, so no edits came back and the run ended on `LLMTruncatedError`. The truncation was logged (the F5 change); before it, this call would have left no trace.
- The model never produced a response to the Tester evidence, so **whether that evidence would have helped is still unmeasured.**

**Numbers.** The first Implementer call in this run used 1,665 thinking and 9,788 output tokens. Under `thinking_level=LOW` the Implementer's thinking has ranged from 1,665 to 8,253 across schema calls, so `LOW` does not bound it (see F5: it is an effort setting, not a token cap). Output for a full-file rewrite has been 9,779 to 9,843 tokens across the logged schema attempts.

**Change.** Implementer `MAX_TOKENS` raised from 16000 to **24000**. `thinking_level` is unchanged (`LOW`). The Planner (16000) and Reviewer are unchanged. The ceiling is set by the HTTP timeout, not the model's output limit: the client allows 120 s per attempt (`DEFAULT_TIMEOUT`), logged throughput is about 275-325 tokens/s, so 24,000 tokens is about 90 s in the worst case, while 32,000 would approach the timeout and an SDK retry would re-spend the whole call. The model's documented maximum output is 65,536 (65,535 on one Google page); that figure is not the binding constraint. A 24,000 budget leaves about 14k for thinking after a roughly 10k output. `tests/test_prompts.py` asserts the new value.

**Proved.**
- Attempt 1 was applied and failed two tests, one of them a regression.
- Attempt 2 overran 16,000 (15,984 used, `MAX_TOKENS`) with 8,253 of it thinking.
- The Implementer's thinking under `LOW` varied by about 5x across calls.
- The truncated call was logged with stop reason and usage.

**Unverified.**
- **Whether 24,000 is enough.** It has not been run. A retry that thinks as long as attempt 2 did and outputs about 10k fits (about 18.3k), but nothing shows thinking cannot exceed 14k.
- Whether 24,000 stays inside the 120 s timeout in practice: the ~90 s figure is arithmetic on two logged calls, not a measured 24k-token call.
- Whether `MINIMAL` would cut Implementer thinking, and what it would do to retry quality. Not changed here on purpose: one data point, and changing the level and the budget together would confound the next run (the same caution as F6).
- Whether the retry, once it can respond, uses the Tester evidence well.
- A later run exercised 24,000 and the retry recovered (20,139 tokens used), and logged a different model name than requested; see F8.

### F8 — schema: the first retry on a real repo that recovered, and a model-name discrepancy

`transcripts/schema_20261010T042800Z.jsonl`. **`succeeded`, 2 of 5 attempts, `reason: the suite is green.`**

**What happened.**
- **Attempt 1** was approved by the Reviewer and **applied**. The Tester failed it: `test_schema.py::test_dict` and `test_schema.py::test_wrong_key_reports_key_schema_error` (the same two nodeids as F7's attempt 1). Its diff added a `key_errors` dict, recorded every `SchemaError` per key, and always passed `autos`/`errors` from all rejecting key schemas into `SchemaWrongKeyError`, with no check for whether a key schema carried a custom error.
- **Attempt 2** carried `evidence_kind: tester_failure` (57,220 characters against 37,836 for attempt 1), produced a response, was approved, applied, and the Tester returned green. Its diff added a `_has_custom_error` helper (checks `_error`, recursing through `_schema`), kept key errors as `(key, skey, err)` tuples, and raised with the key errors' `autos`/`errors` only when the rejecting key schema carries a custom error (or an error text is non-`None`). Otherwise the original default message is raised.
- This is the **retry path recovering on a real model and a real repo from a Tester failure**: the 57k-character evidence prompt that truncated in F7 was answered, and the answer fixed the suite.

**Numbers (from the log).**

| call | thinking | output | notes |
| --- | --- | --- | --- |
| Planner | not logged | 278 | |
| Implementer, attempt 1 | not logged | 9,737 | |
| Reviewer, attempt 1 | 976 | 156 | |
| Implementer, attempt 2 | 10,207 | 9,932 | 20,139 total, `STOP` |
| Reviewer, attempt 2 | 1,133 | 150 | |

Attempt 2's 20,139 tokens is **under the 24,000 ceiling set in F7 and over the old 16,000**, so at the old limit this call would have truncated, as the F7 call did. "Not logged" means the response carried no `thoughts_token_count`; it is absent, not recorded as zero.

**Model discrepancy: the log reports `gemini-3.6-flash`, not the requested model. Explained by Google's routing, see below.**
- At the time of the run `DEFAULT_MODEL` was `gemini-3.5-flash`, and neither `cli.py` nor the agents pass a model, so the request named 3.5 (read from the code; the request's model name is not itself logged).
- The logged name is the response's `model_version`, falling back to `self.model` (`harness/llm.py:536`). Every `llm_response` in this run says `gemini-3.6-flash`. The 2026-10-08 logs (`schema_20261008T104322Z`, `...110638Z`) said `gemini-3.5-flash`.
- **Cause: Google's deprecation notice.** Google's deprecations page says requests to `gemini-3.5-flash` are automatically routed to `gemini-3.6-flash`; the notice is dated 2026-10-10 as reported to me. The page I fetched (2026-10-11) shows that routing statement, 3.5 Flash's release date (2026-05-19) and "no shutdown date announced", but no date next to the routing statement. The log order is consistent with it (2026-10-08 runs labelled 3.5, 2026-10-10 runs labelled 3.6), but this repo cannot observe the routing itself, so that fit is an inference from timing, not a measurement.
- **Change:** `DEFAULT_MODEL` is now `gemini-3.6-flash`, so the request names the model that is served. F5-F7 stay labelled 3.5, because that is what their logs report.
- **Consequence, stated plainly: the F5-F7 numbers and this run are not like-for-like.** Thinking and output figures from 2026-10-08 are from the model the log named 3.5; this run's are from what the log names 3.6. Treat the `LOW` thinking range in F7 (1,665 to 8,253) and this run's 10,207 as possibly different populations. The 3.6 label also appears on the earlier python-slugify runs in this file, which is not evidence about this run's cause.

**Compared with upstream fix `15158c539`** (read-only: `git show` in `experiments/schema`, whose `HEAD` is that commit).
- **Upstream** stores `x.code` per key, only when the rejecting key schema has `_error is not None`, and raises `SchemaWrongKeyError(message, [e.format(data) or None] + custom_errors)`, so the default message stays in `autos` and the custom text is appended to `errors`.
- **The green diff** differs in four ways: it keeps a list of `(key, skey, err)` tuples instead of a dict, recursing through `_schema` for custom errors instead of checking only the top-level `_error`; it passes the key errors' `autos` and `errors` through unchanged; when a custom error is found it replaces the default `message` in `autos` instead of keeping it; and its condition also fires whenever any key error text is non-`None`.
- **A scratch probe** (copies of both versions; my own inputs, not upstream tests) shows the displayed `code` is the same on the suite's three scenarios and on the default case, and differs in two I tried:
  - with an outer `Schema(..., error="outer: {}")` and no custom error on the key, the green version adds a second line (`outer: zz`) that upstream does not;
  - with a nested custom error (`And(And(..., error="deep"), len)`) the green version surfaces `deep`, where upstream gives the default wrong-key message.

  The first is a behaviour difference from upstream that the suite does not cover; whether it is a defect depends on intent, and I have not judged that.

**Proved.**
- The retry recovered from a Tester failure and the suite went green on attempt 2.
- 24,000 was enough on this run (20,139 used, `STOP`), and 16,000 would not have been.
- The two attempts' diffs are structurally different, and the second is not upstream's fix.
- A difference from upstream exists in the outer-`error=` case, on my probe input.

**Unverified.**
- That 24,000 is enough in general: this is one call, and thinking has ranged from about 1.7k to 10.2k.
- Correctness beyond the suite: green is the suite passing, and the probe above shows behaviour the suite does not pin.
- Whether 3.5 and 3.6 behave alike on this task. Google's notice explains the label, not the behaviour; the thinking and output figures in F5-F7 and F8 may still come from different populations.
- Whether the green result comes from the retry evidence rather than from run-to-run variation: attempt 1 here repeated F7's failure shape, but with one recovered run and one truncated run there is no basis for a rate. (A second recovered run is recorded below.)

#### Second data point: `schema_20261010T092053Z`

`transcripts/schema_20261010T092053Z.jsonl`. **`succeeded`, 2 of 5 attempts, `reason: the suite is green.`** This is a second run of the same repo and configuration as the first F8 run, not a new finding.

**What happened.** The same shape as the first run. Attempt 1 was approved and applied, then failed the Tester with the **same two nodeids** (`test_schema.py::test_dict`, `test_schema.py::test_wrong_key_reports_key_schema_error`). That is the third schema run in a row (F7, F8 run 1, this one) whose attempt 1 failed those two. Attempt 1 appended every caught `SchemaError` to one list and raised with all of their `autos`/`errors` whenever the list was non-empty. Attempt 2 (`evidence_kind: tester_failure`) kept the list but appended only when the error carried a custom message (`x.errors` non-`None`, or a `_error` on the key schema or on its `_schema`), then raised with the collected `autos`/`errors` and, unlike the first run's green diff, **without** `message` or `e.format(data)`. It was approved, applied, and the Tester returned green.

**Numbers (from the log).** The model reported was `gemini-3.6-flash` on every call, so this run is like-for-like with the first F8 run and not with F5-F7.

| call | thinking | output | notes |
| --- | --- | --- | --- |
| Planner | not logged | 305 | |
| Implementer, attempt 1 | 2,879 | 9,739 | 12,618 total |
| Reviewer, attempt 1 | 966 | 213 | |
| Implementer, attempt 2 | 6,324 | 9,789 | 16,113 total, `STOP`; prompt 57,258 characters (attempt 1: 38,032) |
| Reviewer, attempt 2 | 781 | 207 | |

Attempt 2's 16,113 tokens is **113 over the old 16,000** and under 24,000, so at the old ceiling this call would have been cut off by a margin that small. Across the three `gemini-3.6-flash` Implementer calls with a logged count, thinking was 2,879, 6,324 and 10,207.

**Compared with upstream `15158c539`** (read-only; same scratch probe as the first run, eight inputs of my own, on copies, nothing in `experiments/schema` changed; its `HEAD` is the upstream commit and the clone's only modification is still the injected bug). Observed behaviour only:

| input | upstream | this run's green | first run's green |
| --- | --- | --- | --- |
| Regex key with custom error | `code` `Invalid profile name` | same | same |
| `And` key with custom error | `must be upper` | same | same |
| Regex key, no custom error | default message | same | same |
| plain `str` key, no custom error | default message | same | same |
| two wrong keys, custom error | `must be upper` | same `code` | same `code` |
| outer `error="outer: {}"`, no key custom | `outer: {'a': 1, 'zz': 2}` | `outer: zz` | `outer: {'a': 1, 'zz': 2}` + `outer: zz` |
| outer `error=`, key custom | `outer: {'FOO': 1, 'bar': 2}` / `outer: bar` / `must be upper` | `outer: bar` / `must be upper` | same three lines as upstream |
| nested custom (`And(And(..., error="deep"), len)`) | default wrong-key message | `deep` | `deep` |

Beyond `code`, this run's green also differs from upstream in the exception's fields: `autos` has 2, 1, 2, 1, 1, 2, 3 and 4 entries on the eight probe inputs (Regex custom, Regex default, `And` custom, plain `str`, outer `error=`, outer plus custom, nested, two wrong keys) against upstream's 1 on every input. I have not judged any of these as defects; they are differences in what the exception carries, on inputs the suite does not cover.

**Proved.**
- The retry recovered from a Tester failure a second time, on the same repo, and the suite went green on attempt 2.
- 24,000 was enough on this run (16,113 used, `STOP`), and 16,000 would have been exceeded by 113 tokens.
- The two green diffs from the two runs differ from each other (the first inserts `e.format(data)`, this one does not) and neither equals upstream.
- On my probe inputs, this green's outer-`error=` output differs from upstream's, and differs from the first run's green.

**Unverified.**
- That 24,000 is enough in general: this is three Implementer calls on one model label.
- Correctness beyond the suite: green means the suite passed, and the probe shows behaviour the suite does not pin.
- Whether attempt 1's repeated failure shape is stable or coincidence: three runs, one repo, no basis for a rate.
- The model-name discrepancy is explained by Google's routing notice (see the first F8 run); that explanation is Google's statement plus timing, not something the log shows.
- Whether recovery depends on the evidence the retry carried: two recoveries and one truncation (F7) do not separate the evidence from run-to-run variation.

### F9 — subtest-only failures are invisible to the Tester's summary parser

Found during the task-set pre-checks, not in a run. **Status: not fixed. Gate: a run on a repo that uses `unittest` subtests.**

**What happens.** pytest 9 reports a failed `self.subTest(...)` block in the short summary as `SUBFAILED(<params>) <nodeid> - ...`. `_SUMMARY_LINE` in `harness/agents/tester.py` matches only `^(?:FAILED|ERROR)\s+`, so those lines are skipped. A test that fails only inside subtests contributes no nodeid, while pytest still exits 1.

**Proved** (real `Tester._run` on exported trees, raw `pytest -q` to read the lines):
- python-slugify `548b14f` (parent source + the fix's tests): pytest reported **28 failed**; `failed_tests` was **empty**. Every failure was a `SUBFAILED` line.
- python-semanticversion `0811fca`, same construction: **3 failed**, `failed_tests` **empty**, three `SUBFAILED` lines.
- Mixed case, xmltodict `ab4c86f`: 5 failed, **1 named** — the one plain `FAILED` line; the four `SUBFAILED` lines were dropped.

**Unverified**, read from the code rather than observed:
- `summary_parsed` should be `True` here (the summary header is present and the exit code is 1), so the loop would retry rather than escalate, with a `TesterFailure` naming no tests — the Implementer would get "The suite did not pass." plus whatever `_failure_detail` extracts. Whether that detail contains the subtest assertion has not been checked.
- `cli.discover_failure` would print `auto-discovered (0 failing test(s))` while handing the Planner output describing many.
- How many real repos this affects: only seen on repos using `self.subTest`.

The benchmark task set avoids every subtest-dependent candidate, so it does not hit this.

### F10 — tokenize-rt, arm C: the Planner targeted only the test file, and five attempts could not recover

`experiments/bench/logs/armC_tokenize-rt_cb7836b_20261015T094833Z_95e648.jsonl`, scored in `experiments/bench/results/eval.jsonl`. **`escalated_retry_limit`, 5 of 5 attempts, 11 calls, not resolved.** Model reported: `gemini-3.6-flash` on every call. Four earlier arm C attempts on this task ended after one request each, as infrastructure incidents (one 429, three 503s); they are not scored.

**Proved, from the log and the score row:**
- The plan's `target_files` is **only `tests/tokenize_rt_test.py`**. Its summary is to *update* `test_src_to_tokens_fstring_with_escapes` to match the 3.12+ tokenization, and its first constraint is "No file other than tests/tokenize_rt_test.py is modified."
- The Reviewer **approved attempts 1, 2 and 4** and **rejected 3 and 5**. Attempt 3 for wrong token names in the edited test (`UNIMPORTANT_WS` instead of `FSTRING_MIDDLE`), attempt 5 for changes to two other tests the plan did not name.
- The Tester **failed after attempts 1, 2 and 4**, each time on the target `test_src_to_tokens_fstring_with_escapes` alone. Every applied edit was to `tests/tokenize_rt_test.py`.
- The run ended `escalated_retry_limit` with **11 calls** (Planner 1, Implementer 5, Reviewer 5).
- The upstream fix (`cb7836b`) is **5 lines in `tokenize_rt.py`**; the run changed **0 source lines**. With the hidden test restored, the target still fails (`targets_pass: false`, no regressions). The run directory ended as the task baseline, because attempt 5 was rejected before apply.

**Unverified:**
- **Why the Planner chose the test file.** The failure output shows a mismatch between expected and actual tokens. Reading the expected list as the stale side is one possible cause; nothing in the log says which reading it took, or why.
- **Whether a re-plan would have helped.** v1 never replans, so every retry re-read the same test-only plan. Under "Fixture design: the recoverability constraint", a wrong plan is unrecoverable by construction, which matches what happened, but no run has tested a second plan.

**Status: recorded, nothing built.** This is the **first candidate for the parked re-plan gate** (routing back to the Planner, behind the scope fence as a v2 item). It is not yet a reason to build it: one run, one task, one model. It is also the **first observed instance** of "A known hole: a plan that targets a test file", whose named fix is a `Plan` validator rejecting test paths in `target_files`. That trigger has now fired once; the decision is still open.

### Phase 5, complete as recorded

**All three probe passes are complete.** The Reviewer probe scored **8/8**, the Implementer probe **5/5**, and the conditional end-to-end `fixture_repo_3` run went green on attempt 1. Both halves of retry-on-failure are measured directly; see "What the retry mechanism proved, together".

**5.1 — the probe pass and the run it authorised. Both spent; 12 calls.** The pass scored **8/8** (`transcripts/probe_20260902T061724Z.jsonl`). Probe 1 was rejected, which under the rule settled in advance authorised the end-to-end run — and that run went **green on attempt 1 in three calls** (`transcripts/fixture_repo_3_20260902T062218Z.jsonl`).

**The Planner closed the door again, and this was predicted in writing before the call.** Its plan named `_regular_plural` and listed the sibilant suffixes, so the Implementer had nothing to be unfaithful with and wrote the correct fix; the Reviewer approved it correctly. That is the fixture-2 outcome one layer down, anticipated in "What this fixture measures, and the lottery it shares with fixture 2" rather than discovered. **Two fixtures, two Planner rescues** — the finding that the plan/implement split earns its keep now has two data points, not one.

It also settles what the fixture is for: **`fixture_repo_3` is not the instrument for the Reviewer question and should not be re-engineered to become one.** The probe measured that in eight independent shots; the run measured one happy path and confirmed a prediction. Both are worth having, and they are not substitutes.

**Phase 4 — complete as built.** 4A: the escalation output. 4B: `tasks/fixture_repo_2/`, the naive-fix trap. 4C: the run against it — **green on attempt 1 in three calls; the trap did not spring.** The fail-then-recover run is still owed, and `fixture_repo_2` can no longer supply it. See "The trap did not spring".

**5B — `probe_implementer.py`. 5/5.** Hand-constructs a plausible failing attempt on fixture 1 (`>` overcorrected to `>= tier.min_quantity - 1`), applies it through the real `apply_edits`, runs the real Tester, packages a real `TesterFailure`, resets to baseline, and hands plan + evidence to the real Implementer. One call; the plan is lifted verbatim from a saved fixture-1 transcript. The failing variant is 2 failed / 18 passed against the baseline's 1 failed / 19 passed, **and they are different tests** — it fixes the reported bug and breaks two other boundaries. Read "What the probe measures and what it cannot" before citing the 5/5 anywhere: on a plan that names the fix exactly, a green result cannot distinguish evidence-reading from plan-following, and that is a consequence of invariant 4 rather than a flaw in the probe. The `--no-evidence` control arm exists for that question and is a second call, not run by default.

**5A — `tasks/fixture_repo_3/` and `probe_reviewer.py`.** A `labels` package whose sibilant-plural bug has a one-row shortcut through the `IRREGULAR` table. Eighteen tests, one red at baseline; **both the shortcut and the correct fix turn the suite fully green**, which is the inversion of fixture 2 that makes this a Reviewer fixture rather than a Tester one. `probe_reviewer.py` feeds eight hand-written `(plan, diff)` pairs — five unfaithful, three controls — to the real `Reviewer` via `reviewer.run(state)`, on a plan fetched from the real Planner. Read "fixture_repo_3 — the unfaithful-in-scope diff" and "Measuring the Reviewer directly" before touching either, and read "The ruling: never engineer a fixture to blind the Planner" before editing the fixture's docstrings — the terseness of `plurals.py`'s module docstring is a decision, argued there, not an omission.

**4A — the escalation output.** `cli.report` now takes `(state, log_path)` and renders each terminal status differently: an attempt ledger and a what-is-on-disk line for `escalated_retry_limit`, the duplicated diff and the attempt it came from for `escalated_livelock`, pytest's exit code and traceback for `escalated_broken_suite`, the Reviewer's verdict and the refused diff for `aborted_by_human`, and `Recovered from a <kind> on attempt N-1` for a `succeeded` run that took a retry. New in `cli.py`: `read_events`, `attempt_ledger`, `rendered_attempt`, `OUTCOME_EVENTS`, `APPLIED_OUTCOME`. `test_cli.py` grew 37 → 66. Everything argued in full under "The escalation output".

**What Phase 4 did *not* have to build.** Evidence packaging and retry-with-evidence were already there — `tester_failure_from` and three `ReviewerRejection` sites in `loop.py` from 2.3, `render_evidence` and `RESET_NOTICE` in `implementer.py` from 3A. Phase 4's real content was the third item in its title.

**4B — `tasks/fixture_repo_2/`.** A `billing` package whose commission bug tempts a one-token fix to a shared rounding helper that two invisible sibling tests depend on. Twenty tests, one red at baseline; the naive fix turns that into two red *different* tests, and the correct fix is all-green. Both halves proven mechanically before any call was spent — see "fixture_repo_2 — the naive-fix trap", and read "The ruling: never engineer a fixture to blind the Planner" before touching it.

**The retry against a real model is still unexercised, and `fixture_repo_2` did not deliver it.** No model has yet received a `TesterFailure` or a `ReviewerRejection` in its prompt: `fixture_repo_1` is fixed on attempt 1 every time, and `fixture_repo_2`'s Planner fenced the trap off before the Implementer saw it. So `render_evidence` has still never produced a byte a model read, and `RESET_NOTICE` — the load-bearing sentence stopping a model from making an incremental edit on a baseline that never held its fix — is still argued rather than measured. Re-running `fixture_repo_2` will not change that: the Planner will narrow `target_files` again. The next route to the retry path is Phase 5, and the honest expectation is that it is a lottery there too — see the fixture 3 design.

**3B.** `harness/agents/reviewer.py` (`Reviewer`, `SYSTEM_PROMPT`, `render_plan`, `render_user_message`, `NONE_STATED`), the `ReviewVerdict` field reorder in `state.py`, and `--stub` in `cli.py` (`stub_targets`, `stub_agents`). Tests: `test_prompts.py` grew a Reviewer section (48 → 71) and `test_cli.py` grew a `--stub` section (20 → 37).

All four agents are now real on the default path, so **invariant 1 is satisfied by two independent gates** for the first time — a model's verdict on the diff, then a human's. Everything argued in full above: what is left for the Reviewer to judge once the mechanical checks have run, why it must not review the plan, the five anti-rubber-stamp guards and why none of them is "be skeptical", why an approving reason costs nothing downstream, why `render_plan` is duplicated, and why the contract stays at `plan` + `diff`.

**Two things 3B deliberately left unbuilt**, both recorded above rather than forgotten: a `Plan` validator rejecting test paths in `target_files` (see "A known hole"), and the `repo_path` widening (see "The contract stays at `plan` and `diff`"). Each has a named trigger; neither should be built on unease.

**The model-name drift is fixed, and it had three homes.** `DEFAULT_MODEL` had moved to `gemini-3.6-flash` while this file named `gemini-2.5-flash` twice and `test_llm.py` pinned the literal twice — the two test failures were sitting red before 3B started. Both tests are now bound to the constant, and `test_the_configured_model_id_is_sent` builds its own client with a model nothing else mentions, so it proves the plumbing carries what the caller configured instead of restating a constant. **Do not reintroduce a model literal into a test or into this file**; name `llm.DEFAULT_MODEL` and let the one definition be the one definition.

`test_prompts.py`'s stale `"claude-opus-5"` string is gone too, replaced by `DEFAULT_MODEL` for the same reason: it was an inert invented value that outlived the provider it named by a whole phase.

**A note on running the suite.** It spawns real pytest subprocesses, so **do not run two sessions of it at once.** A concurrent run pushed it from ~200s to 873s and made `test_tester.py`'s 120s Tester timeout fire on a fixture suite that finishes in 0.51s — three unrelated-looking failures that were all contention. If the Tester times out, suspect the machine before the code.

**3A.1.** `google-genai==2.19.0` replaces `anthropic` in `requirements.txt`; the key comes from `GEMINI_API_KEY` and the model is whatever `llm.DEFAULT_MODEL` names. The Anthropic API needs prepaid credits this project does not have. **One source file changed** — see "The transport swap" for what moved inside `harness/llm.py` and what did not move anywhere else, and "Free-tier limits are the real constraint" for the daily-quota arithmetic that now caps how many debugging runs a day holds.

**3A.** `harness/llm.py` (`LLMClient`, `LLMResult`, `extract_json`, `describe_problems`, `repair_prompt`, and the error hierarchy), `harness/agents/planner.py`, `harness/agents/implementer.py`, `cli.py`, plus `workspace.list_repo_files` / `read_repo_file` and a `target_files` validator on `Plan`.

Three test modules, none of which touch the network: `tests/test_llm.py` (71), `tests/test_prompts.py` (48), `tests/test_cli.py` (20).

`tests/test_llm.py`'s `client` fixture is **module-scoped**, for the reason `test_loop.py` shares its scenarios: a `genai.Client` costs about a second to construct (it sets up a trust store), and function scope made that module take a minute instead of five seconds. Safe because every test that issues a request replaces `_sdk` outright; a test needing different client *settings* builds its own.

**Every decision from the 3A design pass is argued in full above** — the dependency boundary, `repo_path` in two more contracts, the parse ladder, the repair turn, the failure taxonomy, `LLMResponseError` staying fatal, the `Plan` validator, and the entrypoint's shape. (3A's "there is no `--stub` flag yet" is now spent; see "`--stub`".)

Two things worth knowing before touching this next:

**The Implementer retries blind to its own last attempt.** Its contract is `plan` + `evidence`; `diff` belongs to the Reviewer and the gate, `previous_diffs` to the harness. This is fine for a scope violation (the offending paths are in `violated_constraints`) and largely fine for a test failure (the traceback describes how the code actually behaved). The thin case is a *judged* rejection — "this overreaches the plan" is hard to act on without seeing what you wrote. Left as-is deliberately; Phase 5 builds the fixture that exercises the Reviewer's judgment, and that is when it can be measured rather than guessed at.

**`loop.py`'s reason strings are now prompt text.** `"the Implementer produced no edits"` and `"edited N file(s) outside the plan's target_files: ..."` are read by a model, not just by a human. `render_evidence` quotes them under a `Reason given:` heading rather than inlining them into a sentence, which is why they still read correctly in the second person without needing to be rewritten. Anyone editing those strings is editing a prompt.

**One stale string, left deliberately.** `tests/test_prompts.py`'s `FakeClient` returns `LLMResult(model="claude-opus-5", ...)`. It is an invented value in a double that no assertion reads, so it is inert — but it is the last "claude" in the repo outside `llm.py`'s historical docstrings, and 3A.1's scope was llm.py, its tests, `requirements.txt`, and this file. Change it whenever `test_prompts.py` is next touched for a real reason.

**Livelock gets rarer from here.** The check is defined over byte-identical rendered diffs, and a real model sampling twice rarely produces them. That does not make the check wrong — it is still the right test for the condition it names — but do not expect real runs to trip it the way `test_loop.py` does.

**Next task: Phase 4 — failure evidence packaging, retry with evidence, escalation output.** It also owns the one open question left from 3A: whether `LLMResponseError` deserves a seventh `Status`. Decide that with evidence from real runs.

Worth doing before or alongside it: **a real end-to-end run.** Everything in Phase 3 is tested against fakes, and the whole of 3B's argument about judgment is unmeasured. `--stub` proves the wiring; only a real run against `fixture_repo_1` proves the prompts. Budget ~3 API calls for a green first attempt, ~11 for a run to the cap — see "Free-tier limits are the real constraint".

Previously: 2.3 — `harness/loop.py` (`run_task`, `render_diff`, `MAX_ATTEMPTS`, `LIVELOCK_REASON`), `workspace.apply_edits` and `workspace.normalize_path`, `Status.ESCALATED_BROKEN_SUITE`, `StubApprover`, and `out_of_scope_edits()`. `write_edits` now delegates to `apply_edits`. See "The control loop" above for the whole of it.

Decisions folded in from the 2.3 design pass, each argued in full above: agents as `run_task` parameters (operands, not a seam); `escalated_broken_suite` as a sixth status; the gate and apply step moved from Phase 3; per-attempt clearing of `edits`/`diff`/`review`/`test_result` with `evidence` surviving; normalized path comparison in the scope check; `render_diff` in `loop.py`; reset at top-of-iteration; `approve(diff) -> bool`; the no-edits branch; and the thirteen harness event names.

Previously: 2.2 — `harness/agents/stubs.py` (`StubPlanner`, `StubImplementer`, `StubReviewer`, `Script`, `ScriptExhausted`) and `tests/fixture_edits.py` (`failing_edits`, `fixing_edits`, `broken_edits`, `write_edits`). `tests/test_stubs.py`: 24 tests, the last five of which run the generators through the real Tester to prove red is red and green is green. See "Stub agents" and "Edit generators for the fixture" above.

`tests/fixture_edits.py` is a helper module, not a test module — it is importable as top-level `fixture_edits` because pytest's default `prepend` import mode puts `tests/` on `sys.path`. Do not add `tests/__init__.py`; that would change how the existing test modules are imported.

(`write_edits` was a test-only stand-in for the apply step while that step did not exist. As of 2.3 it delegates to the real `workspace.apply_edits` — see "Apply" above. It stays in `tests/` because what it now contributes is a name for the gate bypass, not an implementation.)

Previously: 2.1 — `harness/agents/base.py`: `Agent` ABC, the `CONTRACTS` table, `Contract`, `AgentContractError`. `tester.py` refitted onto it: `Tester(Agent)` with `name = "tester"` and `_run(*, repo_path)`; `run(repo_path, attempt=)` and the `test_run_completed` event are gone. `tests/test_agent_base.py`: 28 tests. `tests/test_tester.py` updated for the reshaped signature — a `run_tester` helper unwraps to the `TestResult` so the pytest-reporting tests read as they did, plus a `TestAgentInterface` class for the state-level contract. Suite is 139 tests, all passing, about 57s.

Previously: 1.4 — `harness/workspace.py` (`prepare_run_dir`, `reset_run_dir`, `run_dir_for`) and `harness/agents/tester.py` (`Tester`, `tester_failure_from`). `tests/test_tester.py`: 30 tests run against the real `fixture_repo_1`, not a mock — the Tester's whole job is reporting what a real subprocess said, so a stubbed subprocess would only test the stub. Suite is 82 tests, all passing, about 40s because of the real pytest subprocesses.

Then the `summary_parsed` decision, folded in above: added to `TestResult`, decided from the exit code, `True` on timeout, `False` on collection errors and crashes. The Tester's `requires`/`produces` class attributes were removed — unenforced until Phase 2's base class defines the real shape, and unenforced contracts drift.

Both `prepare_run_dir` and `reset_run_dir` take an optional `runs_root` so tests can point them at `tmp_path`. That is the only addition to the signatures as specified.

Previously: 1.3 — `tasks/fixture_repo_1/`: a `pricing` package (money → catalog → discounts → orders), 20 tests, exactly one failing. The bug is `>` where `>=` was meant in `discounts.tier_for`, so quantities landing exactly on a tier boundary get the tier below. `task.json` holds the real captured pytest output. Added `mach/pytest.ini` and a fixture-local `pytest.ini` — see "Keeping the two pytest suites apart".

Previously: 1.2 — `harness/events.py`. `EventLog` with a single `append` method, `logs/` created on init, one JSON object per line. `tests/test_events.py`: 27 tests.

Previously: 1.1 — `harness/state.py`, all seven models plus the `Status` StrEnum and `make_task_id()`, with `tests/test_state.py` (23 tests). Root `conftest.py` puts the repo root on `sys.path` so `tests/` can `import harness`, and sets `__test__ = False` on `TestResult` and `TesterFailure` — both match pytest's `Test*` collection glob, and the opt-out belongs there rather than in `state.py` or repeated in each test file.

Decisions folded in so far: the `Plan` shape with `target_files` as a mechanical scope check; `task_id` generated by the harness, not read from `task.json`; the `TestResult` shape; the scope check reusing `ReviewerRejection` rather than adding a third evidence type; `failure_input` kept verbatim and never sanitized.

Conventions worth not re-litigating: `None` means "not yet produced" and is what the agent contract assertion checks — so `edits` is `Optional`, never defaulting to `[]`, or "never ran" and "ran and produced nothing" become indistinguishable. Harness-owned fields with a meaningful empty value (`attempt_count`, `previous_diffs`, `status`) get real defaults instead. `extra="forbid"` everywhere. Paths are `str` in models, never `Path`, so state round-trips through the JSONL log with no custom serializer. `max_attempts` is a module constant in `loop.py`, not a `TaskState` field. The event log degrades a bad payload rather than raising — logging must not be able to kill a run.

(The three open shape questions this section used to carry — what the gate prints, what a run reports on exit, and whether `--stub` needs scripts — were all settled in 3A. See "task.json" and the CLI notes above.)

Phases 1–2 made zero API calls and that held. Phase 3 is the first one that does not — and the whole test suite still makes none: the SDK is faked wherever it is reached for, and no test needs an API key.

**Open questions:** whether `LLMResponseError` deserves a seventh `Status`. **Still deferred after Phase 4**, deliberately. The first all-real run produced `parse_attempts: 1` on all three calls and fired no repair turn — recorded as evidence *toward* the condition being rare, not as a resolution. One green run is evidence about one green run. See "LLMResponseError is fatal, for now".

**Settled in Phase 4, so do not reopen on the old motivation:** widening the gate to `approve(diff, review)`. The refusal rendering prints `state.review` after the halt, which buys what the widening was for while leaving the ownership table and `run_task`'s signature untouched. See "The refusal rendering prints `state.review`".
