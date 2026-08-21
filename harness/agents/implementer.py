"""The Implementer: a plan in, full file contents out. Runs once per attempt.

It reads `plan`, `repo_path`, and -- on a retry -- `evidence`. It never sees a
test result, and it never sees the diff it produced last time: `diff` belongs to
the Reviewer and the gate, `previous_diffs` to the harness.

`repo_path` is in its contract because `FileEdit` is **full file replacement**.
An agent asked to return a file's complete new contents cannot do it for a file
it has never read; the alternative would be asking a model to reconstruct code
from a description, which is not a plumbing problem this project should have.

Two things about the prompt are load-bearing rather than decorative.

**The reset warning.** Every attempt starts from a fresh copy of the fixture
(invariant 4), so on a retry the file contents in the prompt are the *originals*,
not the model's previous output. Without a sentence saying so, a model told "your
change failed" while looking at unmodified code will very reasonably conclude it
is looking at its own edit and make an incremental change on top of a baseline
that never contained its fix. That sentence appears in both evidence renderings.

**Evidence renders differently per kind.** `ReviewerRejection` and
`TesterFailure` describe two different worlds -- one where nothing was applied
and nothing ran, one where the change was applied and the whole suite ran -- and
collapsing them into a generic "that didn't work" would throw away the reason
CLAUDE.md refuses to have a generic `error: str` in the first place.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from harness.agents.base import Agent
from harness.events import EventLog
from harness.llm import LLMClient
from harness.state import Evidence, FileEdit, Plan, ReviewerRejection, TesterFailure
from harness.workspace import read_repo_file

# Whole files, not hunks -- and thinking shares the budget. Generous because a
# truncated response is an `LLMTruncatedError`, not a smaller edit. Staying under
# ~16k also keeps a non-streaming request clear of HTTP timeouts, which is a
# reason to keep fixture files small rather than to reach for streaming.
MAX_TOKENS = 16000


class ImplementerResponse(BaseModel):
    """The wire shape of one Implementer call: `{"edits": [...]}`.

    An envelope exists for one mechanical reason: the API's structured outputs
    needs a JSON *object* at the schema root, and this agent produces
    `list[FileEdit]`, which is an array. `Plan` needs no equivalent because it is
    already an object.

    It lives here and not in `state.py` deliberately. `state.py` is the typed
    contract *between agents*; this is the wire format of a single call, unwrapped
    before it reaches state. `_run` still returns `list[FileEdit]`, exactly as
    `CONTRACTS` says.
    """

    model_config = ConfigDict(extra="forbid")

    edits: list[FileEdit]


SYSTEM_PROMPT = """\
You implement a plan that has already been written for you, against a small
Python repository. You do not design the fix and you do not run anything.

Output contract, which is unusual and matters:

For every file you change, return its **complete new contents** — the entire
file, from its first line to its last, with your change in place. Not a diff, not
a patch, not just the changed function, and never an elision like
"# ... rest unchanged ...". The harness renders the diff itself from what you
return and the original file, so a fragment is read as a file that lost
everything you left out.

If a file needs no change, leave it out of the list entirely.

Rules the harness enforces mechanically, before any reviewer reads your work:

- Only the files listed in the plan's target_files. An edit to any other path is
  rejected without review and costs one of five attempts.
- Paths exactly as the plan spells them, repo-relative with forward slashes.

Everything you were not asked to change, copy through verbatim: imports,
docstrings, comments, blank lines, unrelated functions, the file's existing line
endings, and its trailing newline. The diff shows every line you touch, so
reformatting a file buries the real change in noise and gets the whole thing
rejected.

