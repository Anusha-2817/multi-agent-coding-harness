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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.agents.base import Agent
from harness.events import EventLog
from harness.llm import LLMClient
from harness.state import Evidence, FileEdit, Plan, ReviewerRejection, TesterFailure
from harness.workspace import normalize_path, read_repo_file

# How the model writes its change. `full` is the original contract -- whole
# files -- and stays the default, so every result recorded before `diff` existed
# is still what the default produces. `diff` is search/replace blocks, resolved
# against the baseline into the same `list[FileEdit]` before anything outside
# this module sees them. See CLAUDE.md, "Diff-mode edits".
EDIT_FORMATS = ("full", "diff")

# Whole files, not hunks -- and thinking shares the budget. Generous because a
# truncated response is an `LLMTruncatedError`, not a smaller edit. A ~38k-char
# file is ~9.8k tokens of output, and one retry on it thought for 8,253 tokens
# even at LOW, which overran the old 16,000. The ceiling is bounded above by the
# HTTP timeout (120 s per attempt, DEFAULT_TIMEOUT in llm.py), not by the model's
# 65,535 output limit: logged throughput is ~275-325 tokens/s, so 24,000 is
# ~90 s worst case, while 32,000 would approach the timeout and an SDK retry
# would re-spend the whole call.
MAX_TOKENS = 24000

# Thinking effort. The output is the whole file again, which on a ~38k-character
# file is most of MAX_TOKENS, so thinking left at the default can crowd it out.
# An effort setting, not a token cap: MAX_TOKENS is still the only hard ceiling.
THINKING_LEVEL = "LOW"


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


class SearchReplace(BaseModel):
    """One diff-mode edit block: find `search` in `path`, put `replace` there.

    Wire format only, like `ImplementerResponse`: it never reaches `TaskState`.
    `search` is declared before `replace` so the model commits to the anchor it
    copied before writing the change. Every field is a required string with no
    default, which is what strict structured output accepts.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    search: str
    replace: str


class DiffImplementerResponse(BaseModel):
    """The wire shape of one diff-mode call: `{"edits": [{path, search, replace}]}`.

    Structurally the same schema as `ImplementerResponse` -- one `$defs` entry,
    one `$ref`, `additionalProperties: false` -- so it uses no schema feature the
    full-mode call has not already used against both backends.
    """

    model_config = ConfigDict(extra="forbid")

    edits: list[SearchReplace]


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

# The rules of the diff-mode output format, shared word for word by this agent's
# diff prompt and the baseline's (`baseline.SYSTEM_PROMPT_DIFF`), so the two arms
# are told the same format. Mechanical rules only: what is matched and how. It
# says nothing about which files to edit -- that would be a second change riding
# along with the format.
DIFF_EDIT_RULES = """\
You change files with edit blocks. Each block names a path, a search text and a
replace text. The harness finds the search text in the file and puts the replace
text in its place. Every other line of the file is kept exactly as it is, by the
harness, not by you.

- Copy the search text exactly from the file contents shown to you: the same
  characters, indentation, whitespace and line breaks. It is matched exactly,
  with no fuzzy matching.
- The search text must occur exactly once in its file. If the lines you want to
  change occur more than once, include enough surrounding lines to make the
  search text unique.
- Every search text is matched against the original file, not against the
  result of your other blocks, and two blocks in one file must not overlap.
- Keep each block small: the lines you are changing, plus only as much context
  as uniqueness needs. Never send a whole file as a search text.
- To create a file that does not exist yet, use an empty search text; the replace
  text becomes the whole file.
- If any block cannot be applied, none of them are.

If a file needs no change, send no block for it.\
"""

SYSTEM_PROMPT_DIFF = f"""\
You implement a plan that has already been written for you, against a small
Python repository. You do not design the fix and you do not run anything.

Output contract, which is unusual and matters:

{DIFF_EDIT_RULES}

Rules the harness enforces mechanically, before any reviewer reads your work:

- Only the files listed in the plan's target_files. An edit to any other path is
  rejected without review and costs one of five attempts.
