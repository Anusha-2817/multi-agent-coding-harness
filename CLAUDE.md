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
| 4 | Failure evidence packaging, retry with evidence, escalation output | in progress — evidence packaging and retry-with-evidence were already built in 2.3/3A; 4A adds the escalation output |
| 5 | Fixture repo 3, Reviewer-rejection scenario, saved transcripts, README update — `fixture_repo_2` moved into Phase 4, which needed it for the recovery run | |

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

At run start the harness copies the fixture to `runs/<task_id>/` with `shutil.copytree`, ignoring `__pycache__` and `.pytest_cache`. All work — apply, test, reset — happens in the copy. The fixture directory itself is never written to.

**Baseline reset = delete `runs/<task_id>/` and re-copy from the fixture.** No git, no patch reversal, no cleanup logic to get wrong. `runs/` is gitignored.

This replaces an earlier `git checkout . && git clean -fd` design. A copy is simpler than a nested git repo inside the harness repo, needs no submodule or init script, and makes "identical ground" a property of the filesystem rather than of git's index state.

### Keeping the two pytest suites apart

The harness has a test suite and so does every fixture. They must never collect together — a fixture's deliberate bug is *data*, and reporting it as a harness failure would make the harness suite permanently red.

Two ini files, pulling in opposite directions:

- **`mach/pytest.ini`** sets `testpaths = tests` and adds `tasks` and `runs` to `norecursedirs`. `testpaths` covers a bare `pytest`; `norecursedirs` covers `pytest .` and explicit directory arguments. Its other job is pinning rootdir to `mach/` — with no ini file here, pytest walks up and roots itself in the parent of the repo. Note that setting `norecursedirs` *replaces* pytest's defaults rather than extending them, so the defaults are restated in the file; dropping them would send collection into `.venv` and `.git`.
- **`tasks/fixture_repo_1/pytest.ini`** gives the fixture its own rootdir. This is the load-bearing one. When the Tester runs pytest inside `runs/<task_id>/`, pytest walks up looking for an ini file and **stops at the fixture's own** — so rootdir is the run directory, and because `confcutdir` defaults to rootdir, the harness's `conftest.py` is never loaded either. The fixture is tested in isolation from the harness that is testing it. It also carries `pythonpath = .`, so `import pricing` resolves without an installed package or a `conftest.py` shim.

Every fixture repo needs its own `pytest.ini` for the same reason.

### task.json

`task.json` contains exactly two hand-authored fields: `task_description` and `failure_input`. Nothing else. The harness does not run pytest to discover the failure before planning — that would put the Tester ahead of the Planner and violate the stated order of operations. The failure text is an input to the task, not a product of it.

`failure_input` is the **real, verbatim** pytest output, captured by running the suite — never written from memory and never cleaned up. That includes the platform banner, the pytest and plugin version numbers, and the machine-specific absolute `rootdir:` line. **Do not sanitize it here, and do not sanitize it in future fixtures.**

Real failure reports are noisy, and the noise is part of the input. A Planner that only works once a human has trimmed the header off a traceback is overfit to the fixtures — it would be reading a format the harness manufactured rather than the one pytest actually emits, and that gap would not show up until the harness met a report from anywhere else.

The stale `rootdir:` is the clearest case. It points at `tasks/fixture_repo_1`, but at runtime the Tester works in `runs/<task_id>/` and its output will say so. A Planner that trips over that discrepancy has a defect worth finding in Phase 3, not a fixture worth editing.

### task_id

`task_id` is **generated by the harness**, not read from `task.json`. It identifies a *run*, not a task: it is both the `runs/` directory name and the log filename, so two runs against the same fixture must produce different ids.

Format: `<fixture_dir_name>_<UTC timestamp>` — e.g. `fixture_repo_1_20260814T120000Z`. The timestamp is compact rather than full ISO 8601 because it becomes a path segment and ISO's colons are illegal in Windows filenames. Built by `make_task_id()` in `harness/state.py`.

CLI:

```
python cli.py --task tasks/fixture_repo_1/task.json [--stub]
```

No YAML. JSON only. `cli.py` reads the task file, generates the `task_id`, prepares the run directory, builds the `TaskState`, constructs the four agents, and supplies the human at the gate. It validates that `task.json` holds *exactly* the two hand-authored fields at the boundary — a typo'd key should name the file it is in, not surface three steps later as a contract violation inside an agent.

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

**None of this is measured.** It is argued, not evidenced. The thing that could measure it is Phase 5's fixture — a diff that stays inside `target_files` and is unfaithful — and until that exists, `test_prompts.py` can only check the rendered text, not the judgment.

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

## Tech stack

