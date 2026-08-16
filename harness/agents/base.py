"""The Agent ABC and the contract table.

This module is the enforcement half of role purity. CLAUDE.md's agent-contract
table says which fields each agent may read and which one it writes; this file
turns that table into something the interpreter checks, so a prompt that drifts
cannot quietly widen an agent's inputs.

Three decisions shape it.

**The contracts live here, in one table, not on the subclasses.** Four entries
side by side is how you check role purity -- you read the whole thing at once and
see that `test_result` appears in exactly one `produces` and no `requires`. A
`requires` tuple on each subclass would be the same information scattered across
four files, and each copy would be free to drift from CLAUDE.md.

**`_run` receives only its contracted fields, never the whole state.** This is
what makes the "must never read" column structural instead of documentary. The
Reviewer's `_run(*, plan, diff)` cannot reach `test_result` because it was never
handed it, so invariant 2 -- blind review -- holds by construction rather than by
the Reviewer's good manners. It also makes the contract self-checking: if the
table and a subclass signature disagree, `run` says so on the first call.

**The base class writes the produced field; subclasses only return a value.**
`requires` and `produces` are then enforced symmetrically, and no agent is in a
position to write a field it does not own.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from harness.events import EventLog
from harness.state import TaskState


class AgentContractError(Exception):
    """A contract was broken: a missing input, no output, or a bad signature.

    Deliberately not an `AssertionError` and deliberately not a bare `assert`
    statement -- `python -O` strips those, and this is an invariant rather than a
    debug aid.

    It is fatal and nothing catches it. A contract violation means the control
    loop routed to an agent whose inputs are not ready, which is a harness bug,
    not a task outcome. `Status` has no value for it on purpose: the run does not
    end in a state worth recording, it ends in a stack trace worth reading.
    """

    def __init__(self, agent: str, detail: str, *, missing: tuple[str, ...] = ()) -> None:
        self.agent = agent
        self.missing = missing
        super().__init__(f"{agent}: {detail}")


@dataclass(frozen=True)
class Contract:
    """What one agent may read and what it must write.

    `produces` is a single field name, not a tuple. Every agent in CLAUDE.md's
    table writes exactly one field, and a tuple here would invite an agent that
    writes two -- which is the ownership table's whole objection.

    `optional` covers the Implementer's "`plan` (+ `evidence` on retry)": passed
    through to `_run` whether or not it is set, but never asserted. That keeps the
    subclass signature stable across the first attempt and the retries, so `_run`
    does not need a different shape depending on where in the loop it was called.
    """

    requires: tuple[str, ...]
    produces: str
    optional: tuple[str, ...] = field(default=())


# The table from CLAUDE.md, "Agent contracts". The "must never read" column has
# no entry here because it needs none: an agent reads what it is passed, and it
# is passed `requires + optional`. Absence is the enforcement.
CONTRACTS: dict[str, Contract] = {
    "planner": Contract(
        requires=("task_description", "failure_input"),
        produces="plan",
    ),
    "implementer": Contract(
        requires=("plan",),
        optional=("evidence",),
        produces="edits",
    ),
    "reviewer": Contract(
        requires=("plan", "diff"),
        produces="review",
    ),
    "tester": Contract(
        requires=("repo_path",),
        produces="test_result",
    ),
}


class Agent(ABC):
    """Base for the four agents. Subclasses set `name` and implement `_run`.

    `run` is the template: check the contract, call `_run` with exactly the
    contracted fields, and write the result onto a new state. Subclasses never
    touch `TaskState` -- they take values and return a value.
    """

    #: Key into CONTRACTS. The only class-level declaration a subclass makes.
    name: ClassVar[str]

    def __init__(self, event_log: EventLog) -> None:
        if getattr(self, "name", None) not in CONTRACTS:
            raise AgentContractError(
                getattr(self, "name", type(self).__name__),
                f"has no entry in CONTRACTS; known agents are {sorted(CONTRACTS)}",
            )
        self.event_log = event_log
        # The attempt the current `run` belongs to, so `self.log` can stamp
        # domain events without every subclass threading it through. Set at the
        # top of `run`; 0 before the first one. Mutable per-instance state is
        # safe here only because v1 is single-threaded and one agent runs at a
        # time -- concurrency is out of scope by CLAUDE.md's scope fence.
        self._attempt = 0

    @property
    def contract(self) -> Contract:
        return CONTRACTS[self.name]

    def run(self, state: TaskState) -> TaskState:
        """Check the contract, run the agent, return a new state.

        Never mutates `state`. The control loop owns state transitions, and an
        agent mutating in place would make any before/after pair in the event log
        a record of the same object twice.
        """
        contract = self.contract
        self._attempt = state.attempt_count

        inputs = self._collect(state, contract)
        self._check_signature(inputs)

        value = self._run(**inputs)
        if value is None:
            self._violation(
                f"produced None for {contract.produces!r}; an agent that ran and "
                f"produced nothing has broken its contract as surely as one that "
                f"was called without its inputs",
                missing=(contract.produces,),
            )

        self.log(event="agent_produced", payload={contract.produces: value})

        # model_validate, not model_copy(update=...): `model_copy` does not
        # validate, so a subclass returning the wrong type would land it in state
        # unchecked and surface somewhere much later. Revalidating the whole
        # state costs one dump-and-parse per agent step and buys the guarantee
        # that anything in `TaskState` has been through `TaskState`'s own rules.
        return TaskState.model_validate(state.model_dump() | {contract.produces: value})

    def log(self, *, event: str, payload: dict[str, Any]) -> None:
        """Append a domain event for this agent, stamped with the current attempt."""
        self.event_log.append(
            attempt=self._attempt,
            agent=self.name,
            event=event,
            payload=payload,
        )

    @abstractmethod
    def _run(self, **inputs: Any) -> Any:
        """Do the work. Subclasses declare explicit keyword-only parameters.

        Those parameters must match `requires + optional` exactly -- that
        agreement is what `_check_signature` verifies, and writing them out is
        how a reader sees an agent's inputs without opening the contract table.
        """

    # -- contract checking ---------------------------------------------------

    def _collect(self, state: TaskState, contract: Contract) -> dict[str, Any]:
        """The contracted fields, asserting the required ones are present.

        All missing fields are reported together. Naming one, being fixed, and
        then tripping on the next is two round trips for information that was
        available the first time.
        """
        missing = tuple(name for name in contract.requires if getattr(state, name) is None)
        if missing:
            self._violation(
                f"requires {list(contract.requires)}; missing: {', '.join(missing)}",
                missing=missing,
            )

        inputs = {name: getattr(state, name) for name in contract.requires}
        # Optional fields are passed even when None, so the signature does not
        # change shape between a first attempt and a retry.
        inputs.update({name: getattr(state, name) for name in contract.optional})
        return inputs

    def _check_signature(self, inputs: dict[str, Any]) -> None:
        """Verify `_run` accepts exactly the contracted fields, before calling it.

        `Signature.bind` rather than catching `TypeError` around the call: a
        `TypeError` raised *inside* `_run` is a real bug in the agent, and
        reporting it as a contract mismatch would send the reader to the wrong
        file. Binding fails without executing anything, so the two stay distinct.
        """
        try:
            inspect.signature(self._run).bind(**inputs)
        except TypeError as exc:
            self._violation(
                f"_run{inspect.signature(self._run)} does not match its contract, "
                f"which supplies {sorted(inputs)}: {exc}"
            )

    def _violation(self, detail: str, *, missing: tuple[str, ...] = ()) -> None:
        """Log the violation, then raise it. Never returns."""
        self.event_log.append(
            attempt=self._attempt,
            agent=self.name,
            event="contract_violation",
            payload={"detail": detail, "missing": list(missing)},
        )
        raise AgentContractError(self.name, detail, missing=missing)