- Paths exactly as the plan spells them, repo-relative with forward slashes.

Inside a block, copy every line you are not changing from the search text to the
replace text unchanged: code, docstrings, comments, blank lines and trailing
whitespace. The diff shows every line you touch, so reformatting buries the real
change in noise and gets the whole thing rejected.

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

# The same sentence for diff mode. Only the last clause differs: "return the
# complete new contents" is the full-mode contract and would be an instruction to
# break this one.
RESET_NOTICE_DIFF = (
    "The repository has been reset to its original state. The file contents above "
    "are the originals, not your edited versions — your previous edits are gone. "
    "Send your edit blocks again, matched against the original contents shown above."
)

_RESET_NOTICES = {"full": RESET_NOTICE, "diff": RESET_NOTICE_DIFF}


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


def render_evidence(evidence: Evidence, edit_format: str = "full") -> str:
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

    `edit_format` changes one sentence, the reset notice; the default keeps the
    full-mode rendering byte-identical to what it was before diff mode existed.
    """
    reset_notice = _RESET_NOTICES[edit_format]
    if isinstance(evidence, ReviewerRejection):
        return _render_rejection(evidence, reset_notice)
    if isinstance(evidence, TesterFailure):
        return _render_test_failure(evidence, reset_notice)
    raise TypeError(f"unknown evidence kind: {type(evidence).__name__}")


def _render_rejection(evidence: ReviewerRejection, reset_notice: str) -> str:
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

    parts.append(reset_notice)
    parts.append(
        "Produce a different change that satisfies the plan without this problem. "
        "Submitting the same change again halts the run."
    )

    return "<previous_attempt_rejected>\n" + "\n\n".join(parts) + "\n</previous_attempt_rejected>"


def _render_test_failure(evidence: TesterFailure, reset_notice: str) -> str:
    failed = "\n".join(f"- {nodeid}" for nodeid in evidence.failed_tests)
    parts = [
        "Your last change was applied and the full test suite ran. "
        + (f"These tests failed:\n{failed}" if failed else "The suite did not pass.")
    ]

    if evidence.traceback:
        parts.append(f"Failure detail:\n{evidence.traceback}")
    if evidence.stdout_tail:
        parts.append(f"End of the pytest output:\n{evidence.stdout_tail}")

    parts.append(reset_notice)
    parts.append(
        "Produce a change that makes these tests pass without breaking the others."
    )

    return (
        "<previous_attempt_failed_tests>\n"
        + "\n\n".join(parts)
        + "\n</previous_attempt_failed_tests>"
    )


def render_user_message(
    *, repo_path: str, plan: Plan, evidence: Evidence | None, edit_format: str = "full"
) -> str:
    """Plan, then files, then evidence -- and that order is not cosmetic.

    The first two sections are byte-identical on every attempt: v1 never replans,
    and the files are re-read from a freshly reset baseline. Evidence is the only
    part that changes. Stable prefix first, volatile suffix last, so attempts 2
    through 5 can read the shared prefix from the API's cache instead of paying
    for it five times.

    The two edit formats share the plan and the files exactly; only the
    evidence's reset notice differs (see `render_evidence`).
    """
    sections = [render_plan(plan), render_current_files(repo_path, plan)]
    if evidence is not None:
        sections.append(render_evidence(evidence, edit_format))
    return "\n\n".join(sections)


# -- diff mode: resolving edit blocks ----------------------------------------
#
# Resolution turns search/replace blocks into full-file `FileEdit`s against the
# baseline on disk. It is pure apart from reading files: nothing is written, and
# it either resolves every block or returns none of them. Everything downstream
# -- the scope check, `render_diff`, livelock, the Reviewer, `apply_edits` -- sees
# only the resolved `FileEdit`s, exactly as in full mode.

#: How long a search text's first line may be when it is echoed back.
SEARCH_ECHO_LIMIT = 120

#: What each failure kind means, worded for the model.
FAILURE_TEXT = {
    "no_match": "the search text was not found in the file",
    "ambiguous": "the search text occurs more than once in the file",
    "overlap": "the block overlaps another block in the same file",
    "empty_search": "the search text is empty but the file already has contents",
    "file_missing": "the file does not exist, so a non-empty search text cannot match",
    "path_escapes_repo": "the path is outside the repository",
    "unreadable": "the file could not be read as UTF-8 text",
}


@dataclass(frozen=True)
class EditFailure:
    """Why one edit block could not be applied. `index` is 1-based, in emitted order."""

    index: int
    path: str
    kind: str
    search: str
    match_count: int | None = None
    hint: str | None = None

    def first_line(self) -> str:
        """The search text's first non-blank line, capped for echoing back."""
        line = next((line.strip() for line in self.search.splitlines() if line.strip()), "")
        if len(line) > SEARCH_ECHO_LIMIT:
            line = line[:SEARCH_ECHO_LIMIT] + "..."
        return line

    def describe(self) -> str:
        """`block N (path): what went wrong`."""
        detail = FAILURE_TEXT[self.kind]
        if self.kind == "ambiguous":
            detail += f" ({self.match_count} times)"
        if self.hint:
            detail += f"; {self.hint}"
        return f"block {self.index} ({self.path}): {detail}"

    def log_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "kind": self.kind,
            "match_count": self.match_count,
            "hint": self.hint,
            "search": self.search,
        }


