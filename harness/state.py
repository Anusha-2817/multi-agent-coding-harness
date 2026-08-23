"""Typed state for the harness.

Every agent reads and writes fields on `TaskState`. There is no shared chat log;
this module is the entire interface between the four agents. See CLAUDE.md,
"Field ownership (TaskState)", for who may write what.

Two conventions run through this file:

1. `None` means "no agent has produced this yet." The agent base class asserts
   that its required fields are present and non-None before calling the LLM, so
   `None` is load-bearing, not decorative. Fields the harness owns and that are
   meaningfully empty at init (`attempt_count`, `previous_diffs`, `status`) get a
   real default instead. No field is both.

2. Paths are `str`, never `pathlib.Path`. The event log inlines full state as
   JSON and must round-trip on its own; `Path` needs a custom serializer and
   renders inconsistently across platforms (on Windows the backslashes land in
   the JSONL escaped). Keeping paths as `str` means `model_dump_json()` works
   with no extra config. Do not "improve" this to `Path`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# CLAUDE.md, "Failure evidence types": TesterFailure.stdout_tail is capped at
# 2000 characters. Enforced by truncation, not by rejection -- a long pytest
# stdout is normal input, not a validation error.
STDOUT_TAIL_LIMIT = 2000


class _Model(BaseModel):
    """Base for every model in this file.

    `extra="forbid"` is the point of it. A typo'd field name in the control loop
    silently doing nothing is exactly the failure this typed-state design exists
    to prevent, and these models are also parsed from LLM output, where an
    unexpected key means the model misunderstood its contract.
    """

    model_config = ConfigDict(extra="forbid")


class Status(StrEnum):
    """Run status. CLAUDE.md, "Status and attempt counting"."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    ESCALATED_RETRY_LIMIT = "escalated_retry_limit"
    ESCALATED_LIVELOCK = "escalated_livelock"
    # A suite that will not collect is not fixable by another diff: there is no
    # failing test to aim at, so a retry would burn an attempt producing a change
    # nobody can evaluate. Distinct from the other two escalations because the
    # fault is neither the plan (livelock) nor exhaustion (retry limit) -- the
    # Implementer emitted something that does not parse.
    ESCALATED_BROKEN_SUITE = "escalated_broken_suite"
    ABORTED_BY_HUMAN = "aborted_by_human"


class Plan(_Model):
    """Produced by the Planner. Read by the Implementer and the Reviewer.

    `target_files` is load-bearing: an edit to a path outside it is a mechanical
    scope violation the harness can detect before the Reviewer runs. The Reviewer
    still judges faithfulness and minimality -- it just isn't the only line of
    defence on scope.
    """

    summary: str
    steps: list[str]
    target_files: list[str]
    constraints: list[str]

    @field_validator("target_files")
    @classmethod
    def _require_at_least_one_target(cls, value: list[str]) -> list[str]:
        """An empty `target_files` is a reachable, silent disaster. Reject it here.

        A `Plan` with `target_files == []` is structurally valid and semantically
        catastrophic: it puts *every* possible edit outside scope, so the loop
        burns all five attempts on the scope check and halts with
        `escalated_retry_limit` while the log reads as though the Implementer
        kept going outside its plan. That is exactly the plumbing-versus-model
        ambiguity Phase 3 exists to remove -- the model would be blamed for a
        plan that made success unreachable.

        Rejecting at the boundary also earns its keep in the prompt: this is an
        LLM-produced model, so the failure arrives as a `ValidationError` naming
        this field, and the LLM client's repair turn hands that message straight
        back to the Planner.
        """
        if not value:
            raise ValueError(
                "target_files must name at least one file; a plan that targets "
                "nothing puts every possible edit outside its own scope"
            )
        return value


class FileEdit(_Model):
    """Full file replacement, not a patch. CLAUDE.md, "Edits, not patches".

    Models are unreliable at emitting valid unified diffs, so the Implementer
    emits whole files and the harness renders the diff with `difflib`.
    """

    path: str
    new_content: str


class ReviewVerdict(_Model):
    """Produced by the Reviewer, which never sees test results.

    `violated_constraints` is required with no default. An approving Reviewer has
    to say `[]` explicitly rather than have Pydantic say it on its behalf -- this
    is LLM output, and a silent default would hide a model that omitted the field.

    **`reason` is declared before `approved`, and the order is the point.** This
    model is handed to the API as `response_json_schema`, and property order in
    the schema is the order the model emits its fields in. Verdict-first would
    have it commit to yes or no and then write a justification for a decision
    already made; reason-first makes it walk the diff against the plan and reach
    the verdict at the end. That is the cheapest guard there is against a
    reviewer that approves everything, and it costs nothing: no code reads these
    fields by position, and `extra="forbid"` and validation are unaffected.
    """

    reason: str
    approved: bool
    violated_constraints: list[str]


