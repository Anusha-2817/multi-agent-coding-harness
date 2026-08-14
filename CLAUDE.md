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
Plan → Implement → [render diff] → [livelock check] → Review → [human approval] → Apply → Test
          ↑                                                                                │
          └──────────────── reset to baseline (re-copy fixture) ──────────────────────────┘
                                    (on reject or fail)
```

Review happens **before** apply. A rejected diff never touches disk. The permission boundary is drawn at reversibility.

Render and livelock check are harness steps, not agents. They sit between Implement and Review so that a duplicate diff is caught before spending a review on it.

---

## Fixtures and the run directory

Fixtures are **plain directories with no git**. `tasks/fixture_repo_1/` is ordinary files plus a hand-authored `task.json`.

At run start the harness copies the fixture to `runs/<task_id>/` with `shutil.copytree`, ignoring `__pycache__` and `.pytest_cache`. All work — apply, test, reset — happens in the copy. The fixture directory itself is never written to.

**Baseline reset = delete `runs/<task_id>/` and re-copy from the fixture.** No git, no patch reversal, no cleanup logic to get wrong. `runs/` is gitignored.

This replaces an earlier `git checkout . && git clean -fd` design. A copy is simpler than a nested git repo inside the harness repo, needs no submodule or init script, and makes "identical ground" a property of the filesystem rather than of git's index state.

### task.json

`task_description` and `failure_input` are hand-authored in the fixture's `task.json`. The harness does not run pytest to discover the failure before planning — that would put the Tester ahead of the Planner and violate the stated order of operations. The failure text is an input to the task, not a product of it.

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
| `task_id`, `repo_path`, `task_description`, `failure_input` | harness (init, from `task.json`)    | Planner                       |
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

**Phase:** 0 — complete
**Last completed:** Repo scaffold (`harness/`, `harness/agents/`, `tasks/`, `tests/`, `cli.py`, `requirements.txt`, `.gitignore`) committed as `cfc3dbe`. All Phase 1 design questions resolved and folded into this file, committed as `6ada8df`. `CLAUDE.md` and `README.md` now live in the repo root alongside `cli.py` and are tracked, so project memory is versioned with the code it describes. `requirements.txt` re-saved as ASCII and installable.
**Next task:** 1.1 — `harness/state.py`: `TaskState`, `Plan`, `FileEdit`, `ReviewVerdict`, `TestResult`, `ReviewerRejection`, `TesterFailure`, and the status enum. Zero API calls.
**Open questions:** none