class EditsNotApplicable(Exception):
    """Diff-mode edit blocks that still did not apply after the one repair call.

    Raised out of the Implementer's `_run` and caught by the control loop (and by
    `baseline.py`), which routes it back as a `ReviewerRejection` and logs
    `edit_apply_failed` -- the same treatment as a scope violation: a mechanical
    catch, no diff produced, one attempt consumed. Not fatal, unlike
    `LLMResponseError`, because a fresh sample from a reset baseline can succeed.
    """

    def __init__(self, failures: list[EditFailure], block_count: int) -> None:
        self.failures = failures
        self.block_count = block_count
        super().__init__(self.reason())

    def reason(self) -> str:
        """The evidence reason. Prompt text: the model reads it on the next attempt.

        Echoes each failing block's first search line (D9 in CLAUDE.md, "Diff-mode
        edits"): without it, "block 2 did not match" names nothing the model can
        recognise, since it retries without seeing its last attempt.
        """
        lines = [
            f"{len(self.failures)} of {self.block_count} edit block(s) could not be "
            "applied to the original files, so none were applied:"
        ]
        for failure in self.failures:
            echo = failure.first_line()
            suffix = f" (search began: {echo!r})" if echo else ""
            lines.append(f"- {failure.describe()}{suffix}")
        return "\n".join(lines)

    def log_payload(self) -> list[dict[str, Any]]:
        return [failure.log_payload() for failure in self.failures]


def resolve_edits(repo_path: str | Path, blocks: list[SearchReplace]) -> list[FileEdit]:
    """Resolve edit blocks against the files in `repo_path` into full-file edits.

    All or nothing: if any block fails, `EditsNotApplicable` carries every
    failure at once and no edit is returned. Each search is matched against the
    *original* file, so the result does not depend on block order. Blocks for one
    file are grouped under the spelling of the first block that named it, which is
    what the scope check will report back. A file whose blocks change nothing is
    dropped, so an all-no-op response resolves to `[]` and takes the loop's
    no-edits branch.
    """
    groups: dict[str, list[tuple[int, SearchReplace]]] = {}
    for index, block in enumerate(blocks, start=1):
        groups.setdefault(normalize_path(block.path), []).append((index, block))

    root = Path(repo_path).resolve()
    failures: list[EditFailure] = []
    resolved: list[FileEdit] = []

    for normalized, members in groups.items():
        outcome = _resolve_file(root, normalized, members)
        if isinstance(outcome, list):
            failures.extend(outcome)
        elif outcome is not None:
            resolved.append(FileEdit(path=members[0][1].path, new_content=outcome))

    if failures:
        raise EditsNotApplicable(sorted(failures, key=lambda f: f.index), len(blocks))
    return resolved


