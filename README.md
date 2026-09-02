# Multi-Agent Coding Harness

A framework-free, hand-rolled orchestration system where specialized agents — Planner,
Implementer, Reviewer, Tester — collaborate through typed state handoffs to resolve coding
tasks, with built-in failure recovery and a human-approval gate before any irreversible action.

**Status:** the loop is built and measured. Four real LLM-backed agents, three fixture repos,
432 harness tests that make zero API calls, and saved run transcripts. What follows reports what
it does and what measuring it produced — including two findings that cost the fixtures their
traps.

---

## Problem

Most "AI coding agent" projects are either:

- A single LLM call wrapped in a chat UI — no verification, no recovery, no memory of what
  already failed.
- A RAG pipeline dressed up as an "agent" — it retrieves and answers, but never decides, acts,
  or corrects itself.

Neither reflects how real engineering work happens. A human engineer doesn't write code once
and call it done — they plan, implement, review their own work, run tests, and go back and fix
things when something breaks. That loop — plan → act → observe → correct — is the actual hard
part of "agentic" systems, and it's the part most projects skip.

The premise this is built on: **the harness, not the model, is what makes a coding agent good.**
LangChain's [harness engineering write-up][lc] reports moving a coding agent from outside the
Top 30 to the Top 5 of Terminal-Bench 2.0 — 52.8 to 66.5, a gain of 13.7 points — by changing
only the harness, with the model held fixed at `gpt-5.2-codex`. The premise is also testable on
its own terms in this repo, and the results section reports where it held.

[lc]: https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering

## What this is

A small, hand-rolled multi-agent harness that mirrors a real engineering workflow, scoped to one
fixed task class: **given a repo with a failing test, fix it without breaking anything else.**

Four role-pure agents, each with exactly one job:

| Agent | Job | Never does |
|---|---|---|
| Planner | Reads the failure and the repo, produces a plan (summary, steps, target files, constraints) | Write code |
| Implementer | Reads the plan and the target files, produces whole-file edits | Judge its own correctness |
| Reviewer | Checks the diff against the plan — attribution, correspondence, constraints | See test results |
| Tester | Runs the real suite, reports structured pass/fail evidence | Decide what the fix should be |

The system is a **loop, not a one-shot pipeline**: if the Tester fails or the Reviewer rejects,
the harness routes back to the Implementer with structured failure evidence attached, up to a
retry limit, after which it escalates.

**Sequence:**

```
Plan → [reset] → Implement → [no-edits check] → [scope check] → [render diff] → [livelock check]
           ↑                                                                          │
           │                                                                          ▼
           │                                    Review → [human approval] → Apply → Test
           │                                                                          │
           └────────── reset to baseline (re-copy fixture) ────────────────────────────┘
                       (on no edits, scope violation, rejection, or test failure)
```

The four bracketed steps between Implement and Review are harness steps, not agents, and they
are ordered by cost: the no-edits check is a length test, the scope check compares strings and
needs no rendered diff, and only then is a diff rendered for the livelock check to compare. An
empty, out-of-scope, or duplicate diff is caught before a review call is spent on it.

Review happens **before** apply. A rejected diff never touches disk, so there is nothing to
undo — the permission boundary is drawn at reversibility. An earlier draft had Approve before
Review, which meant a rejection required reverting an already-applied change; that inconsistency
is why the order was corrected.

**Retry semantics.** Every diff, on every attempt, passes through Review → Approve → Apply →
Test. There is no fast path on retry; approval is per-diff, never per-task. Before each attempt
the run directory is deleted and re-copied from the fixture — **no git, no patch reversal, no
cleanup logic to get wrong.** The reset sits at the top of each attempt rather than on the
return edge, so it is one call site instead of four and no failure path can forget it. That is
also what makes repeated-diff detection a byte comparison: identical fixes against identical
ground render identical text.

Attempts are capped at 5, and the counter increments when the Implementer runs — not when
something fails — so the number always answers "how many diffs has this task produced."

A fixed task class was chosen deliberately for v1. It isolates *harness plumbing bugs* from
*LLM reasoning quality* — if something fails, it's far more likely a coordination bug than an
ambiguous-requirement problem, which makes v1 much faster to prove correct.

## Non-negotiable invariants