Make the smallest change that satisfies the plan. Do not fix problems the plan
did not mention, add error handling for cases that cannot happen, or tidy code
you happen to be passing through.\
"""

# One shared sentence, in both evidence renderings. See the module docstring:
# this is the single most misreadable thing about the retry, because the model is
# looking at unmodified files while being told its change failed.
RESET_NOTICE = (
    "The repository has been reset to its original state. The file contents above "
    "are the originals, not your edited versions — your previous edits are gone. "
    "Return the complete new contents again."
)


def render_plan(plan: Plan) -> str:
    """The plan as labelled prose, not as its JSON.

    The field names are already self-describing as labels, so JSON punctuation
    would cost tokens and legibility to convey the same thing.
    """
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan.steps, start=1))
    targets = "\n".join(f"- {path}" for path in plan.target_files)
    constraints = "\n".join(f"- {item}" for item in plan.constraints)

    sections = [f"Summary: {plan.summary}"]
    if steps:
        sections.append(f"Steps:\n{steps}")
    sections.append(f"Target files:\n{targets}")
    if constraints:
        sections.append(f"Constraints:\n{constraints}")

    return "<plan>\n" + "\n\n".join(sections) + "\n</plan>"


def render_current_files(repo_path: str, plan: Plan) -> str:
    """The contents of exactly `plan.target_files` -- not the whole repository.

    Narrower than what the Planner is shown, and deliberately so: this is
    precisely the set of files this agent may edit, which makes "return the
    complete new contents" concrete rather than abstract. A file the plan names
    but that does not exist yet is shown as empty, since a plan may legitimately
    call for a new file.
    """
    blocks = []
    for path in plan.target_files:
        try:
            contents = read_repo_file(repo_path, path)
        except FileNotFoundError:
            contents = ""
        blocks.append(f'<file path="{path}">\n{contents}</file>')

    return "<current_files>\n" + "\n".join(blocks) + "\n</current_files>"


def render_evidence(evidence: Evidence) -> str:
    """The previous failure, rendered according to which kind of failure it was.

    Exhaustive over the discriminated union. The two branches differ in more than
    wording:

    `ReviewerRejection` -- the change was judged (or caught mechanically by the
    scope check) and **never applied**. Nothing ran. The correction is to the
    shape and scope of the change. It also carries the livelock warning, because
    the cheapest wrong response to "that was rejected" is to send it again, and
    a byte-identical diff halts the run outright.

    `TesterFailure` -- the change was reviewed, approved, applied, and the whole
    suite ran. The correction is to the behaviour the change produces, and the
    traceback describes the code as it actually behaved.

    Note what is *not* here: the diff that was rejected. This agent retries blind
    to its own last attempt, which follows from the ownership table. It is fine
    for a scope violation (the offending paths are in `violated_constraints`) and
    largely fine for a test failure (the traceback describes the current
    behaviour); the thin case is a judged rejection, where "this overreaches" is
    hard to act on without seeing what was written. Left as-is for now, and worth
    measuring once Phase 5 builds a fixture that exercises the Reviewer's
    judgment.
    """
    if isinstance(evidence, ReviewerRejection):
        return _render_rejection(evidence)
    if isinstance(evidence, TesterFailure):
        return _render_test_failure(evidence)
    raise TypeError(f"unknown evidence kind: {type(evidence).__name__}")


def _render_rejection(evidence: ReviewerRejection) -> str:
    parts = [
        "Your last change was rejected during review. It was never applied and no "
        "tests were run.",
        f"Reason given:\n{evidence.reason}",
    ]

    # Omitted rather than left empty: the no-edits branch writes `[]`, and a bare
    # heading with nothing under it reads as a truncated prompt.
    if evidence.violated_constraints:
        listed = "\n".join(f"- {item}" for item in evidence.violated_constraints)
        parts.append(f"Constraints it violated:\n{listed}")

    parts.append(RESET_NOTICE)
    parts.append(
        "Produce a different change that satisfies the plan without this problem. "
        "Submitting the same change again halts the run."
    )

    return "<previous_attempt_rejected>\n" + "\n\n".join(parts) + "\n</previous_attempt_rejected>"


def _render_test_failure(evidence: TesterFailure) -> str:
    failed = "\n".join(f"- {nodeid}" for nodeid in evidence.failed_tests)
    parts = [
        "Your last change was applied and the full test suite ran. "
        + (f"These tests failed:\n{failed}" if failed else "The suite did not pass.")
    ]

    if evidence.traceback:
        parts.append(f"Failure detail:\n{evidence.traceback}")
    if evidence.stdout_tail:
        parts.append(f"End of the pytest output:\n{evidence.stdout_tail}")

    parts.append(RESET_NOTICE)
    parts.append(
        "Produce a change that makes these tests pass without breaking the others."
    )

    return (
        "<previous_attempt_failed_tests>\n"
        + "\n\n".join(parts)
        + "\n</previous_attempt_failed_tests>"
    )


def render_user_message(*, repo_path: str, plan: Plan, evidence: Evidence | None) -> str:
    """Plan, then files, then evidence -- and that order is not cosmetic.

    The first two sections are byte-identical on every attempt: v1 never replans,
    and the files are re-read from a freshly reset baseline. Evidence is the only
    part that changes. Stable prefix first, volatile suffix last, so attempts 2
    through 5 can read the shared prefix from the API's cache instead of paying
    for it five times.
    """
    sections = [render_plan(plan), render_current_files(repo_path, plan)]
    if evidence is not None:
        sections.append(render_evidence(evidence))
    return "\n\n".join(sections)


class Implementer(Agent):
    """Produces `edits`. Its contract lives in `CONTRACTS["implementer"]`."""

    name = "implementer"

    def __init__(self, event_log: EventLog, client: LLMClient) -> None:
        super().__init__(event_log)
        self.client = client

    def _run(
        self,
        *,
        repo_path: str,
        plan: Plan,
        evidence: Evidence | None,
    ) -> list[FileEdit]:
        user = render_user_message(repo_path=repo_path, plan=plan, evidence=evidence)

        # `evidence_kind` is the one field here worth reading later: it is how the
        # log shows what the retry was told, separately from what it produced.
        self.log(
            event="llm_request",
            payload={
                "system_chars": len(SYSTEM_PROMPT),
                "user_chars": len(user),
                "evidence_kind": evidence.kind if evidence is not None else None,
            },
        )

        result = self.client.complete_structured(
            system=SYSTEM_PROMPT,
            user=user,
            schema=ImplementerResponse,
            max_tokens=MAX_TOKENS,
        )

        self.log(event="llm_response", payload=result.log_payload())
        return result.value.edits
