# Multi-Agent Coding Harness

A framework-free, hand-rolled orchestration system where specialized agents — Planner,
Implementer, Reviewer, Tester — collaborate through typed state handoffs to resolve coding
tasks, with built-in failure recovery and a human-approval gate before any irreversible action.

**Status:** the loop is built and measured. Four real LLM-backed agents, three fixture repos,
628 harness tests that make zero API calls (on branch `diff-edits`), saved run transcripts, and a
first three-arm benchmark on six real upstream bugs (tag `v0.1.0`; see "Benchmark results").
What follows reports what it does and what measuring it produced — including two findings that
cost the fixtures their traps.

---

## Install

Requires Python 3.10 or newer.

From the repo folder:

    pip install .

This adds a `mach` command. Check it with:

    mach --help

## Set your API key

The agents call Gemini, so set a key in the same terminal before a real run.

PowerShell:

    $env:GEMINI_API_KEY = "your-key"

macOS / Linux:

    export GEMINI_API_KEY="your-key"

## Run

Point it at any repository that has a failing pytest suite:

    mach --repo path/to/repo
    mach --repo path/to/repo --description "what you want fixed"

The harness runs the suite once to find the failure, then loops through
Planner, Implementer, Reviewer and Tester. Before any change is applied to
the working copy, you see the diff and approve it with `y` or reject it with
`n`.

To try the loop without an API key or any requests:

    mach --repo path/to/repo --stub

A stub run still asks for approval at the terminal, and it cannot succeed: the
scripted agents cannot fix a bug they were never told about, so it walks every
step and halts at the retry limit.

## Output

Each run writes a working copy under `runs/<task_id>/` and an event log under
`logs/<task_id>.jsonl`, both inside the directory you run `mach` from. Your
original repo is never modified.

## Safety

The target repo's tests run on your machine, in the environment where `mach`
is installed, so the target's own dependencies must be installed there too.
There is no sandbox. Use it only on repositories you trust.

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
| Implementer | Reads the plan and the target files, produces whole-file edits (or search/replace blocks the harness resolves into whole files, with `--edit-format diff`) | Judge its own correctness |
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
| Model | `llm.DEFAULT_MODEL` — `gemini-3.6-flash` on the Google backend (Google routes `gemini-3.5-flash` requests to it); the benchmark uses `google/gemini-3.8-flash` through the OpenRouter backend. Structured JSON schema output |
| Edits | Whole-file `new_content` per `FileEdit` — not patches. On branch `diff-edits`, the benchmark scripts' `--edit-format diff` has the model send search/replace blocks instead, which the harness resolves into the same whole-file edits. The default stays `full` |
| Diff rendering | `difflib.unified_diff` over edits + baseline, for review only |
| Test execution | `subprocess` running `pytest`, 120s timeout, via `sys.executable -m pytest` |
| Event log | Append-only `.jsonl`, one file per run |
| Baseline reset | Delete `runs/<task_id>/`, re-copy the fixture with `shutil.copytree` |
| Entrypoint | `python cli.py --repo <path> [--description "<text>"] [--stub]`, or `--task <path/to/task.json>` |

Edits carry whole files rather than patches because a model emitting a valid unified diff with
correct line offsets is a much harder ask than emitting a file; the diff is *rendered* by the
harness for review, never parsed from the model. Diff mode keeps that rule: the model sends
exact search text and replacement text, never hunk headers or line numbers. A block that
doesn't match exactly once is not applied.

The Tester runs the **whole** suite, not just the target test — "without breaking anything else"
is only verifiable against all of it. A hung pytest returns `exit_code=-1`, deliberately outside
pytest's own 0–5 range, and never propagates an exception into the loop.

**The free-tier arithmetic governs everything.** A happy path is **3 calls** (plan, implement,
review). One retry is 5. A run to the cap is 11. That budget is why measurement here is done
with targeted probes rather than repeated end-to-end runs.

## Results

Everything below is from saved transcripts in `transcripts/`, not from memory.

### The harness suite

**628 tests on branch `diff-edits`, all passing, ~850s, and zero API calls.** No test needs an API key; the SDK is
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
survives the reset, and arrives in the Implementer's next prompt. That routing is covered by
tests with stubs. Real runs have now taken retries: in the benchmark, the full harness (arm C)
needed 2 to 3 attempts on several tasks, and its tokenize-rt runs reached the retry limit, so the
retry path has run against a real model. What no one has yet checked is, attempt by attempt, that
the rejection text appears in the next Implementer prompt. Before the benchmark, every fixture
built to force a retry was rescued by the Planner instead.