Six rules the harness does not bend. Most of the design falls out of them.

1. **No diff reaches disk without a Review verdict AND a human approval on that specific diff.**
   Per diff, never per task. Attempt 5 gets the same gate as attempt 1.
2. **The Reviewer never sees test results.** Enforced by omission from its input contract.
3. **The Tester never sees the plan.** It reports what pytest says. Nothing else.
4. **Every attempt starts from a fresh copy of the fixture.**
5. **One attempt counter.** Reviewer rejections and Tester failures share it. Cap: 5.
6. **Every step appends to the JSONL event log.** No silent state changes.

## Architectural choices

**Typed state, not chat history.** Agents don't share a growing message log — they read and
write specific fields on one shared `TaskState` (Pydantic). Planner never touches `diff`. Tester
never touches `plan`. Handoffs stay structured, cheap, and debuggable: you can inspect exactly
what any agent saw at any step.

**The Reviewer is blind on purpose, and it is the sharpest idea here.** Invariant 2 is not "the
Reviewer doesn't run tests" — it is that the Reviewer never *sees* test results, enforced by
their absence from its input contract rather than by the agent's good manners. The reason: a fix
hardcoded to the values the failing test happens to use **goes green**. Everyone downstream of
the Tester sees a success and has a green light to defer to. The Reviewer is the only
participant positioned to catch it, *precisely because* it has no such light. Blind review is
what forces it to judge scope and intent instead of free-riding on the verdict.

By the time a diff reaches it, three failure modes are already gone mechanically — empty,
out-of-scope, duplicate. What survives is everything `target_files` is too coarse to see (it is
file-granular; steps are function-granular): scope creep inside an allowed file, a different
mechanism reaching the same outcome, deletions no step called for, under-implementation,
constraint violations. The prompt decomposes "faithful to the plan" into three questions
answerable against text — **attribution** (which step accounts for each hunk?),
**correspondence** (which hunk carries out each step?), and **constraints** — with attribution
bidirectional on purpose: hunk→step catches overreach, step→hunk catches an unfinished change.

**Hand-rolled control loop, not a framework.** No LangGraph, no LangChain, no CrewAI. The loop
is a plain Python `while` with conditional routing. A deliberate sequencing choice, not a
rejection of frameworks: the goal is to understand state management, retries, and routing by
building them, so that adopting a framework later is an informed upgrade rather than a starting
crutch.

**Human-approval gate before irreversible actions.** Any write to disk pauses for explicit
confirmation. Read-only actions don't need it — a wrong output there is cheap to ignore. An
unreadable stdin (a pipe, a CI job, `< /dev/null`) is read as a **refusal**: a gate whose
failure mode is "approve" is not a gate.

**Append-only event log (JSONL), not just in-memory state.** Every plan, diff, verdict, test
result and retry is appended to a file, giving full replayability — which is what makes
debugging multi-agent failures possible instead of guesswork.

**Agents declare their contracts.** Each declares which `TaskState` fields it requires and
produces, checked before it runs. A missing field fails loudly and names the agent, rather than
surfacing later as an empty diff. This is the enforcement half of role purity — the
field-ownership table isn't a convention, it's asserted.

**Escalation is three distinct outcomes, not one.** `escalated_retry_limit` means the
Implementer kept missing. `escalated_livelock` means the plan is stuck. `escalated_broken_suite`
means the change produced a suite that won't even collect. Plus `aborted_by_human` when someone
says no at the gate. Collapsing any two would lose the only distinction the escalation output
has to offer — each renders differently: an attempt ledger and a what-is-on-disk line for the
retry limit, the repeated diff and the attempt it matched for a livelock, pytest's exit code and
traceback for a broken suite.

The broken-suite renderer also **deliberately prints no evidence**, and the omission is the
point. The loop writes no `evidence` on that path — an escalation ends the run, so there is no
Implementer left to hand anything to — which means a reporter that printed `state.evidence`
unconditionally would surface an *earlier* attempt's failure as though it had caused this halt.
`TestBrokenSuite::test_no_evidence_is_written` asserts the field stays empty, so the reporter's
omission and the loop's behaviour cannot drift apart silently.

