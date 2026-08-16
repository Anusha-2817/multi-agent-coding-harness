"""Scripted stand-ins for the three LLM-backed agents.

Phases 1-2 make zero API calls, so the control loop is built and tested against
these. They exist to make the loop's own behaviour -- routing, retry, livelock,
escalation -- observable without a model in the way.

The governing rule is that **a stub is scripted, not smart**. Each one returns
the next entry from a list the caller handed it, indexed by call count, and
decides nothing. A `StubImplementer` that read `evidence` and "fixed itself" on
the third attempt would turn every loop test into a test of the stub's cleverness
instead of the loop's routing -- the assertion would still pass if the loop had
routed nothing back at all.

Two capabilities go beyond returning a value, and both are there to make loop
tests able to assert something:

- **They record what they were handed** (`seen`). "Retry with evidence" is
  otherwise untestable: you can see that a second attempt happened, but not that
  the failure was carried into it.
- **They raise when the script runs out.** A loop that ran six times against a
  five-entry script must fail loudly. Repeating the last entry would let a
  runaway loop look like a passing test.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harness.agents.base import Agent
from harness.events import EventLog
from harness.state import Evidence, FileEdit, Plan, ReviewVerdict


class ScriptExhausted(RuntimeError):
    """A stub was called more times than its script has entries.

    Not an `AgentContractError`: nothing about the agent contract was broken.
    This is the test's script running out, which usually means the loop ran more
    attempts than the test expected -- exactly the thing worth failing on.
    """


class Script:
    """A list of return values, handed out one per call.

    Held by composition rather than mixed into `Agent`, so the stubs inherit one
    thing and the scripting has no opinion about contracts.
    """

    def __init__(self, agent_name: str, entries: Sequence[Any]) -> None:
        self.agent_name = agent_name
        self.entries = list(entries)
        self.calls = 0

    def next(self) -> Any:
        if self.calls >= len(self.entries):
            raise ScriptExhausted(
                f"{self.agent_name} script has {len(self.entries)} "
                f"entr{'y' if len(self.entries) == 1 else 'ies'}; "
                f"call {self.calls + 1} has nothing to return"
            )
        value = self.entries[self.calls]
        self.calls += 1
        return value


class _ScriptedAgent(Agent):
    """Shared plumbing: hold a script, record inputs, log which entry was used."""

    def __init__(self, event_log: EventLog, entries: Sequence[Any]) -> None:
        super().__init__(event_log)
        self.script = Script(self.name, entries)
        #: One entry per call, holding exactly the kwargs `_run` received. Loop
        #: tests assert against this to show what the harness routed back.
        self.seen: list[dict[str, Any]] = []

    def _scripted(self, **inputs: Any) -> Any:
        self.seen.append(inputs)
        self.log(
            event="stub_scripted",
            payload={"call": self.script.calls + 1, "entries": len(self.script.entries)},
        )
        return self.script.next()


class StubPlanner(_ScriptedAgent):
    """Returns pre-built `Plan`s.

    Takes a sequence even though v1 plans exactly once -- routing rejections back
    to the Planner is a v2 item behind the scope fence. A one-entry script is
    therefore also an assertion: if the loop ever plans twice, it raises.
    """

    name = "planner"

    def __init__(self, event_log: EventLog, plans: Sequence[Plan]) -> None:
        super().__init__(event_log, plans)

    def _run(self, *, task_description: str, failure_input: str) -> Plan:
        return self._scripted(task_description=task_description, failure_input=failure_input)


class StubImplementer(_ScriptedAgent):
    """Returns pre-built edit lists, one per attempt.

    The workhorse of the loop tests. Note what it does *not* do: it does not
    decide whether an attempt succeeds. The real Tester does that, by running
    real pytest against whatever these edits produced -- which is why "fail
    twice, then succeed" is expressed as three edit lists and not as a flag.
    """

    name = "implementer"

    def __init__(self, event_log: EventLog, edit_lists: Sequence[list[FileEdit]]) -> None:
        super().__init__(event_log, edit_lists)

    def _run(self, *, plan: Plan, evidence: Evidence | None) -> list[FileEdit]:
        return self._scripted(plan=plan, evidence=evidence)


class StubReviewer(_ScriptedAgent):
    """Returns pre-built verdicts, one per review.

    Records the `diff` it was shown, which is how a livelock test proves the
    check ran *before* review: the Reviewer was never called a second time.
    """

    name = "reviewer"

    def __init__(self, event_log: EventLog, verdicts: Sequence[ReviewVerdict]) -> None:
        super().__init__(event_log, verdicts)

    def _run(self, *, plan: Plan, diff: str) -> ReviewVerdict:
        return self._scripted(plan=plan, diff=diff)
