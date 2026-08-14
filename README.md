# Multi-Agent Coding Harness

A framework-free, hand-rolled orchestration system where specialized agents — Planner, Implementer, Reviewer, Tester — collaborate through typed state handoffs to resolve coding tasks, with built-in failure recovery and a human-approval gate before any irreversible action.

**Status:** in progress. This README documents the intended design; sections are marked as they move from planned → implemented.

---

## Problem

Most "AI coding agent" projects are either:

- A single LLM call wrapped in a chat UI — no verification, no recovery, no memory of what already failed.
- A RAG pipeline dressed up as an "agent" — it retrieves and answers, but never decides, acts, or corrects itself.

Neither approach reflects how real engineering work happens. A human engineer doesn't write code once and call it done — they plan, implement, review their own work, run tests, and go back and fix things when something breaks. That loop — plan → act → observe → correct — is the actual hard part of "agentic" systems, and it's the part most projects skip.

There's also a known industry finding worth anchoring to: in LangChain's Terminal-Bench experiment, changing *only the harness* (the control loop and scaffolding around the model) — with the same underlying model — moved a coding agent from ~30th place into the top 5. The harness, not the model, is what makes a coding agent good. This project is built on that premise.

## Proposed solution

Build a small, hand-rolled multi-agent harness that mirrors a real engineering workflow, scoped to one fixed task class first: **given a repo with a deliberately failing test, fix it, without breaking anything else.**

Four role-pure agents, each with exactly one job:

| Agent | Job | Never does |
|---|---|---|
| Planner | Reads the failure/requirement, produces a plan (suspected file, fix approach, acceptance criteria) | Write code |
| Implementer | Reads the plan, edits the repo, produces a diff | Judge its own correctness |
| Reviewer | Checks the diff against the plan — scope, intent, minimality | Run tests |
| Tester | Runs the real test suite, reports structured pass/fail evidence | Decide what the fix should be |

The system is a **loop, not a one-shot pipeline**: if the Tester fails or the Reviewer rejects, the harness routes the task back to the Implementer with structured failure evidence attached, and tries again — up to a retry limit, after which it escalates for human input.

**Sequence:** Plan → Implement → Review → Approve → Apply → Test. The Reviewer works on diff text, not on an applied change — so it runs *before* the approval gate, not after. This keeps the permission boundary consistent with its own principle (drawn at reversibility): a rejected diff never touches disk at all, so there's nothing to undo. Earlier drafts of this design had Approve before Review, which meant a Reviewer rejection required reverting an already-applied change — that inconsistency is why the order was corrected.

**Retry semantics:** every diff, on every attempt, passes through Review → Approve → Apply → Test — there is no fast path on retry; approval is granted per-diff, never per-task. Before each retry, the fixture is hard-reset to its known-good baseline (fixtures are git-initialized, baseline committed, `git checkout .` before every attempt), so every attempt is a fresh diff against identical ground. This keeps the Reviewer's minimality judgment consistent across attempts and makes repeated-identical-diff detection a trivial string comparison. Reviewer rejection (diff never applied) and Tester failure (diff applied, then reverted) both route back to the Implementer with structured evidence, and share a single `attempt_count` on the task — not separate counters per failure type, which would otherwise let the loop alternate between rejection and failure indefinitely without ever hitting a limit.

A fixed task class (bug repair, not open-ended feature work) was chosen deliberately for v1. It isolates *harness plumbing bugs* from *LLM planning/reasoning quality* — if something fails, it's far more likely to be a coordination bug than an ambiguous-requirement problem, which makes v1 much faster to debug and prove correct. Feature implementation (ambiguous requirement → plan → code → review → test) is a natural v2 once the loop itself is proven solid.

Attempts are capped at 5. If the Implementer produces a byte-identical diff on two attempts — detectable precisely because every attempt starts from the same baseline — the harness halts early and escalates, reporting that the plan is the likely fault rather than the implementation. Routing rejections back to the Planner for revision is a natural v2; v1 diagnoses the condition without acting on it.

## Architectural choices

**Typed state, not chat history.** Agents don't share a growing message log — they read and write specific fields on one shared `TaskState` object (Pydantic). Planner never touches `diff`. Tester never touches `plan`. This keeps handoffs structured, cheap (each agent reads only what it needs, not the whole conversation), and debuggable — you can inspect exactly what any agent saw at any step.

**Hand-rolled control loop, not a framework.** No LangGraph, no LangChain, no CrewAI — at least for v1. The loop is a plain Python `while` with conditional routing. This is a deliberate sequencing choice, not a rejection of frameworks: the goal is to fully understand state management, retries, and routing by building them, so that if a framework is adopted later, it's an informed upgrade over something already understood, not a starting crutch.

**Human-approval gate before irreversible actions.** Any action that writes to disk (applying a diff) pauses for explicit confirmation. Read-only actions (reading a file, running tests) don't need this — a wrong output there is cheap to ignore. A wrong write to disk is real, and can't be un-done the same way. The permission boundary is drawn at *reversibility*, not at the agent's confidence.

**Append-only event log (JSONL), not just in-memory state.** Every step — every plan, diff, review verdict, test result, retry — is appended to a log file. This gives full replayability: you can reconstruct exactly what happened in any run, which is what makes debugging multi-agent failures possible instead of guessing.

**Agents declare their contracts.** Each agent declares which TaskState fields it requires and which it produces, checked before it runs. A missing field fails loudly and immediately, naming the agent, rather than surfacing later as an empty diff or a confused plan. This is the enforcement half of role purity — the field-ownership table isn't a convention, it's asserted.