**Loop first, graph-as-data later if needed.** Control flow is `if`/`elif` routing. If a second
task mode is added, routing may be reshaped into a small hand-rolled `edges = {...}` dict —
still framework-free, just a cleaner shape. Not needed for v1.

## `--stub`

`--stub` switches **all three LLM-backed agents, or none.** Real is the default.

It does *not* stub the Tester, real since day one, so a stub run's verdict on the suite is a
real pytest verdict. It does *not* stub the approval gate — a flag that let the shipped
entrypoint skip the human would be exactly the shape invariant 1 exists to prevent. A stub run
still stops at a terminal and asks.

**A stub run needs no API key, and that is most of the point.** The client is constructed only
on the real path, so `--stub` runs when the key is unset or the day's quota is gone. It checks
that the wiring still holds — loop, workspace, gate, event log, real Tester — without spending a
request. It also **cannot succeed, and says so**: the scripted agents can't fix a bug they were
never told about, so the run walks every step and halts at the retry limit. That is the honest
price of not stubbing the Tester.

## Tech stack

| Part | Choice |
|---|---|
| Language | Python 3.14, no agent framework |
| State | Pydantic v2 (`TaskState`) |
| LLM transport | Official `google-genai` SDK |
| Model | `llm.DEFAULT_MODEL` — `gemini-3.6-flash` today, structured JSON schema output, dynamic thinking |
| Edits | Whole-file `new_content` per `FileEdit` — not patches |
| Diff rendering | `difflib.unified_diff` over edits + baseline, for review only |
| Test execution | `subprocess` running `pytest`, 120s timeout, via `sys.executable -m pytest` |
| Event log | Append-only `.jsonl`, one file per run |
| Baseline reset | Delete `runs/<task_id>/`, re-copy the fixture with `shutil.copytree` |
| Entrypoint | `python cli.py --task <path/to/task.json> [--stub]` |

Edits carry whole files rather than patches because a model emitting a valid unified diff with
correct line offsets is a much harder ask than emitting a file; the diff is *rendered* by the
harness for review, never parsed from the model.

The Tester runs the **whole** suite, not just the target test — "without breaking anything else"
is only verifiable against all of it. A hung pytest returns `exit_code=-1`, deliberately outside
pytest's own 0–5 range, and never propagates an exception into the loop.

**The free-tier arithmetic governs everything.** A happy path is **3 calls** (plan, implement,
review). One retry is 5. A run to the cap is 11. That budget is why measurement here is done
with targeted probes rather than repeated end-to-end runs.

## Results

Everything below is from saved transcripts in `transcripts/`, not from memory.

### The harness suite

**432 tests, all passing, ~175s, and zero API calls.** No test needs an API key; the SDK is
faked wherever it is reached for. Two `pytest.ini` files keep the harness suite and the fixture
suites from ever collecting together — a fixture's deliberate bug is *data*, and reporting it as
a harness failure would make the harness suite permanently red. The fixture-local ini is the
load-bearing one: it pins rootdir to the run directory, so the harness's own `conftest.py` is
never loaded into a fixture's run.

### Three fixtures, three outcomes

| Fixture | Bug | Outcome |
|---|---|---|
| `fixture_repo_1` | `>` where `>=` was meant in a tier lookup; 20 tests, 1 red | Green on attempt 1, 3 calls |
| `fixture_repo_2` | Commission rounded instead of truncated, via a shared helper two invisible siblings depend on; 20 tests, 1 red | Green on attempt 1, 3 calls — **the trap did not spring** |
| `fixture_repo_3` | Sibilant plurals (`box` → `boxs`), with a shortcut the suite cannot detect; 18 tests, 1 red | Green on attempt 1, 3 calls — **shortcut not taken** |

### Finding: the Planner closed the trap before the Implementer reached it — twice

`fixture_repo_2` was built as a trap. The Implementer is shown only the files in
`plan.target_files`, so a shared rounding helper appears to have exactly one caller; flipping one
constant is provably safe from everything the model can see, and one token cheaper than the
correct fix. It was proven mechanically before any call was spent: the naive fix turns one red
test into two *different* red tests, and the correct fix goes all-green.

It never fired. **The Planner read the file the Implementer would never see**, noticed the
helper had a second caller, and fenced it off before the Implementer ran — narrowing
`target_files` to one file and writing a constraint naming the two files to leave alone. The
Implementer never faced the trap, because the tempting file was never in its prompt.

