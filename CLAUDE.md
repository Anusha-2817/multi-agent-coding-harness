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

---

## Phases

Build order. Phases 1–2 make **zero API calls**.

| Phase | Contents | Status |
| ----- | -------- | ------ |
| 0 | Scaffold, CLAUDE.md, first commit | done |
| 1 | `TaskState` + all models, `EventLog`, fixture repo 1, Tester, unit tests | next |
| 2 | Agent base class with requires/produces assertion, stub agents, control loop (routing, retry, livelock, escalation), scripted failure tests | |
| 3 | LLM client wrapper, real Planner, real Implementer, approval gate, apply step, real Reviewer, `--stub`/`--real` switch | |
| 4 | Failure evidence packaging, retry with evidence, escalation output | |
| 5 | Fixture repos 2–3, Reviewer-rejection scenario, saved transcripts, README update | |

**Phase 5 note.** Now that the scope check runs first, a diff touching an out-of-scope file never reaches the Reviewer — it is caught mechanically and routed back. So the Reviewer-rejection scenario can no longer be built from an out-of-scope edit. Exercising the Reviewer's *judgment* requires a diff that **stays inside `target_files` but is unfaithful to the plan** — right files, wrong change: overreaching within an allowed file, solving a different problem, or gutting behaviour the plan meant to preserve. Design fixture repo 2 or 3 with that in mind.

The Tester lands in Phase 1, before any agent scaffolding, because it is the only agent that needs no LLM and because a working Tester is what makes Phase 2's scripted failures verifiable.

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
Plan → Implement → [scope check] → [render diff] → [livelock check] → Review → [human approval] → Apply → Test
          ↑                                                                                              │
          └──────────────────────── reset to baseline (re-copy fixture) ────────────────────────────────┘
                                    (on reject or fail)
```

Review happens **before** apply. A rejected diff never touches disk. The permission boundary is drawn at reversibility.

Scope check, render, and livelock check are harness steps, not agents. They sit between Implement and Review so that an out-of-scope or duplicate diff is caught before spending a review on it. Scope check runs first because it is the cheapest — it compares `edit.path` against `plan.target_files` and needs no rendered diff.

---

## Fixtures and the run directory

Fixtures are **plain directories with no git**. `tasks/fixture_repo_1/` is ordinary files plus a hand-authored `task.json`.

At run start the harness copies the fixture to `runs/<task_id>/` with `shutil.copytree`, ignoring `__pycache__` and `.pytest_cache`. All work — apply, test, reset — happens in the copy. The fixture directory itself is never written to.

**Baseline reset = delete `runs/<task_id>/` and re-copy from the fixture.** No git, no patch reversal, no cleanup logic to get wrong. `runs/` is gitignored.

This replaces an earlier `git checkout . && git clean -fd` design. A copy is simpler than a nested git repo inside the harness repo, needs no submodule or init script, and makes "identical ground" a property of the filesystem rather than of git's index state.

### task.json

`task.json` contains exactly two hand-authored fields: `task_description` and `failure_input`. Nothing else. The harness does not run pytest to discover the failure before planning — that would put the Tester ahead of the Planner and violate the stated order of operations. The failure text is an input to the task, not a product of it.

### task_id

`task_id` is **generated by the harness**, not read from `task.json`. It identifies a *run*, not a task: it is both the `runs/` directory name and the log filename, so two runs against the same fixture must produce different ids.

Format: `<fixture_dir_name>_<UTC timestamp>` — e.g. `fixture_repo_1_20260814T120000Z`. The timestamp is compact rather than full ISO 8601 because it becomes a path segment and ISO's colons are illegal in Windows filenames. Built by `make_task_id()` in `harness/state.py`.

CLI:

```
python cli.py --task tasks/fixture_repo_1/task.json [--stub]
```

No YAML. JSON only.

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

### TestResult

```python
TestResult: {passed: bool, failed_tests: list[str], traceback: str, stdout: str, exit_code: int}
```

`failed_tests` holds pytest **nodeids** (`tests/test_calculator.py::test_add`). `traceback` is the empty string on a pass.

Deliberately a superset of what `TesterFailure` needs, because the **harness** — not the Tester — derives the evidence from it, the same way it derives `ReviewerRejection` from a rejecting verdict. `stdout` is kept whole here and truncated to `stdout_tail` only at the evidence boundary, so the event log retains the full run output even when the Implementer sees only the tail.

### Agent contracts

Each agent declares `requires` and `produces`. The base class asserts required fields are present and non-None **before** calling the LLM, and raises naming the agent and the missing field.

| Agent       | requires                            | produces      | must never read       |
| ----------- | ----------------------------------- | ------------- | --------------------- |
| Planner     | `task_description`, `failure_input` | `plan`        | `diff`, `test_result` |
| Implementer | `plan` (+ `evidence` on retry)      | `edits`       | `test_result`         |
| Reviewer    | `plan`, `diff`                      | `review`      | `test_result`         |
| Tester      | `repo_path`                         | `test_result` | `plan`, `diff`        |

This is the enforcement half of role purity. Prompts are suggestions; missing fields are guarantees.

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
status: running | succeeded | escalated_retry_limit | escalated_livelock | aborted_by_human
```

