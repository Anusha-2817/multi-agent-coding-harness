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
| 3 | LLM client wrapper, real Planner, real Implementer, real Reviewer, `--stub`/`--real` switch | in progress — 3A done (client, Planner, Implementer, `cli.py`), 3A.1 done (Anthropic → Gemini) |
| 4 | Failure evidence packaging, retry with evidence, escalation output | |
| 5 | Fixture repos 2–3, Reviewer-rejection scenario, saved transcripts, README update | |

**Phase 5 note.** Now that the scope check runs first, a diff touching an out-of-scope file never reaches the Reviewer — it is caught mechanically and routed back. So the Reviewer-rejection scenario can no longer be built from an out-of-scope edit. Exercising the Reviewer's *judgment* requires a diff that **stays inside `target_files` but is unfaithful to the plan** — right files, wrong change: overreaching within an allowed file, solving a different problem, or gutting behaviour the plan meant to preserve. Design fixture repo 2 or 3 with that in mind.

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
python cli.py --task tasks/fixture_repo_1/task.json
```

No YAML. JSON only. `cli.py` reads the task file, generates the `task_id`, prepares the run directory, builds the `TaskState`, constructs the four agents, and supplies the human at the gate. It validates that `task.json` holds *exactly* the two hand-authored fields at the boundary — a typo'd key should name the file it is in, not surface three steps later as a contract violation inside an agent.

**There is no `--stub` flag, and that is a decision, not an omission.** It had nothing coherent to switch at 3A: the Reviewer is stubbed either way, and stubbing the Planner and Implementer from the CLI would need the specific scripts only a test can supply — the same argument that made the agents `run_task` parameters rather than a boolean. It arrives with the real Reviewer in 3B, when it finally means something: all three LLM-backed agents, or none.

**The terminal gate takes the diff and nothing else**, per the ownership table. The note under "The control loop" imagined `cli.py` showing the plan and the verdict alongside it — but the plan does not exist when the callback is built, and widening `run_task`'s signature has no reason to happen before the Reviewer is real. An unreadable stdin (a pipe, a CI job, `< /dev/null`) is read as a refusal: a gate whose failure mode is "approve" is not a gate.

**At 3A the human is the only real gate.** Invariant 1 needs a Review verdict *and* a human approval; until 3B the Reviewer is a stub approving every diff, so one of those two is a stand-in. Worth being plain about rather than discovering from the logs.

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
ReviewVerdict: {approved: bool, reason: str, violated_constraints: list[str]}
```

The Reviewer produces the verdict. The **harness** converts a rejection into a `ReviewerRejection` and writes it to `evidence` — the Reviewer never writes `evidence` itself, consistent with the ownership table.

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

A seventh `Status` is **deliberately deferred to Phase 4**, which owns escalation output. `escalated_broken_suite` earned its place because the other two escalations would have been lies about a condition the loop reaches on a normal path; this one is rare, and a stack trace naming the raw text is more useful than a status value. Adding one now would be justified by a guess about frequency.

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

**Google no longer publishes a static free-tier table.** The rate-limits page defers to a per-account dashboard, so the numbers are yours to read rather than ours to quote: <https://aistudio.google.com/rate-limit>. What is stable is the shape — free tier is capped on requests per minute, tokens per minute, and **requests per day**, with RPD in the tens-to-low-hundreds depending on model, and `gemini-2.5-flash` more generous than `gemini-2.5-pro`.

What matters more than the number is the arithmetic against it, and that is exact:

| Run | API calls |
| --- | --------- |
| Happy path (plan, one attempt, green) | **2** |
| One retry, then green | 3 |
| Five attempts to the cap | **6** |
| Any of the above, per parse repair | +1 each |

One Planner call plus one Implementer call per attempt. The real Reviewer in 3B adds one per attempt that reaches review, roughly doubling a failing run. So a day's RPD divided by ~6 is the honest ceiling on debugging runs, and a run that dies on a `MAX_TOKENS` at attempt four has still spent five calls.

Two consequences worth designing around:

- **A daily-quota 429 is not a transient failure.** The SDK will retry it three times with backoff and fail anyway. `_is_daily_quota` exists so the resulting error says "tomorrow" rather than "shortly" — see the failure taxonomy above.
- **The parse repair costs a whole request.** That is a second reason, beyond token cost, to keep `max_parse_retries` at 1: on a metered daily allowance, a second repair would trade a real debugging run for a model that already failed twice.

### Reading the repo for a prompt

`workspace.list_repo_files` and `workspace.read_repo_file`. They live beside `apply_edits` because that module owns the run directory, so the two new agents do not each grow their own `Path` arithmetic and their own idea of which directories to skip.