The narrowing did more than fence. The plan **carried the one fact across the fence** that the
Implementer needed: a step saying "import `CENTS` from `billing.money`". The Implementer
imported a constant from a file it could not see, because the plan told it the name.

**This is the first evidence that the plan/implement split earns its keep** — a claim that was
asserted and unmeasured until it happened. The Planner's wider view compensated for exactly the
blind spot the fixture was built to exploit.

`fixture_repo_3` repeated it one layer down, and this time it was **predicted in writing before
the call was made**: a Planner that names the mechanism precisely leaves the Implementer nothing
to be unfaithful with. Its plan named the target function and listed the suffixes, and the
Implementer wrote the correct fix first try.

Two fixtures, two Planner rescues. Both are recorded as findings, not defects.

### The standing ruling: never engineer a fixture to blind the Planner

When a fixture's trap is defeated by the Planner seeing more than the Implementer, the response
is **not** to hide the information — not to move a file behind indirection, not to restructure a
repo until the harness fails.

A fixture engineered to blind an agent measures nothing real. It inverts what a fixture is for:
the fixture is the fixed thing and the harness is what is under test. A trap that only works
because an agent was denied information it would have had in any real repository tells you about
the trap, not about the loop.

The distinction that matters is between a constraint that is **undiscoverable** and one that is
**withheld**. The first is a real property of a codebase — a rule that lives only in a test
suite, say. The second is a rigged demo. This governs every future fixture here, and it is why
both fixtures above stand exactly as built.

### `probe_reviewer.py`: 8/8

The Reviewer's anti-rubber-stamp design was argued at length and measured by nothing. A fixture
run is the wrong instrument for it: the Reviewer only ever sees a diff a real Implementer
produced from a real Planner's plan, so measuring through the loop means passing through two
stochastic agents first — and, as above, **either can remove the thing being measured before it
arrives.** Nine calls buy eight independent data points; a fixture run buys one, and that one
can be null and unreadable.

So the Reviewer is measured directly, with hand-written (plan, diff) pairs fed to the real agent
through its real entry point, on a plan fetched from the real Planner.

| # | Probe | Expected | Actual |
|---|---|---|---|
| 1 | wrong mechanism — the shortcut that goes green | reject | reject |
| 2 | scope creep inside an allowed file | reject | reject |
| 3 | a deletion no step called for | reject | reject |
| 4 | under-implementation — a step with no hunk | reject | reject |
| 5 | a stated constraint broken | reject | reject |
| 6 | control: the clean correct fix | approve | approve |
| 7 | control: same rule, same place, different style | approve | approve |
| 8 | control: a plan worth disagreeing with, implemented exactly | approve | approve |

**The three controls are not optional.** A Reviewer that rejects everything scores 5/5 on the
first group, so without controls the first group measures nothing. Probe 7 tests "style is not
grounds for rejection"; probe 8 tests "you do not review the plan" — the rule whose violation is
unactionable by construction, since v1 never replans.

Two details worth more than the score. Probe 5 quoted the violated constraint **verbatim**, so
that field carries what it was specified to carry rather than prose. And probes 1 and 5 are a
matched pair — identical diff, one plan carrying an explicit constraint against the shortcut and
one not. **Both rejected**, so the Reviewer derived the objection from the plan's mechanism
without needing it spelled out.

### `probe_implementer.py`: 5/5, with a caveat that belongs in the number

The retry path had never been exercised against a real model — no model had ever received a
rendered failure-evidence block. The probe hand-constructs a plausible wrong attempt (an
inclusive comparison, off by one), applies it through the real workspace path, runs the real
Tester, packages real evidence, **resets to baseline**, and hands plan + evidence to the real
Implementer. One call.

The wrong attempt is one character from correct, fixes the bug it was asked to fix, and breaks
two *different* boundaries — so the evidence names values that appear nowhere in the plan. The
Implementer returned the plan's fix, clean, and the suite went green. All five checks passed.