def _resolve_file(
    root: Path, normalized: str, members: list[tuple[int, SearchReplace]]
) -> str | list[EditFailure] | None:
    """New contents for one file, `None` for no net change, or its failures."""

    def fail(index: int, block: SearchReplace, kind: str, **extra: Any) -> EditFailure:
        return EditFailure(index=index, path=block.path, kind=kind, search=block.search, **extra)

    target = (root / normalized).resolve()
    if not target.is_relative_to(root):
        return [fail(i, b, "path_escapes_repo") for i, b in members]

    if not target.exists():
        creators = [(i, b) for i, b in members if b.search == ""]
        problems = [fail(i, b, "file_missing") for i, b in members if b.search != ""]
        problems += [fail(i, b, "overlap") for i, b in creators[1:]]
        return problems or creators[0][1].replace

    try:
        original = read_repo_file(root, normalized)
    except (UnicodeDecodeError, ValueError):
        return [fail(i, b, "unreadable") for i, b in members]

    spans: list[tuple[int, int, int, SearchReplace]] = []
    problems = []
    for index, block in members:
        if block.search == "":
            # Fills an empty file, and must then be the only block for it.
            if original != "":
                problems.append(fail(index, block, "empty_search"))
            elif len(members) > 1:
                problems.append(fail(index, block, "overlap"))
            else:
                spans.append((0, 0, index, block))
            continue

        starts = _occurrences(original, block.search)
        if not starts:
            problems.append(
                fail(index, block, "no_match", match_count=0, hint=_near_miss(original, block.search))
            )
        elif len(starts) > 1:
            problems.append(fail(index, block, "ambiguous", match_count=len(starts)))
        else:
            spans.append((starts[0], starts[0] + len(block.search), index, block))

    spans.sort(key=lambda span: span[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            problems.append(fail(current[2], current[3], "overlap"))
    if problems:
        return problems

    pieces, cursor = [], 0
    for start, end, _, block in spans:
        pieces.append(original[cursor:start])
        pieces.append(block.replace)
        cursor = end
    pieces.append(original[cursor:])
    updated = "".join(pieces)
    return None if updated == original else updated


def _occurrences(text: str, needle: str) -> list[int]:
    """Every start position of `needle` in `text`, overlapping ones included.

    `str.count` skips overlapping matches and would call an ambiguous search
    unique.
    """
    found, start = [], text.find(needle)
    while start != -1:
        found.append(start)
        start = text.find(needle, start + 1)
    return found


def _near_miss(original: str, search: str) -> str | None:
    """A diagnostic for a search that did not match. Reported, never applied.

    Retries the comparison line by line with trailing whitespace ignored, then
    with all surrounding whitespace ignored, and names the first line where that
    looser match exists.
    """
    wanted = search.splitlines()
    lines = original.splitlines()
    if not wanted or len(wanted) > len(lines):
        return None
    for label, norm in (("trailing whitespace", str.rstrip), ("indentation", str.strip)):
        target = [norm(line) for line in wanted]
        normed = [norm(line) for line in lines]
        for start in range(len(normed) - len(target) + 1):
            if normed[start : start + len(target)] == target:
                return f"it would match at line {start + 1} if {label} were ignored"
    return None


def render_apply_failures(failures: list[EditFailure]) -> str:
    """The section appended to the user message for the one repair call.

    Appended *after* the original message, which is otherwise unchanged, so the
    repair call reads the same cached prefix. The failing search texts are shown
    in full: this is the same attempt, nothing was applied, and the model is being
    shown its own output from moments ago, as a parse repair does.
    """
    parts = [
        "Your edit blocks could not be applied, so none of them were. Nothing was "
        "written and no tests ran. The files above are unchanged."
    ]
    for failure in failures:
        parts.append(
            f"{failure.describe()}\n"
            f"Search text you sent:\n<search>\n{failure.search}\n</search>"
        )
    parts.append(
        "Send the complete list of edit blocks again -- every block, including the "
        "ones that were fine -- with each search text copied exactly from the file "
        "contents above."
    )
    return (
        "<previous_edits_did_not_apply>\n"
        + "\n\n".join(parts)
        + "\n</previous_edits_did_not_apply>"
    )


def request_diff_edits(
    *,
    client: Any,
    system: str,
    user: str,
    repo_path: str,
    request_payload: dict[str, Any],
    log: Callable[[str, dict[str, Any]], None],
) -> list[FileEdit]:
    """One diff-mode Implementer turn: call, resolve, and at most one repair call.

    Shared by the Implementer and `baseline.py`, so arms A, B and C resolve and
    repair identically. `log(event, payload)` is the caller's own logger, which
    stamps the agent name and the attempt. The repair call does not count as an
    attempt -- no diff was produced -- but it is a real `llm_request`, so every
    call-count comparison includes it.

    Raises `EditsNotApplicable` if the repair call's blocks fail too.
    """
    stage_user = user
    for stage in ("initial", "repair"):
        payload = {**request_payload, "user_chars": len(stage_user), "edit_format": "diff"}
        if stage == "repair":
            payload["repair_of"] = "edit_apply"
        log("llm_request", payload)

        result = client.complete_structured(
            system=system,
            user=stage_user,
            schema=DiffImplementerResponse,
            max_tokens=MAX_TOKENS,
            thinking_level=THINKING_LEVEL,
        )
        log("llm_response", result.log_payload())

        blocks = result.value.edits
        raw = [block.model_dump() for block in blocks]
        try:
            edits = resolve_edits(repo_path, blocks)
        except EditsNotApplicable as exc:
            log("edit_resolution_failed", {"stage": stage, "failures": exc.log_payload(), "edits": raw})
            if stage == "repair":
                raise
            stage_user = user + "\n\n" + render_apply_failures(exc.failures)
            continue

        log("edits_resolved", {"stage": stage, "edits": raw})
        return edits

    raise AssertionError("request_diff_edits fell out of its repair loop")


class Implementer(Agent):
    """Produces `edits`. Its contract lives in `CONTRACTS["implementer"]`.

    `edit_format` is a constructor setting, like the client: it changes the wire
    format of the call, never the contract. In both formats `_run` returns
    `list[FileEdit]` with full contents.
    """

    name = "implementer"

    def __init__(self, event_log: EventLog, client: LLMClient, edit_format: str = "full") -> None:
        if edit_format not in EDIT_FORMATS:
            raise ValueError(f"unknown edit format {edit_format!r}; expected one of {EDIT_FORMATS}")
        super().__init__(event_log)
        self.client = client
        self.edit_format = edit_format

    def _run(
        self,
        *,
        repo_path: str,
        plan: Plan,
        evidence: Evidence | None,
    ) -> list[FileEdit]:
        evidence_kind = evidence.kind if evidence is not None else None

        if self.edit_format == "diff":
            return request_diff_edits(
                client=self.client,
                system=SYSTEM_PROMPT_DIFF,
                user=render_user_message(
                    repo_path=repo_path, plan=plan, evidence=evidence, edit_format="diff"
                ),
                repo_path=repo_path,
                request_payload={
                    "system_chars": len(SYSTEM_PROMPT_DIFF),
                    "evidence_kind": evidence_kind,
                },
                log=lambda event, payload: self.log(event=event, payload=payload),
            )

        user = render_user_message(repo_path=repo_path, plan=plan, evidence=evidence)

        # `evidence_kind` is the one field here worth reading later: it is how the
        # log shows what the retry was told, separately from what it produced.
        self.log(
            event="llm_request",
            payload={
                "system_chars": len(SYSTEM_PROMPT),
                "user_chars": len(user),
                "evidence_kind": evidence_kind,
            },
        )

        result = self.client.complete_structured(
            system=SYSTEM_PROMPT,
            user=user,
            schema=ImplementerResponse,
            max_tokens=MAX_TOKENS,
            thinking_level=THINKING_LEVEL,
        )

        self.log(event="llm_response", payload=result.log_payload())
        return result.value.edits
