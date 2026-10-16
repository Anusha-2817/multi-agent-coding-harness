"""Stage 0 of arm D: can a re-plan fix a refuted plan? Not part of the harness.

`python probe_replanner.py --dry-run` then `python probe_replanner.py`.

**What this is for.** On tokenize-rt every B and C run failed because the plan
prescribed `tok_text = line[scol:ecol]`. Under 3.12+ tokenize, `ecol` for
`FSTRING_MIDDLE` is counted on the *unescaped* text, so that slice is
`tok_text` itself and the fix changes nothing; the upstream fix doubles the
braces and advances `ecol`. Arm D (C + one re-plan) is only worth building if a
Planner shown "this mechanism was tried and the failure did not move" writes a
different mechanism. This asks exactly that, against the recorded evidence, and
builds nothing of D.

**What the re-planner is shown** is what D's re-planner would be shown at its
trigger, replayed from the log by `replay_trigger` with D's rule: the first
livelock, or the first tested failure byte-identical (failed tests and
traceback) to the previous tested failure, provided an attempt is left. At
that point it gets the Planner's own inputs (`repo_path`, `task_description`,
`failure_input`) plus the refuted plan, the last *tested* diff, and the
`TesterFailure` that diff produced -- never a raw `TestResult`, the same
packaging the Implementer already reads. The repository is the bench's task
copy, rendered by `planner.render_repo`, read-only.

**The prompt lives here until D is approved.** `REPLAN_SYSTEM_PROMPT` and
`render_replan_message` move verbatim into the harness when D is built, and this
script then imports them from there, so the prompt that passed the probe is the
prompt D runs. Until then nothing in `harness/` knows a re-plan exists.

**Grading is a heuristic plus the plans verbatim.** `grade` flags whether the
new plan still prescribes a `[scol:ecol]` slice and whether it names a
brace-doubling or `ecol`-adjusting mechanism; PASS needs the second without the
first. The flags are a first read, not the verdict -- every plan is printed in
full so a human can overrule them. Gate (CLAUDE.md, "Arm D"): >= 6 of 12 PASS
builds D; < 3 stops it; in between is a judgment call.

Like the other probes it lives at the repo root, not in `tests/`: it makes real
calls (`OPENROUTER_API_KEY`, the backend the 3.8 runs used) and the harness
suite makes none. Calls are paced at the driver's interval.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

from cli import DEFAULT_DESCRIPTION
from driver import MIN_CALL_INTERVAL, PacedClient, Pacer
from harness.agents.planner import MAX_TOKENS, SYSTEM_PROMPT, THINKING_LEVEL, render_user_message
from harness.agents.reviewer import render_plan
from harness.agents.tester import tester_failure_from
from harness.events import EventLog
from harness.llm import LLMError
from harness.loop import MAX_ATTEMPTS
from harness.state import Plan, TesterFailure, TestResult

BENCH = Path(r"D:\Agentic_Project1\experiments\bench")
TASK_DIR = BENCH / "tasks" / "tokenize-rt_cb7836b"
#: The four arm-C runs on Gemini 3.8: full-file repeats 0-2, then diff mode.
RUN_IDS = (
    "armC_tokenize-rt_cb7836b_20261016T152222Z_2e564e",
    "armC_tokenize-rt_cb7836b_20261016T155807Z_545a2e",
    "armC_tokenize-rt_cb7836b_20261016T160347Z_d4d575",
    "armC_tokenize-rt_cb7836b_20261016T191958Z_d73bc3",
)
SAMPLES = 3
GATE_BUILD = 6  # of 12: build arm D
GATE_STOP = 3  # below this of 12: do not build arm D
RULE = "=" * 78

REPLAN_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + """

This is a second plan, not a first one. An earlier plan for this same failure
was implemented faithfully, reviewed against that plan, applied, and tested.
You are shown that plan, the diff that carried it out, and the test failure the
diff produced.

Read the failure as evidence about the plan, not about the implementation. When
the failing assertion is unchanged by a faithful implementation, the mechanism
the plan named does not affect the value being asserted on: an assumption in
that plan is false. Find which one, check it against the code and the failing
output, and plan a mechanism that changes the asserted value. Do not resubmit
the earlier mechanism reworded; a plan the evidence already refutes will fail
the same way.