**Loop first, graph-as-data later if needed.** The control flow is currently `if`/`elif` routing. If a second task mode (feature implementation) is added, routing may be reshaped into a small hand-rolled `edges = {...}` dict — still framework-free, just a cleaner shape for two modes sharing agents but branching differently. Not needed for v1.

## Tech stack

| Part | Choice | Why |
|---|---|---|
| Language | Python, no agent framework | Full control and understanding of every mechanic |
| State | Pydantic (`TaskState`) | Typed fields, validation, no raw chat history |
| LLM calls | One OpenAI-compatible / Anthropic API | Each agent is a differently-prompted call to the same model |
| Test execution | `subprocess` running `pytest` | Real test execution — ground truth, not an LLM's opinion |
| Event log | Flat `.jsonl` file, append-only | Simple, human-readable, sufficient at this scale |
| Diff application | Direct file read/write, or `git diff`/patch | Whichever is simplest to get an Implementer writing real changes |
| Entrypoint | CLI (`python cli.py`) | No UI needed to prove the loop works |

## Where and how this gets tested

Testing happens entirely locally, against small, controlled fixture repositories — not against real-world repos, at least for v1.

**Fixture repo setup:**
- One small Python repo (5–10 files) per test scenario, checked into `tasks/fixture_repo_N/`.
- Each fixture starts in a **known-good state** except for one deliberately injected bug (e.g. an off-by-one in a loop, a wrong comparison operator, a missing null check).
- Each fixture ships with its own test suite, where exactly one test fails because of the injected bug.

**What "injecting a test case" means concretely:**
1. Take a small working function.
2. Introduce one deliberate, realistic bug.
3. Write (or already have) a test that fails specifically because of that bug.
4. Point the harness at the repo with only the failure/traceback as input — the harness never sees the "correct" answer.

**What gets tested, in order of build priority:**
1. **Single scenario, single bug** — does the full loop (Plan → Implement → Approve → Review → Test) run end to end and actually fix it?
2. **Deliberate Implementer failure** — seed a bad first attempt, confirm the retry-on-failure path actually fires and the Implementer receives useful failure evidence.
3. **Multiple fixture repos (2–3), different bugs, different files** — confirms the harness generalizes within the fixed task class rather than being hardcoded to one scenario.
4. **Reviewer rejection path** — confirm a diff that technically passes tests but violates scope (e.g. touches unrelated files) gets caught and routed back.

**What's explicitly out of scope for testing right now:** real-world/large repos, non-Python codebases, concurrent/parallel task runs, anything requiring network calls beyond the LLM API itself.

## Deployment — what this actually is at the end

This is **not** a web app and **not** initially exposed as a hosted API. At the end of the 2-week build, the deliverable is:

**A local CLI tool.** You run `python cli.py --repo path/to/fixture --task task.yaml` from a terminal, and watch the agent loop reason and act step by step, printed live — plan, diff, approval prompt, review verdict, test result, retry if needed. The "demo" is running this live in front of an interviewer, not clicking through a UI.

**Why not a web UI or hosted API for v1:** neither adds to the thing actually being demonstrated (the harness's decision-making and recovery), and both cost real build time that's better spent making the loop itself solid across multiple fixture repos.

**Natural next steps, if there's time or later interest — not required for the current scope:**
- Wrap the harness in a thin FastAPI layer (`POST /tasks` → runs the loop, returns the final `TaskState` + event log) — turns it into a callable service.
- A minimal Streamlit or terminal-UI (`rich`/`textual`) view over the same event log, for a more visual trace instead of raw stdout.
- Containerizing the CLI (Docker) purely for reproducibility, not for hosting.

## Scalability — how this would be hardened past a portfolio project

The current design (flat JSONL log, single local process, no persistence beyond the file) is intentionally right-sized for a small number of local runs. It has known limits, and the fixes are known but not built for v1:

| Limitation at scale | Fix | Idea |
|---|---|---|
| Log file grows unbounded | Log rotation / segmentation | New log file per day or size threshold, old ones archived — same idea as Kafka's log segments |
| Concurrent writes could corrupt the log | Single-writer principle | Only one process ever writes to the log directly; everything else sends events through it |
| Querying the log is a linear scan | SQLite side-index | Keep the JSONL as source of truth; mirror key fields (task_id, agent, status, timestamp) into a small SQLite table for fast queries |
| Replaying state from scratch gets expensive | Snapshotting / checkpointing | Periodically save a full `TaskState` snapshot; replay only events since the last snapshot — the same pattern LangGraph's checkpointer implements |

None of these are implemented in v1 by design — they're documented here as the answer to "how would this scale," not as unfinished work.

## Roadmap

- [ ] Core loop: Planner → Implementer → approval gate → Reviewer → Tester, single fixture repo
- [ ] Retry routing: Tester/Reviewer failure → back to Implementer with structured evidence
- [ ] Event log: append-only JSONL, one line per step
- [ ] Validate across 2–3 fixture repos with different injected bugs
- [ ] Saved run transcripts for demo purposes
- [ ] (Stretch) Second task mode: small feature implementation from a structured requirement
- [ ] (Stretch) Hand-rolled edges-as-data routing if a second mode is added
- [ ] (Stretch / v2) Livelock detection: if the Reviewer rejects a widened diff repeatedly because the original plan specified a narrower fix, that pattern signals the *plan* is wrong, not the diff — route back to the Planner instead of the Implementer. For v1, this just hits the retry limit and escalates to a human; the smarter routing is a deliberate v2 cut, not an oversight