| Part           | Choice                                                            |
| -------------- | ----------------------------------------------------------------- |
| Language       | Python 3.14, no agent framework                                   |
| LLM transport  | official `google-genai` SDK — see "The one dependency, and where it stops" |
| LLM model      | `llm.DEFAULT_MODEL` — `gemini-3.6-flash` today, `response_json_schema`, dynamic thinking (default) |
| State          | Pydantic v2                                                       |
| Diff rendering | `difflib.unified_diff` over `edits` + baseline                    |
| Test execution | `subprocess` running `pytest`                                     |
| Event log      | append-only `.jsonl`, one file per run                            |
| Baseline reset | delete `runs/<task_id>/`, re-copy fixture via `shutil.copytree`   |
| Entrypoint     | `python cli.py --task <path/to/task.json> [--stub]`               |

The Tester runs the **whole** fixture suite, not just the target test — "fix it without breaking anything else" is only verifiable against the full suite. Before invoking pytest it clears `__pycache__` and `.pytest_cache` in the run directory. Stale caches cause phantom results.

It invokes `[sys.executable, "-m", "pytest"]` so the subprocess uses the harness's own interpreter and virtualenv, with a **120s default timeout**. A hung pytest returns a `TestResult` with `passed=False`, `exit_code=-1` (deliberately outside pytest's own 0–5 range), and the timeout recorded in `traceback` — it never propagates an exception into the loop.

`failed_tests` is read out of pytest's "short test summary info" section as text. Not the reporting API, which would couple the harness to a pytest version, and not a plugin, which would be a second thing to keep working. That section is only consulted for exit code 1 — see `summary_parsed` under "TestResult".

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
runs/               gitignored — working copies, one per task_id
logs/               gitignored — <task_id>.jsonl, one per run
tests/              tests for the harness itself
cli.py
CLAUDE.md
README.md
```

---

## Working conventions

- Small commits, one logical change each, real messages.
- Write the test before or alongside the code, not after the phase.
- Stub agents before real agents. Phases 1–2 make **zero API calls** — if a session is adding an API call before Phase 3, something is out of order.
- Prefer boring, readable code. This project is read by interviewers.
- When a decision is made mid-session that contradicts or extends this file, update this file in the same commit.

---

## Current status

Update this section at the end of every session. It is the first thing to read next session.

**Phase:** 4 — **in progress.** 4A: the escalation output. 4B: `tasks/fixture_repo_2/`, the naive-fix trap. Still owed: the fail-then-recover run itself, which is the first API spend since 3B.
**Last completed:** 4A. Suite is **432 tests, all passing, about 195s.**

**4A — the escalation output.** `cli.report` now takes `(state, log_path)` and renders each terminal status differently: an attempt ledger and a what-is-on-disk line for `escalated_retry_limit`, the duplicated diff and the attempt it came from for `escalated_livelock`, pytest's exit code and traceback for `escalated_broken_suite`, the Reviewer's verdict and the refused diff for `aborted_by_human`, and `Recovered from a <kind> on attempt N-1` for a `succeeded` run that took a retry. New in `cli.py`: `read_events`, `attempt_ledger`, `rendered_attempt`, `OUTCOME_EVENTS`, `APPLIED_OUTCOME`. `test_cli.py` grew 37 → 66. Everything argued in full under "The escalation output".

**What Phase 4 did *not* have to build.** Evidence packaging and retry-with-evidence were already there — `tester_failure_from` and three `ReviewerRejection` sites in `loop.py` from 2.3, `render_evidence` and `RESET_NOTICE` in `implementer.py` from 3A. Phase 4's real content was the third item in its title.

**4B — `tasks/fixture_repo_2/`.** A `billing` package whose commission bug tempts a one-token fix to a shared rounding helper that two invisible sibling tests depend on. Twenty tests, one red at baseline; the naive fix turns that into two red *different* tests, and the correct fix is all-green. Both halves proven mechanically before any call was spent — see "fixture_repo_2 — the naive-fix trap", and read "The ruling: never engineer a fixture to blind the Planner" before touching it.

**The one thing still unexercised is the retry against a real model.** No model has ever received a `TesterFailure` or a `ReviewerRejection` in its prompt: `fixture_repo_1` gets fixed on attempt 1 every time, so `render_evidence` has never produced a byte a model read, and `RESET_NOTICE` — the load-bearing sentence stopping a model from making an incremental edit on a baseline that never held its fix — is still an argued claim rather than a measured one. That is what `tasks/fixture_repo_2/` is for; see "Fixture design: the recoverability constraint" for what shape it has to be, and budget ~5 API calls for the run (1 Planner + 2 × (Implementer + Reviewer)).

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