class TestResult(_Model):
    """Produced by the Tester, which never sees the plan or the diff.

    Deliberately a superset of what `TesterFailure` needs, because the harness --
    not the Tester -- derives the evidence from this. `stdout` is kept whole here
    and truncated only at the evidence boundary.
    """

    passed: bool
    failed_tests: list[str]  # pytest nodeids, e.g. "tests/test_x.py::test_y"
    # False only when a per-test outcome set was expected and could not be read:
    # a collection error, an internal crash. Without it, `failed_tests == []`
    # would conflate "nothing failed" with "something failed and we could not
    # tell which" -- the same collapse `edits` avoids by being Optional rather
    # than defaulting to []. Required, with no default: a default of True would
    # silently claim a summary was read.
    summary_parsed: bool
    traceback: str  # empty string when passed
    stdout: str
    exit_code: int


class ReviewerRejection(_Model):
    """Evidence written by the harness when the Reviewer rejects a diff.

    No `suggested_fix`, deliberately. The Reviewer says what is wrong; the
    Implementer decides what to do about it. Otherwise the Reviewer ends up
    grading its own homework on the next attempt.
    """

    kind: Literal["reviewer_rejection"] = "reviewer_rejection"
    reason: str
    violated_constraints: list[str]


class TesterFailure(_Model):
    """Evidence written by the harness when the test run fails.

    No parsed `expected_vs_actual`, deliberately -- the traceback already
    contains it, and parsing pytest output is brittle.
    """

    kind: Literal["tester_failure"] = "tester_failure"
    failed_tests: list[str]
    traceback: str
    stdout_tail: str

    @field_validator("stdout_tail")
    @classmethod
    def _cap_stdout_tail(cls, value: str) -> str:
        """Keep the last STDOUT_TAIL_LIMIT characters. The tail is the useful end."""
        return value[-STDOUT_TAIL_LIMIT:]


# The `kind` literal exists so this union round-trips through the JSONL event log
# without ambiguity. CLAUDE.md, "Failure evidence types".
Evidence = Annotated[ReviewerRejection | TesterFailure, Field(discriminator="kind")]


def make_task_id(fixture_dir_name: str) -> str:
    """Build a task_id: `<fixture_dir_name>_<UTC timestamp>`.

    A task_id identifies a *run*, not a task -- it is both the `runs/` directory
    name and the log filename, so two runs against the same fixture must differ.
    Generated by the harness; `task.json` does not contain one.

    The timestamp is compact rather than full ISO 8601 because it becomes a path
    segment, and ISO's colons are illegal in Windows filenames.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{fixture_dir_name}_{stamp}"


class TaskState(_Model):
    """The single object passed between agents.

    Field groups, per CLAUDE.md "Field ownership (TaskState)":

    A. Required at construction -- from the harness and `task.json`. A malformed
       task file fails here, at the boundary, rather than several steps into the
       run when an agent contract assertion trips.
    B. `None` until produced -- one field per producing agent. `None` is what the
       requires/produces assertion checks.
    C. Harness bookkeeping with a real default -- empty is a true value for these,
       so `None` would buy nothing and force null-checks in the control loop.
    """

    # -- Group A: required, no default. Written by the harness at init. ------
    task_id: str
    repo_path: str  # the working copy, runs/<task_id>/ -- never the fixture
    task_description: str
    failure_input: str

    # -- Group B: None until the owning step produces it. --------------------
    plan: Plan | None = None
    # Optional rather than defaulting to []: an Implementer that returned nothing
    # is a real and interesting failure, and `[]` would make it indistinguishable
    # from an Implementer that never ran.
    edits: list[FileEdit] | None = None
    diff: str | None = None  # rendered by the harness from `edits` + baseline
    review: ReviewVerdict | None = None
    test_result: TestResult | None = None
    # The single most recent failure, not a history. The harness overwrites it;
    # it never appends.
    evidence: Evidence | None = None

    # -- Group C: harness bookkeeping, meaningfully empty at init. -----------
    # Increments when the Implementer runs -- not when something fails -- so it
    # always answers "how many diffs has this task produced." The cap lives in
    # loop.py, not here.
    attempt_count: int = 0
    previous_diffs: list[str] = Field(default_factory=list)
    status: Status = Status.RUNNING