## Benchmark results

Three arms, the same model, token limits, attempt cap (5) and Tester:
**A** is a single loop with no Planner and no Reviewer, **B** is the harness without the Reviewer, and **C** is the full harness.
Six real upstream bugs (the eval split). The upstream tests are hidden from scoring and restored over the run before it is scored, so editing a test cannot change a score. Model: `google/gemini-3.8-flash` via OpenRouter.

**Every cell is one run (repeat 0) unless stated.** Each table uses one model. Earlier
`gemini-3.6-flash` results are in separate files and are not comparable with these.

**Versions.** Tag **`v0.1.0`** is the first benchmark, with full-file edits only (Table 1). The
diff-edits work (Tables 2 and 3) is on branch **`diff-edits`**, where `--edit-format` defaults
to `full`.

### Table 1 — full-file edits

Model calls per run; ✅ solved, ❌ not solved.

| Task | A | B | C |
|---|---|---|---|
| humps | ✅ 1 | ✅ 2 | ✅ 7 |
| mccabe | ✅ 1 | ✅ 2 | ✅ 5 |
| cfgv | ✅ 1 | ✅ 2 | ✅ 5 |
| addict | ✅ 1 | ✅ 2 | ✅ 3 |
| shortuuid | ✅ 1 | ✅ 2 | ✅ 5 |
| tokenize-rt | ❌ 5 | ❌ 5 | ❌ 11 |

tokenize-rt was run 3 times per arm, and 0 of the 9 runs solved it. Its row shows repeat 0.

### Table 2 — diff edits

`--edit-format diff` (search/replace blocks), 4 tasks, repeat 0, same model. Model calls per
run.

| Task | A | B | C |
|---|---|---|---|
| humps | ✅ 3 | ✅ 2 | ✅ 3 |
| cfgv | ✅ 1 | ✅ 2 | ✅ 3 |
| shortuuid | ✅ 1 | ✅ 2 | ✅ 3 |
| tokenize-rt | ❌ 5 | ❌ 3 | ❌ 8 |

### Table 3 — total output tokens per run, full → diff

| Task | A | B | C |
|---|---|---|---|
| humps | 2022 → 514 (−75%) | 2285 → 473 (−79%) | 6679 → 551 (−92%) |
| cfgv | 3867 → 70 (−98%) | 4076 → 322 (−92%) | 8176 → 433 (−95%) |
| shortuuid | 1726 → 134 (−92%) | 1923 → 338 (−82%) | 3949 → 461 (−88%) |
| tokenize-rt | 10422 → 1601 (−85%) | 8247 → 1039 (−87%) | 10558 → 1998 (−81%) |

Median change across the 12 cells: about −88%. Edit-apply failures: 0 of 12 runs; repair
calls: 0.

### What it shows

- **Solve rate.** A, B and C solved the same 5 of 6 tasks, and none solved tokenize-rt. On
  these tasks the full harness added calls and no solves.
- **Patch size.** On addict, humps and shortuuid, A's patch was larger than the upstream fix
  (8 vs 2, 11 vs 2 and 7 vs 4 source lines). B and C stayed within 2 lines of upstream. This is an
  observation from one run each.
- **Diff edits** cut output tokens by 75 to 98% in every cell. C solved humps, cfgv and
  shortuuid on attempt 1, where full mode took 3, 2 and 2 attempts. That is consistent with the
  Reviewer's earlier rejections coming from collateral damage in whole-file rewrites.
- **Diff mode did not fix tokenize-rt.** All arms still fail it, and in diff mode B and C both
  ended in livelock.

### Limitations

- One run per cell, except tokenize-rt in full mode (3 per arm).
- One model.
- The easy tasks mostly saturate: every arm solves them, so solve rate cannot separate the arms.
- The diff-edits gate was **provisional**: 4 tasks, repeat 0. `--edit-format` still defaults to
  `full`. The full-split gate has not been run.
- The earlier `gemini-3.6-flash` results live in separate files and are not comparable.
- Cost (`cost_usd`) is not reported yet.

### Arm D (one re-plan): designed, probe-gated, gate failed, not built

**Diagnosis, from the logs alone.** All 8 tokenize-rt B and C runs failed for the same reason:
- The Planner picked the right file, `tokenize_rt.py`, every time.
- Every plan prescribed `tok_text = line[scol:ecol]` for f-string middles. Under Python 3.12+ tokenize, `ecol` is counted on the *unescaped* text, so that slice equals `tok_text` and the fix changes nothing.
- The Implementer followed the plan, the Tester showed the same failing assertion every attempt, and in arm C the Reviewer approved the plan-faithful diffs.