`max_attempts = 5` means **five Implementer runs.** `attempt_count` increments when the Implementer runs — not when something fails — so the number always answers "how many diffs has this task produced."

A livelock halt does **not** consume an attempt. It ends the run on the spot; there is no next attempt for the count to describe.

### Human rejection at the approval gate

A human "no" **halts the run** with status `aborted_by_human`. It does not create a new evidence type, does not route back to the Implementer, and does not consume an attempt.

The human is rejecting for reasons the harness cannot see — out-of-band context about the repo, the task, or the approach. Feeding a retry that the harness can't articulate is asking the Implementer to guess at an objection it was never told, which is wasted work and wasted tokens. Stop, and let the human act on what they know.

---

## Livelock detection

Because every attempt starts from an identical baseline, two identical diffs are byte-identical text. The check runs **immediately after the Implementer, before Review**: render the diff, compare against `previous_diffs`, and if it is already there, halt with `escalated_livelock` and escalate with — _the plan is the likely fault, not the implementation._

Comparison is literal byte equality. No whitespace normalization, no fuzzy matching. If the loop is genuinely stuck, the text will be identical; anything looser risks halting a run that was actually making progress.

v1 diagnoses this. It does not act on it by replanning.

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

---

## Retry vs. backoff — do not conflate

- **Agent retries** (Reviewer rejection, Tester failure) → route back to Implementer with evidence. No delay. The model is stateless; waiting changes nothing about the next output.
- **HTTP retries** (429, 5xx) → live in the LLM client wrapper, with exponential backoff. Never in the agent loop.

---

## Tech stack

| Part           | Choice                                                            |
| -------------- | ----------------------------------------------------------------- |
| Language       | Python 3.14, no agent framework                                   |
| State          | Pydantic v2                                                       |
| Diff rendering | `difflib.unified_diff` over `edits` + baseline                    |
| Test execution | `subprocess` running `pytest`                                     |
| Event log      | append-only `.jsonl`, one file per run                            |
| Baseline reset | delete `runs/<task_id>/`, re-copy fixture via `shutil.copytree`   |
| Entrypoint     | `python cli.py --task <path/to/task.json> [--stub]`               |

The Tester runs the **whole** fixture suite, not just the target test — "fix it without breaking anything else" is only verifiable against the full suite. Before invoking pytest it clears `__pycache__` and `.pytest_cache` in the run directory. Stale caches cause phantom results.

---

## Repo layout

Repo root is `mach/`.

```
harness/
  state.py          TaskState, Plan, FileEdit, ReviewVerdict, TestResult, evidence types
  events.py         EventLog — append-only JSONL writer
  loop.py           the control loop: routing, retries, escalation
  llm.py            LLM client wrapper (HTTP retry/backoff lives here)
  agents/
    base.py         Agent ABC, requires/produces assertion
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

**Phase:** 1 — in progress
**Last completed:** 1.2 — `harness/events.py`. `EventLog` with a single `append` method, `logs/` created on init, one JSON object per line. `tests/test_events.py`: 27 tests. Suite is 50 tests, all passing.

Previously: 1.1 — `harness/state.py`, all seven models plus the `Status` StrEnum and `make_task_id()`, with `tests/test_state.py` (23 tests). Root `conftest.py` puts the repo root on `sys.path` so `tests/` can `import harness`, and sets `__test__ = False` on `TestResult` and `TesterFailure` — both match pytest's `Test*` collection glob, and the opt-out belongs there rather than in `state.py` or repeated in each test file.

Decisions folded in so far: the `Plan` shape with `target_files` as a mechanical scope check; `task_id` generated by the harness, not read from `task.json`; the `TestResult` shape; the scope check reusing `ReviewerRejection` rather than adding a third evidence type.

Conventions worth not re-litigating: `None` means "not yet produced" and is what the agent contract assertion checks — so `edits` is `Optional`, never defaulting to `[]`, or "never ran" and "ran and produced nothing" become indistinguishable. Harness-owned fields with a meaningful empty value (`attempt_count`, `previous_diffs`, `status`) get real defaults instead. `extra="forbid"` everywhere. Paths are `str` in models, never `Path`, so state round-trips through the JSONL log with no custom serializer. `max_attempts` is a module constant in `loop.py`, not a `TaskState` field. The event log degrades a bad payload rather than raising — logging must not be able to kill a run.

**Next task:** 1.3 — `tasks/fixture_repo_1/`: a plain directory, no git, with a hand-authored `task.json` carrying `task_description` and `failure_input`, plus one deliberately failing test. Then 1.4, the Tester. Zero API calls.
**Open questions:** none