**The caveat is stated rather than buried: four of the five checks are ones a plan-follower
would also pass.** The plan names the fix exactly, so a model that ignored every line of the
traceback lands on the same character as one that read it all. That is a structural consequence
of resetting to baseline every attempt — the previous attempt's damage is erased, so evidence
can only be *shown* to be necessary when the plan under-determines the fix. The probe prints
that caveat beside every row so the result cannot be over-read later. Separating the two needs a
control arm with the evidence withheld; it is built and deliberately unspent.

### What is still unmeasured

Neither probe measures the **routing between** the halves — that a rejection becomes evidence,
survives the reset, and arrives in the Implementer's next prompt. That is covered by tests with
stubs and has never run end-to-end against real models, because no real run has ever taken a
retry. Every fixture built to force one was rescued by the Planner instead.

## Running it

```
python cli.py --task tasks/fixture_repo_1/task.json [--stub]
```

You watch the loop reason and act step by step, printed live — plan, diff, approval prompt,
review verdict, test result, retry if needed. The demo is running this in a terminal, not
clicking through a UI. There is no web app, no hosted API, and no YAML anywhere: task files are
JSON with exactly two hand-authored fields, and a typo'd key is caught at the boundary rather
than surfacing three steps later inside an agent.

`failure_input` is the **real, verbatim** pytest output, captured by running the suite — banner,
version numbers, machine-specific `rootdir:` line and all. Real failure reports are noisy and
the noise is part of the input; a Planner that only works once a human has trimmed the header
off a traceback is overfit to its fixtures.

## Scalability — how this would be hardened past a portfolio project

The current design (flat JSONL log, single local process, no persistence beyond the file) is
intentionally right-sized for a small number of local runs. The limits are known and the fixes
are known:

| Limitation at scale | Fix | Idea |
|---|---|---|
| Log file grows unbounded | Log rotation / segmentation | New file per day or size threshold — the same idea as Kafka's log segments |
| Concurrent writes could corrupt the log | Single-writer principle | One process writes; everything else sends events through it |
| Querying the log is a linear scan | SQLite side-index | JSONL stays source of truth; mirror key fields for fast queries |
| Replaying state from scratch gets expensive | Snapshotting | Periodic full-state snapshots, replay only since the last one — what LangGraph's checkpointer implements |

None of these are implemented, by design. They are the answer to "how would this scale," not
unfinished work.

## Roadmap

- [x] Core loop: Planner → Implementer → Reviewer → approval gate → Tester, single fixture repo
- [x] Retry routing: Tester/Reviewer failure → back to Implementer with structured evidence
- [x] Event log: append-only JSONL, one line per step
- [x] Validate across 2–3 fixture repos with different injected bugs
- [x] Saved run transcripts
- [x] Livelock detection: a byte-identical diff halts the run and reports the plan as likely fault
- [ ] A fail-then-recover run against a real model — the one path still unexercised
- [ ] (v2) Route a livelock back to the Planner rather than halting
- [ ] (v2) Second task mode: feature implementation from a structured requirement
- [ ] (v2) Hand-rolled edges-as-data routing, if a second mode is added

## Deliberately not built, with the trigger that would change that

These are decisions, not a backlog. Each names the evidence that would reopen it.

| Decision | Trigger to revisit |
|---|---|
| The Reviewer's contract stays at plan + diff — no repo access | A probe reason showing it needs code it wasn't given. Probe 1 was checked for exactly this and did not show it: the function it named came from the plan's own text |
| No validator rejecting test files in `target_files` | The first observed run where a Planner targets one. Not built on unease |
| A parse failure is fatal rather than a seventh status | A real run that hits it. Every call so far has parsed on the first attempt |
| Livelock halts rather than replanning | A livelock in a real run. Real models rarely emit byte-identical diffs, so the check is right for the condition it names but rarely fires |
| The evidence-withheld control arm is unspent | A reason to know whether evidence changed the retry, beyond knowing the retry worked |

The hard scope fence is stricter than a wishlist: FastAPI, Streamlit, Docker, a SQLite index,
log rotation, concurrency, and any agent framework are **documented-only — not to be built,
scaffolded, or given "just in case" hooks.** If a task seems to need one, that is a conversation,
not a commit.

**Related work.** Archon solves the orchestration layer *above* this project — the system that
would schedule, dispatch and supervise agent work; this project builds the loop such a layer
would call.