`read_repo_file` keeps its own containment check rather than sharing one with `apply_edits` — the two raise for different reasons and a reader borrowing the writer's message would misdescribe what went wrong. What they share is `normalize_path`, which is the part that has to agree: a reader accepting a spelling the scope check would reject would show the Implementer a file it is not allowed to edit. The check is reachable, not theoretical — `normalize_path` deliberately does not resolve `..` away, so a plan can name an escaping path in `target_files` and the scope check will accept it. Catching it on the read means it never reaches the write.

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
| LLM model      | `gemini-2.5-flash`, `response_json_schema`, dynamic thinking (default) |
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
    planner.py
    implementer.py
    reviewer.py
    tester.py
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

**Phase:** 3 — in progress. 3A done: the LLM client, the real Planner, the real Implementer, and `cli.py`. 3A.1 done: the provider swap.
**Last completed:** 3A.1 — Anthropic → Gemini, a transport-only change. Suite is **363 tests, all passing, about 170s.**

**3A.1.** `google-genai==2.19.0` replaces `anthropic` in `requirements.txt`; the key comes from `GEMINI_API_KEY` and the model is `gemini-2.5-flash`. The Anthropic API needs prepaid credits this project does not have. **One source file changed** — see "The transport swap" for what moved inside `harness/llm.py` and what did not move anywhere else, and "Free-tier limits are the real constraint" for the daily-quota arithmetic that now caps how many debugging runs a day holds.

**3A.** `harness/llm.py` (`LLMClient`, `LLMResult`, `extract_json`, `describe_problems`, `repair_prompt`, and the error hierarchy), `harness/agents/planner.py`, `harness/agents/implementer.py`, `cli.py`, plus `workspace.list_repo_files` / `read_repo_file` and a `target_files` validator on `Plan`.

Three test modules, none of which touch the network: `tests/test_llm.py` (71), `tests/test_prompts.py` (48), `tests/test_cli.py` (20).

`tests/test_llm.py`'s `client` fixture is **module-scoped**, for the reason `test_loop.py` shares its scenarios: a `genai.Client` costs about a second to construct (it sets up a trust store), and function scope made that module take a minute instead of five seconds. Safe because every test that issues a request replaces `_sdk` outright; a test needing different client *settings* builds its own.

**Every decision from the 3A design pass is argued in full above** — the dependency boundary, `repo_path` in two more contracts, the parse ladder, the repair turn, the failure taxonomy, `LLMResponseError` staying fatal, the `Plan` validator, the entrypoint's shape, and why there is no `--stub` flag yet.

Two things worth knowing before touching this next:

**The Implementer retries blind to its own last attempt.** Its contract is `plan` + `evidence`; `diff` belongs to the Reviewer and the gate, `previous_diffs` to the harness. This is fine for a scope violation (the offending paths are in `violated_constraints`) and largely fine for a test failure (the traceback describes how the code actually behaved). The thin case is a *judged* rejection — "this overreaches the plan" is hard to act on without seeing what you wrote. Left as-is deliberately; Phase 5 builds the fixture that exercises the Reviewer's judgment, and that is when it can be measured rather than guessed at.

**`loop.py`'s reason strings are now prompt text.** `"the Implementer produced no edits"` and `"edited N file(s) outside the plan's target_files: ..."` are read by a model, not just by a human. `render_evidence` quotes them under a `Reason given:` heading rather than inlining them into a sentence, which is why they still read correctly in the second person without needing to be rewritten. Anyone editing those strings is editing a prompt.

**One stale string, left deliberately.** `tests/test_prompts.py`'s `FakeClient` returns `LLMResult(model="claude-opus-5", ...)`. It is an invented value in a double that no assertion reads, so it is inert — but it is the last "claude" in the repo outside `llm.py`'s historical docstrings, and 3A.1's scope was llm.py, its tests, `requirements.txt`, and this file. Change it whenever `test_prompts.py` is next touched for a real reason.

**Livelock gets rarer from here.** The check is defined over byte-identical rendered diffs, and a real model sampling twice rarely produces them. That does not make the check wrong — it is still the right test for the condition it names — but do not expect real runs to trip it the way `test_loop.py` does.

**Next task: 3B — the real Reviewer.** `reviewer.py` is the last stubbed LLM-backed agent, and the one whose prompt matters most: it reads `plan` and `diff`, never `test_result` (invariant 2), and judges faithfulness and minimality now that the scope check has taken mechanical scope off its plate. Its verdict is `ReviewVerdict`, already an object at the schema root, so it needs no envelope. `--stub` lands with it, meaning "all three LLM-backed agents, or none". Until then `cli.py` approves every diff by script and the human is the only real gate.

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

**Open questions:** whether `LLMResponseError` deserves a seventh `Status`. Deferred to Phase 4 on purpose — see "LLMResponseError is fatal, for now". Decide it with evidence from real runs, not before.