The same four fields, with the same rules, apply as for any plan.\
"""
)


@dataclass(frozen=True)
class Trigger:
    run_id: str
    kind: str  # "livelock" or "stalled_failure"
    at_attempt: int
    plan: Plan
    tried_diff: str
    failure: TesterFailure
    failure_input: str

    @property
    def attempts_left(self) -> int:
        return MAX_ATTEMPTS - self.at_attempt


# -- replaying D's trigger from a recorded run -------------------------------


def read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def replay_trigger(run_id: str, events: list[dict]) -> Trigger | None:
    """Where D would have re-planned in this run, and what it would have been shown.

    None when D would not have re-planned: no trigger, a trigger with no attempt
    left, or a livelock before any diff was tested (nothing refutes the plan yet).
    """
    plan = failure_input = None
    diffs: dict[int, str] = {}
    last: tuple[str, TesterFailure] | None = None  # the last tested diff and its failure

    for event in events:
        name, agent, attempt, payload = event["event"], event["agent"], event["attempt"], event["payload"]
        kind: str | None = None
        if name == "failure_discovery":
            failure_input = payload["output"]
        elif name == "agent_produced" and agent == "planner":
            plan = Plan.model_validate(payload["plan"])
        elif name == "diff_rendered":
            diffs[attempt] = payload["diff"]
        elif name == "livelock_detected":
            kind = "livelock"
        elif name == "agent_produced" and agent == "tester":
            result = TestResult.model_validate(payload["test_result"])
            if result.passed or not result.summary_parsed:
                return None  # green, or a broken suite: D halts exactly as C does
            failure = tester_failure_from(result)
            if last is not None and (last[1].failed_tests, last[1].traceback) == (
                failure.failed_tests, failure.traceback
            ):
                kind = "stalled_failure"
            last = (diffs[attempt], failure)

        if kind is None:
            continue
        if last is None or attempt >= MAX_ATTEMPTS or plan is None or failure_input is None:
            return None
        return Trigger(run_id, kind, attempt, plan, last[0], last[1], failure_input)
    return None


# -- the prompt --------------------------------------------------------------


def render_replan_message(*, repo_path: str, task_description: str, failure_input: str,
                          plan: Plan, tried_diff: str, failure: TesterFailure) -> str:
    """The Planner's own message, then what the refuted plan produced.

    The first part is `planner.render_user_message` unchanged, so the re-planner
    reads the repository and the original failure exactly as the Planner did.
    """
    failed = "\n".join(failure.failed_tests) or "(none named)"
    return (
        render_user_message(repo_path=repo_path, task_description=task_description,
                            failure_input=failure_input)
        + f"\n\n<previous_plan>\n{render_plan(plan)}\n</previous_plan>\n\n"
        f"<diff_that_implemented_it>\n{tried_diff}</diff_that_implemented_it>\n\n"
        f"<test_failure_after_that_diff>\nFailed tests:\n{failed}\n\n"
        f"Traceback:\n{failure.traceback}\n\nOutput tail:\n{failure.stdout_tail}\n"
        f"</test_failure_after_that_diff>"
    )


def message_for(trigger: Trigger, task_description: str) -> str:
    return render_replan_message(
        repo_path=str(TASK_DIR), task_description=task_description,
        failure_input=trigger.failure_input, plan=trigger.plan,
        tried_diff=trigger.tried_diff, failure=trigger.failure,
    )


# -- grading -----------------------------------------------------------------

#: The refuted mechanism, in code (`line[scol:ecol]`) or in words ("slicing the
#: source line from (sline, scol) to (eline, ecol)").
_SLICE = re.compile(r"\[\s*scol\s*:\s*ecol\s*\]|slic\w*[^.]{0,160}\(\s*eline\s*,\s*ecol\s*\)", re.I)
_MECHANISM = (
    re.compile(r"replace\(\s*['\"]\{['\"]\s*,\s*['\"]\{\{['\"]"),  # .replace('{', '{{')
    re.compile(r"replace\(\s*['\"]\}['\"]\s*,\s*['\"]\}\}['\"]"),
    # Verb forms only: "double braces" as a noun phrase describes the *source*,
    # and every refuted plan uses it that way.
    re.compile(r"\bdoubl(e|ing)\s+(each|every|all|the|any|those|these)\b[^.]{0,40}(brace|\{|\})", re.I),
    re.compile(r"(brace|\{|\})s?[^.]{0,60}\b(be|are|is|get|gets)\s+doubled\b", re.I),
    re.compile(r"\bre-?escap\w*\b[^.]{0,60}(brace|\{|\})", re.I),
    re.compile(r"\becol\s*\+=|\b(adjust|advance|increase|correct)\w*\s+`?ecol", re.I),
    re.compile(r"\bcount\(\s*['\"][{}]['\"]\s*\)"),
)


@dataclass(frozen=True)
class Grade:
    prescribes_slice: bool
    names_mechanism: bool

    @property
    def label(self) -> str:
        if self.names_mechanism and not self.prescribes_slice:
            return "PASS"
        return "MIXED" if self.names_mechanism else "FAIL"


def grade(plan: Plan) -> Grade:
    """Heuristic flags over summary and steps. Constraints are not the mechanism."""
    text = "\n".join([plan.summary, *plan.steps])
    return Grade(
        prescribes_slice=bool(_SLICE.search(text)),
        names_mechanism=any(pattern.search(text) for pattern in _MECHANISM),
    )


def gate(passes: int, total: int) -> str:
    scaled_build = GATE_BUILD * total / 12
    scaled_stop = GATE_STOP * total / 12
    if passes >= scaled_build:
        return "BUILD D"
    if passes < scaled_stop:
        return "DO NOT BUILD D"
    return "JUDGMENT CALL: read the plans"


# -- the two modes -----------------------------------------------------------


def load_triggers(log_dir: Path = BENCH / "logs") -> list[Trigger]:
    triggers = []
    for run_id in RUN_IDS:
        trigger = replay_trigger(run_id, read_events(log_dir / f"{run_id}.jsonl"))
        if trigger is None:
            raise SystemExit(f"{run_id}: D would not have re-planned; the probe's inputs are wrong")
        triggers.append(trigger)
    return triggers


def show(triggers: list[Trigger], description: str, full: bool) -> None:
    for trigger in triggers:
        print(f"\n{RULE}\n{trigger.run_id}\ntrigger: {trigger.kind} at attempt "
              f"{trigger.at_attempt}, {trigger.attempts_left} attempt(s) left\n{RULE}")
        print(f"refuted plan grades as: {grade(trigger.plan).label}")
        message = message_for(trigger, description)
        print(f"user message: {len(message)} chars, system prompt: {len(REPLAN_SYSTEM_PROMPT)} chars")
        if full:
            print(message)
        else:
            print(message[message.index("<previous_plan>"):])


def run(triggers: list[Trigger], description: str, client, log: EventLog, samples: int) -> int:
    results: list[tuple[Trigger, int, Plan, Grade]] = []

    for trigger in triggers:
        user = message_for(trigger, description)
        for sample in range(1, samples + 1):
            log.append(attempt=trigger.at_attempt, agent="probe", event="replan_probe_started",
                       payload={"run_id": trigger.run_id, "trigger": trigger.kind, "sample": sample})
            log.append(attempt=trigger.at_attempt, agent="replanner", event="llm_request",
                       payload={"system_chars": len(REPLAN_SYSTEM_PROMPT), "user_chars": len(user)})
            result = client.complete_structured(system=REPLAN_SYSTEM_PROMPT, user=user, schema=Plan,
                                                max_tokens=MAX_TOKENS, thinking_level=THINKING_LEVEL)
            log.append(attempt=trigger.at_attempt, agent="replanner", event="llm_response",
                       payload=result.log_payload())
            plan, verdict = result.value, grade(result.value)
            log.append(attempt=trigger.at_attempt, agent="replanner", event="agent_produced",
                       payload={"plan": plan, "grade": verdict.label})
            results.append((trigger, sample, plan, verdict))
            print(f"  {trigger.run_id[-6:]} sample {sample}: {verdict.label}")

    print(f"\n{RULE}\nRESULTS\n{RULE}")
    header = f"{'run':<8} {'trigger':<16} {'left':<5} {'sample':<7} {'slice':<6} {'mechanism':<10} grade"
    print(header)
    print("-" * len(header))
    for trigger, sample, _, verdict in results:
        print(f"{trigger.run_id[-6:]:<8} {trigger.kind:<16} {trigger.attempts_left:<5} {sample:<7} "
              f"{'yes' if verdict.prescribes_slice else 'no':<6} "
              f"{'yes' if verdict.names_mechanism else 'no':<10} {verdict.label}")

    print(f"\n{RULE}\nPLANS, VERBATIM\n{RULE}")
    for trigger, sample, plan, verdict in results:
        print(f"\n{trigger.run_id[-6:]} sample {sample} [{verdict.label}]")
        print(indent(render_plan(plan), "  "))

    passes = sum(verdict.label == "PASS" for *_, verdict in results)
    print(f"\n{passes}/{len(results)} PASS by heuristic -> {gate(passes, len(results))}")
    print(f"Overrule by hand where the verbatim plans disagree with the flags. Log: {log.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 0 for arm D: re-plan on recorded evidence.")
    parser.add_argument("--dry-run", action="store_true", help="replay triggers, render prompts; no calls")
    parser.add_argument("--full", action="store_true", help="with --dry-run, print whole user messages")
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--min-call-interval", type=float, default=MIN_CALL_INTERVAL)
    args = parser.parse_args(argv)

    triggers = load_triggers()
    description = DEFAULT_DESCRIPTION  # what the driver handed every run
    if args.dry_run:
        show(triggers, description, args.full)
        print(f"\n{len(triggers) * args.samples} call(s) would be made.")
        return 0

    from llm_openrouter import OpenRouterClient

    log = EventLog(f"probe_replanner_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    try:
        client = PacedClient(OpenRouterClient(), Pacer(args.min_call_interval))
        return run(triggers, description, client, log, args.samples)
    except LLMError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
