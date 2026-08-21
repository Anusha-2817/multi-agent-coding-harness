"""The Planner: one failing test in, one `Plan` out. Runs exactly once per task.

It reads `repo_path`, `task_description`, and `failure_input`, and nothing else.
No diff, no test result -- by the time anything downstream has produced either,
this agent's work is over. v1 never replans.

`repo_path` is in its contract because `target_files` is mechanically enforced
and nothing else in its inputs names the file holding the bug: `failure_input` is
a pytest traceback that stops at the failing *test*, and `fixture_repo_1`'s never
mentions `pricing/discounts.py` at all. See the note above `CONTRACTS` in
`base.py`.
"""

from __future__ import annotations

from harness.agents.base import Agent
from harness.events import EventLog
from harness.llm import LLMClient
from harness.state import Plan
from harness.workspace import list_repo_files, read_repo_file

# The plan is four short lists. Thinking shares this budget, which is what the
# headroom is for.
MAX_TOKENS = 8000

# A source file longer than this is listed rather than inlined. Fixture repos are
# 5-10 small files by design, so this should never fire -- it is here so that if
# one day it does, the prompt degrades to a listing instead of silently becoming
# enormous.
MAX_INLINED_CHARS = 40_000

SYSTEM_PROMPT = """\
You plan the fix for a single failing test in a small Python repository.

The repository is in a known-good state apart from one deliberate bug. Exactly
one test fails because of it. Your job is to write the plan that another agent
will implement — you do not write code yourself, and you do not run anything.

You return a Plan with four fields, each of which is used mechanically further
down the pipeline. What they are for:

summary
    One or two sentences: what is wrong, and what the fix is. This is the only
    field a human reads first.

steps
    The change, described so an implementer can carry it out without guessing.
    Name functions and describe the edit. Do not write the code — a separate
    agent does that, and it can see the files.

target_files
    Every file that needs editing, and nothing else. This is enforced: the
    harness rejects an edit to any path outside this list before a reviewer ever
    sees it, and the implementer is shown only these files. A path missing here
    is a fix that cannot be made; a path here that does not need changing invites
    an edit that should not happen. Repo-relative, forward slashes, e.g.
    "pricing/discounts.py". At least one path is required.

constraints
    What must remain true of the change. A separate reviewer checks the diff
    against this list without seeing any test results, so write constraints that
    can be checked by reading a diff — "tier_for keeps its signature and return
    type", "no file other than discounts.py changes", "no test file is edited" —
    rather than aspirations like "keep it clean".

Two things worth knowing about the input.

The failing-test output is raw pytest output, exactly as pytest emitted it. Its
"rootdir:" line points at wherever the suite was captured and will not match
where the code runs now; ignore it. The paths inside the traceback are
repo-relative and are correct.

Prefer the smallest change that fixes the failing test without altering what any
other input produces. The suite has passing tests that must stay passing, so a
fix that changes behaviour at other values is not a fix.\
"""


def _is_test_file(path: str) -> bool:
    """Test files are shown by path only, never by contents.

    Two reasons. The failing test's body is already in `failure_input`, so
    inlining it duplicates the one test that matters. And the tests are the
    specification being satisfied — putting their source in front of a model
    asked to turn a red test green is an invitation to edit the assertion, which
    is the one fix that is never the right one.
    """
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or name.startswith("test_") or name == "conftest.py"


def render_repo(repo_path: str) -> str:
    """The repository as prompt text: source inlined, everything else listed.

    Sorted (via `list_repo_files`), so two runs against the same fixture build
    byte-identical prompt text. That is what lets the API cache the prefix
    instead of re-reading it on every call.
    """
    inlined: list[str] = []
    listed: list[str] = []

    for path in list_repo_files(repo_path):
        if not path.endswith(".py") or _is_test_file(path):
            listed.append(path)
            continue

        contents = read_repo_file(repo_path, path)
        if len(contents) > MAX_INLINED_CHARS:
            listed.append(f"{path} ({len(contents)} characters, not shown)")
            continue

        inlined.append(f'<file path="{path}">\n{contents}</file>')

    sections = ["\n".join(inlined)] if inlined else []
    if listed:
        sections.append("<other_files>\n" + "\n".join(listed) + "\n</other_files>")
    return "\n".join(sections)


def render_user_message(*, repo_path: str, task_description: str, failure_input: str) -> str:
    """The contracted fields as one prompt.

    Tagged sections rather than markdown headings or `---` rules, because
    `failure_input` is verbatim pytest output full of `====` bars, `____`
    underlines and its own indentation. Any lightweight delimiter would be
    ambiguous against it, and it is not this agent's place to clean it up --
    CLAUDE.md is explicit that the noise is part of the input.
    """
    return (
        f"<task_description>\n{task_description}\n</task_description>\n\n"
        f"<failing_test_output>\n{failure_input}\n</failing_test_output>\n\n"
        f"<repository>\n{render_repo(repo_path)}\n</repository>"
    )


class Planner(Agent):
    """Produces the `Plan`. Its contract lives in `CONTRACTS["planner"]`.

    `LLMClient` is a constructor dependency for the same reason the `EventLog`
    is: collaborators go in `__init__` and the public `run(state)` signature
    stays uniform across all four agents.
    """

    name = "planner"

    def __init__(self, event_log: EventLog, client: LLMClient) -> None:
        super().__init__(event_log)
        self.client = client

    def _run(self, *, repo_path: str, task_description: str, failure_input: str) -> Plan:
        user = render_user_message(
            repo_path=repo_path,
            task_description=task_description,
            failure_input=failure_input,
        )

        self.log(
            event="llm_request",
            payload={"system_chars": len(SYSTEM_PROMPT), "user_chars": len(user)},
        )

        result = self.client.complete_structured(
            system=SYSTEM_PROMPT,
            user=user,
            schema=Plan,
            max_tokens=MAX_TOKENS,
        )

        # The call's cost and shape, not the plan. `agent_produced` from the base
        # class already carries the plan, and re-logging it would put the same
        # object in the log twice.
        self.log(event="llm_response", payload=result.log_payload())
        return result.value