The fault was in the plan, not in the implementation.

**The idea.** Arm D = arm C plus at most one re-plan, triggered by a livelock or by two identical test failures. The re-planner is shown the refuted plan, the diff that implemented it, and the failure it produced.

**The cheap gate first.** Before building anything, `probe_replanner.py` asked the re-plan prompt 12 times (4 recorded C runs × 3 samples), at their replayed trigger points.

| PASS | MIXED | FAIL | TRUNCATED | Gate to build |
|---|---|---|---|---|
| 0 | 2 | 6 | 4 | ≥ 6 of 12 |

**The heuristic grader and a hand read agree that this is far below the gate.** In the hand read:
- At most 1 of 12 plans is plausibly correct, and it is unverified.
- 2 plans would edit the test file.
- 1 is a no-op.
- 4 used the whole 24,000-token budget without producing a plan.

The probe cost about $0.61. **Arm D was not built.** On this task and model, the missing piece looks like model knowledge, not loop structure.

This is one task, one model and one prompt. It does not show that re-planning is useless in general. The design and the full numbers are in `CLAUDE.md`, under "Arm D: one re-plan — not pursued".

### How it is run

Four scripts at the repo root, none of them part of the shipped CLI:
`baseline.py` (arm A), `harness_arms.py` (arms B and C), `scorer.py` (scores one finished
run against the hidden upstream test) and `driver.py` (runs the arms over the task set,
resumable; `--edit-format full|diff`, default `full`, one format per results file). Nine tasks
are defined: small real upstream bug fixes, three for development and six for evaluation.

The arms run model-written code against each task's tests on your machine. The same safety note
applies: trusted repos only, no sandbox yet.

## Running it

The `mach --repo ...` form is under "Run" at the top. It points `--repo` at any directory on disk and the harness runs the suite itself to find the
failing test — no `task.json` needed. `--failure "<verbatim pytest output>"` overrides discovery
under `--repo`. The original form still works, and is what the three built fixtures use:

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
- [x] A fail-then-recover run against a real model (schema, recorded as F8 in `CLAUDE.md`; arm C on humps in the benchmark)
- [x] Three-arm benchmark on six real upstream bugs (tag `v0.1.0`)
- [x] Diff-based edits behind `--edit-format` (branch `diff-edits`). The provisional gate passed; the full-split gate has not been run, so the default stays `full`
- [ ] (v2) Route a livelock back to the Planner rather than halting. Probed as arm D on tokenize-rt: the gate failed at 0/12 and it was not built (see "Arm D" under Benchmark results)
- [ ] (v2) Second task mode: feature implementation from a structured requirement
- [ ] (v2) Hand-rolled edges-as-data routing, if a second mode is added

## Deliberately not built, with the trigger that would change that

These are decisions, not a backlog. Each names the evidence that would reopen it.

| Decision | Trigger to revisit |
|---|---|
| The Reviewer's contract stays at plan + diff — no repo access | A probe reason showing it needs code it wasn't given. Probe 1 was checked for exactly this and did not show it: the function it named came from the plan's own text |
| No validator rejecting test files in `target_files` | The first observed run where a Planner targets one. **This has now happened once** (tokenize-rt, arm C, recorded as F10 in `CLAUDE.md`). The decision is still open |
| A parse failure is fatal rather than a seventh status | A real run that hits it. All 168 logged benchmark calls parsed on the first attempt |
| Livelock halts rather than replanning | A livelock in a real run. **This has now happened**: tokenize-rt ended in livelock in arms B and C, in both full and diff mode. A re-plan arm (D) was designed and probed first: 0 of 12 re-plans fixed the plan, so it was not built and halting stands. Reopen on a different model or a different refuted-plan task |
| The evidence-withheld control arm is unspent | A reason to know whether evidence changed the retry, beyond knowing the retry worked |

The hard scope fence is stricter than a wishlist: FastAPI, Streamlit, Docker, a SQLite index,
log rotation, concurrency, and any agent framework are **documented-only — not to be built,
scaffolded, or given "just in case" hooks.** If a task seems to need one, that is a conversation,
not a commit.

**Related work.** Archon solves the orchestration layer *above* this project — the system that
would schedule, dispatch and supervise agent work; this project builds the loop such a layer
would call.
