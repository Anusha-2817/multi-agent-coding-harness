"""Tests for diff-mode edits: the resolver, the repair call, and the atomic writer.

No API calls. The resolver is pure apart from reading files, so most of this
module is string-in, string-out against a small repository under `tmp_path`.
The property test at the end is seeded `random`, not `hypothesis`: the project
adds no dependency it can do without, and a fixed seed keeps a failure
reproducible from the case number alone.

What these establish, and what they cannot: that resolution is exact,
order-independent and all-or-nothing, and that no write can leave a file half
old and half new. Whether a real model copies its search text exactly is a live
question; see CLAUDE.md, "Diff-mode edits".
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest

import harness.workspace as workspace
from baseline import SYSTEM_PROMPT as BASELINE_SYSTEM_PROMPT
from baseline import SYSTEM_PROMPT_DIFF as BASELINE_SYSTEM_PROMPT_DIFF
from harness.agents.implementer import (
    DIFF_EDIT_RULES,
    MAX_TOKENS,
    RESET_NOTICE,
    RESET_NOTICE_DIFF,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_DIFF,
    THINKING_LEVEL,
    DiffImplementerResponse,
    EditsNotApplicable,
    Implementer,
    ImplementerResponse,
    SearchReplace,
    render_evidence,
    render_user_message,
    resolve_edits,
)
from harness.events import EventLog
from harness.llm import DEFAULT_MODEL, LLMResult, LLMTruncatedError
from harness.loop import render_diff
from harness.state import FileEdit, Plan, ReviewerRejection, TaskState, TesterFailure
from harness.workspace import apply_edits

MODULE = "pkg/mod.py"
TEXT = (
    "import re\n"
    "\n"
    'SPLIT_RE = re.compile(r"([\\-_\\s]*[A-Z]+[^A-Z\\-_\\s]+[\\-_\\s]*)")\n'
    "\n"
    "\n"
    "def first(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def second(x):\n"
    "    return x + 1\n"
)

PLAN = Plan(summary="Fix SPLIT_RE.", steps=["Change + to * in SPLIT_RE"],
            target_files=[MODULE], constraints=[])


def block(search: str, replace: str, path: str = MODULE) -> SearchReplace:
    return SearchReplace(path=path, search=search, replace=replace)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / MODULE).write_text(TEXT, encoding="utf-8", newline="\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n",
                                                encoding="utf-8", newline="\n")
    return root


def failures_of(repo, blocks) -> list:
    with pytest.raises(EditsNotApplicable) as caught:
        resolve_edits(repo, blocks)
    return caught.value.failures


# -- the resolver ------------------------------------------------------------


class TestExactMatch:
    def test_one_match_is_replaced_and_nothing_else_moves(self, repo):
        [edit] = resolve_edits(repo, [block("[^A-Z\\-_\\s]+", "[^A-Z\\-_\\s]*")])

        assert edit.path == MODULE
        assert edit.new_content == TEXT.replace("[^A-Z\\-_\\s]+", "[^A-Z\\-_\\s]*")

    def test_backslashes_and_regex_text_survive_exactly(self, repo):
        line = 'SPLIT_RE = re.compile(r"([\\-_\\s]*[A-Z]+[^A-Z\\-_\\s]+[\\-_\\s]*)")'
        new = 'SPLIT_RE = re.compile(r"([\\-_\\s]*[A-Z]+[^A-Z\\-_\\s]*[\\-_\\s]*)")'
        [edit] = resolve_edits(repo, [block(line, new)])

        assert new in edit.new_content and line not in edit.new_content
        assert edit.new_content.count("\\") == TEXT.count("\\")

    def test_the_trailing_newline_is_preserved_or_absent_as_it_was(self, repo):
        (repo / "pkg" / "bare.py").write_text("a = 1", encoding="utf-8", newline="\n")
        [with_nl] = resolve_edits(repo, [block("return x + 1\n\n\ndef second", "return x + 2\n\n\ndef second")])
        [bare] = resolve_edits(repo, [block("a = 1", "a = 2", path="pkg/bare.py")])

        assert with_nl.new_content.endswith("    return x + 1\n")
        assert bare.new_content == "a = 2"


class TestFailureKinds:
    def test_no_match(self, repo):
        [failure] = failures_of(repo, [block("def third(x):", "def third(y):")])

        assert (failure.kind, failure.match_count, failure.index) == ("no_match", 0, 1)

    def test_a_trailing_whitespace_near_miss_is_named_but_not_applied(self, repo):
        [failure] = failures_of(repo, [block("def first(x):   \n    return x + 1", "x")])

        assert failure.kind == "no_match"
        assert failure.hint == "it would match at line 6 if trailing whitespace were ignored"

    def test_an_indentation_near_miss_is_named(self, repo):
        [failure] = failures_of(repo, [block("def first(x):\nreturn x + 1", "x")])

        assert failure.hint == "it would match at line 6 if indentation were ignored"

    def test_ambiguous_counts_every_occurrence(self, repo):
        [failure] = failures_of(repo, [block("    return x + 1\n", "    return x + 2\n")])

        assert (failure.kind, failure.match_count) == ("ambiguous", 2)

    def test_overlapping_occurrences_are_ambiguous_too(self, repo):
        (repo / "pkg" / "aa.py").write_text("aaa\n", encoding="utf-8")
        [failure] = failures_of(repo, [block("aa", "b", path="pkg/aa.py")])

        assert (failure.kind, failure.match_count) == ("ambiguous", 2)

    def test_overlapping_blocks(self, repo):
        failures = failures_of(repo, [block("def first(x):\n    return", "a"),
                                      block("    return x + 1\n\n\ndef second", "b")])

        assert [(f.index, f.kind) for f in failures] == [(2, "overlap")]

    def test_empty_search_on_a_file_with_contents(self, repo):
        [failure] = failures_of(repo, [block("", "everything\n")])

        assert failure.kind == "empty_search"

    def test_a_search_in_a_missing_file(self, repo):
        [failure] = failures_of(repo, [block("x", "y", path="pkg/new.py")])

        assert failure.kind == "file_missing"

    def test_an_escaping_path_is_a_failure_not_a_crash(self, repo):
        [failure] = failures_of(repo, [block("x", "y", path="../outside.py")])

        assert failure.kind == "path_escapes_repo"
        assert not (repo.parent / "outside.py").exists()

    def test_a_file_that_is_not_utf8(self, repo):
        (repo / "pkg" / "bin.py").write_bytes(b"\xff\xfe\x00bad")
        [failure] = failures_of(repo, [block("bad", "good", path="pkg/bin.py")])

        assert failure.kind == "unreadable"


class TestAllOrNothing:
    def test_every_failure_is_reported_at_once_in_emitted_order(self, repo):
        failures = failures_of(repo, [
            block("nothing like this", "a"),
            block("SPLIT_RE", "SPLIT"),  # fine on its own
            block("    return x + 1\n", "b"),
        ])

        assert [(f.index, f.kind) for f in failures] == [(1, "no_match"), (3, "ambiguous")]

    def test_one_failure_returns_no_edit_for_any_file(self, repo):
        with pytest.raises(EditsNotApplicable) as caught:
            resolve_edits(repo, [block("import re", "import regex"),
                                 block("nope", "x", path="tests/test_mod.py")])

        assert caught.value.block_count == 2
        assert (repo / MODULE).read_text(encoding="utf-8") == TEXT  # nothing written, ever

    def test_the_evidence_reason_echoes_each_failing_searchs_first_line(self, repo):
        with pytest.raises(EditsNotApplicable) as caught:
            resolve_edits(repo, [block("\n\nPASCAL_RE = re.compile(1)\nmore", "x")])

        reason = caught.value.reason()
        assert reason.startswith("1 of 1 edit block(s) could not be applied")
        assert "block 1 (pkg/mod.py): the search text was not found in the file" in reason
        assert "(search began: 'PASCAL_RE = re.compile(1)')" in reason

    def test_a_long_first_line_is_capped_in_the_echo(self, repo):
        with pytest.raises(EditsNotApplicable) as caught:
            resolve_edits(repo, [block("z" * 500, "x")])

        assert "z" * 120 + "..." in caught.value.reason()
        assert "z" * 121 not in caught.value.reason()


class TestOrderAndGrouping:
    def test_block_order_does_not_change_the_result(self, repo):
        blocks = [block("import re", "import regex as re"), block("def second(x):", "def second(y):")]

        assert resolve_edits(repo, blocks) == resolve_edits(repo, blocks[::-1])

    def test_every_search_is_matched_against_the_original(self, repo):
        """Block 2 searches for text block 1 creates. Against the original it is absent."""
        [failure] = failures_of(repo, [block("import re", "import regex"),
                                       block("import regex", "import regex2")])

        assert (failure.index, failure.kind) == (2, "no_match")

    def test_spellings_of_one_path_are_one_file_named_as_first_written(self, repo):
        [edit] = resolve_edits(repo, [block("import re", "import regex", path="pkg\\mod.py"),
                                      block("def second(x):", "def second(y):", path="./pkg/mod.py")])

        assert edit.path == "pkg\\mod.py"
        assert "import regex" in edit.new_content and "def second(y):" in edit.new_content


class TestNoOpsAndNewFiles:
    def test_a_block_that_changes_nothing_drops_its_file(self, repo):
        assert resolve_edits(repo, [block("import re", "import re")]) == []

    def test_no_blocks_resolve_to_no_edits(self, repo):
        assert resolve_edits(repo, []) == []

    def test_an_empty_search_creates_a_missing_file(self, repo):
        [edit] = resolve_edits(repo, [block("", "NEW = 1\n", path="pkg/new.py")])

        assert edit == FileEdit(path="pkg/new.py", new_content="NEW = 1\n")
        assert not (repo / "pkg" / "new.py").exists()  # resolved, not written

    def test_two_blocks_cannot_both_create_one_file(self, repo):
        failures = failures_of(repo, [block("", "a\n", path="pkg/new.py"),
                                      block("", "b\n", path="pkg/new.py")])

        assert [(f.index, f.kind) for f in failures] == [(2, "overlap")]

    def test_an_empty_search_fills_an_empty_file(self, repo):
        (repo / "pkg" / "empty.py").write_text("", encoding="utf-8")
        [edit] = resolve_edits(repo, [block("", "X = 1\n", path="pkg/empty.py")])

        assert edit.new_content == "X = 1\n"


class TestCRLF:
    def test_a_crlf_file_is_matched_as_the_prompt_shows_it(self, repo):
        """Pinned, not chosen: `read_repo_file` reads with universal newlines, so
        the model is shown LF and the search is matched against LF. Full mode
        reads the same way, and `apply_edits` writes LF in both formats."""
        (repo / "pkg" / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
        [edit] = resolve_edits(repo, [block("a = 1\nb = 2", "a = 1\nb = 3", path="pkg/crlf.py")])

        assert edit.new_content == "a = 1\nb = 3\n"


# -- what reaches the wire ---------------------------------------------------


class TestTheSchema:
    def test_it_uses_only_keywords_the_full_mode_schema_already_uses(self):
        def keywords(node, found):
            if isinstance(node, dict):
                for key, value in node.items():
                    found.add(key)
                    if key in ("properties", "$defs"):
                        # Keyed by field or model name, not by schema keyword.
                        for named_schema in value.values():
                            keywords(named_schema, found)
                    else:
                        keywords(value, found)
            elif isinstance(node, list):
                for item in node:
                    keywords(item, found)
            return found

        diff_schema = DiffImplementerResponse.model_json_schema()
        assert keywords(diff_schema, set()) == keywords(ImplementerResponse.model_json_schema(), set())
        assert not {"anyOf", "oneOf", "allOf", "default", "minLength", "pattern"} & keywords(diff_schema, set())

    def test_every_field_is_required_and_extra_keys_are_refused(self):
        definition = DiffImplementerResponse.model_json_schema()["$defs"]["SearchReplace"]

        assert list(definition["properties"]) == ["path", "search", "replace"]  # anchor first
        assert definition["required"] == ["path", "search", "replace"]
        assert definition["additionalProperties"] is False

    def test_openrouter_builds_a_strict_body_for_it(self):
        from llm_openrouter import OpenRouterClient

        body = OpenRouterClient(api_key="k").request_body("s", [], DiffImplementerResponse, MAX_TOKENS, "LOW")
        assert body["response_format"]["json_schema"] == {
            "name": "DiffImplementerResponse", "strict": True,
            "schema": DiffImplementerResponse.model_json_schema()}


class TestFullModeIsUnchanged:
    """Byte pins taken from HEAD before diff mode existed. A full-mode prompt that
    moved would make every earlier result incomparable with every later one."""

    def test_the_full_mode_prompts_are_byte_identical(self):
        def sha(text):
            return hashlib.sha256(text.encode()).hexdigest()

        assert sha(SYSTEM_PROMPT) == "984c3ee0c397dda9910393357e8e99f6f8210b367cbffc22d815f694bda1885b"
        assert sha(RESET_NOTICE) == "d9fd0d26af4e0dd43430efeb9be5f05663444ff7f81079a8e662365fb17deff8"
        assert sha(BASELINE_SYSTEM_PROMPT) == "9f98ca034a39590566366e0b90b5a4510251355bdd28c91e2b561a1b79beae2a"

    def test_evidence_renders_the_full_notice_by_default(self):
        evidence = TesterFailure(failed_tests=["t::x"], traceback="E boom", stdout_tail="1 failed")

        assert render_evidence(evidence) == render_evidence(evidence, "full")
        assert RESET_NOTICE in render_evidence(evidence)
        assert RESET_NOTICE_DIFF in render_evidence(evidence, "diff")
        assert RESET_NOTICE not in render_evidence(evidence, "diff")

    def test_the_diff_prompts_share_the_format_rules_word_for_word(self):
        assert DIFF_EDIT_RULES in SYSTEM_PROMPT_DIFF
        assert DIFF_EDIT_RULES in BASELINE_SYSTEM_PROMPT_DIFF
        assert "test" not in DIFF_EDIT_RULES  # the format says nothing about which files


# -- the Implementer in diff mode --------------------------------------------


class DiffClient:
    """Hands out one scripted response per call: a block list, or an exception."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_structured(self, *, system, user, schema, max_tokens, thinking_level=None):
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens, "thinking_level": thinking_level})
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return LLMResult(value=DiffImplementerResponse(edits=response), model=DEFAULT_MODEL,
                         stop_reason="STOP", parse_attempts=1, usage={"output_tokens": 7})


GOOD = [block("[^A-Z\\-_\\s]+", "[^A-Z\\-_\\s]*")]
BAD = [block("PASCAL_RE = nothing", "x")]


def implement(repo, tmp_path, responses, evidence=None):
    log = EventLog("t", log_dir=tmp_path / "logs")
    client = DiffClient(responses)
    state = TaskState(task_id="t", repo_path=str(repo), task_description="d", failure_input="f",
                      plan=PLAN, evidence=evidence, attempt_count=1)
    try:
        produced = Implementer(log, client, "diff").run(state)
    except (EditsNotApplicable, LLMTruncatedError) as exc:
        produced = exc
    events = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    return produced, client, events


class TestTheImplementerInDiffMode:
    def test_an_unknown_format_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="unknown edit format"):
            Implementer(EventLog("t", log_dir=tmp_path), DiffClient([]), "patch")

    def test_it_produces_resolved_full_file_edits(self, repo, tmp_path):
        produced, client, events = implement(repo, tmp_path, [GOOD])

        assert produced.edits == resolve_edits(repo, GOOD)
        call = client.calls[0]
        assert (call["schema"], call["system"]) == (DiffImplementerResponse, SYSTEM_PROMPT_DIFF)
        assert (call["max_tokens"], call["thinking_level"]) == (MAX_TOKENS, THINKING_LEVEL)
        assert call["user"] == render_user_message(repo_path=str(repo), plan=PLAN, evidence=None,
                                                   edit_format="diff")
        assert [e["event"] for e in events] == ["llm_request", "llm_response", "edits_resolved",
                                                "agent_produced"]
        assert events[0]["payload"]["edit_format"] == "diff"
        assert "repair_of" not in events[0]["payload"]
        assert events[2]["payload"] == {"stage": "initial", "edits": [b.model_dump() for b in GOOD]}

    def test_a_retry_is_told_the_diff_reset_notice(self, repo, tmp_path):
        _, client, _ = implement(repo, tmp_path, [GOOD],
                                 evidence=ReviewerRejection(reason="r", violated_constraints=[]))

        assert RESET_NOTICE_DIFF in client.calls[0]["user"]
        assert RESET_NOTICE not in client.calls[0]["user"]

    def test_a_failed_resolution_gets_one_repair_call_on_the_same_prefix(self, repo, tmp_path):
        produced, client, events = implement(repo, tmp_path, [BAD, GOOD])

        assert produced.edits == resolve_edits(repo, GOOD)
        first, repair = client.calls
        assert repair["user"].startswith(first["user"] + "\n\n<previous_edits_did_not_apply>")
        assert "PASCAL_RE = nothing" in repair["user"]
        assert repair["schema"] is DiffImplementerResponse and repair["system"] == first["system"]
        assert [e["event"] for e in events] == [
            "llm_request", "llm_response", "edit_resolution_failed",
            "llm_request", "llm_response", "edits_resolved", "agent_produced"]
        assert events[3]["payload"]["repair_of"] == "edit_apply"
        assert events[2]["payload"]["failures"][0]["kind"] == "no_match"
        assert all(e["attempt"] == 1 for e in events)  # the repair is not a new attempt

    def test_two_failures_raise_after_exactly_two_calls(self, repo, tmp_path):
        produced, client, events = implement(repo, tmp_path, [BAD, BAD])

        assert isinstance(produced, EditsNotApplicable)
        assert len(client.calls) == 2
        names = [e["event"] for e in events]
        assert names.count("llm_request") == names.count("llm_response") == 2
        assert names.count("edit_resolution_failed") == 2
        assert "agent_produced" not in names

    def test_a_truncated_repair_call_still_logs_its_usage(self, repo, tmp_path):
        truncated = LLMTruncatedError("out of budget", stage="truncated", raw_text="")
        truncated.call_payload = {"model": DEFAULT_MODEL, "stop_reason": "MAX_TOKENS",
                                  "parse_attempts": 1, "usage": {"output_tokens": 9}}
        produced, _, events = implement(repo, tmp_path, [BAD, truncated])

        assert isinstance(produced, LLMTruncatedError)
        responses = [e for e in events if e["event"] == "llm_response"]
        assert [r["payload"]["stop_reason"] for r in responses] == ["STOP", "MAX_TOKENS"]


# -- replay ------------------------------------------------------------------


class TestReplay:
    def test_the_log_alone_reproduces_the_edits_and_the_diff(self, repo, tmp_path):
        """Baseline + `edits_resolved` -> the same `FileEdit`s and the same diff."""
        produced, _, events = implement(repo, tmp_path, [BAD, GOOD])
        logged = next(e for e in events if e["event"] == "edits_resolved")["payload"]["edits"]

        replayed = resolve_edits(repo, [SearchReplace(**b) for b in logged])
        produced_event = next(e for e in events if e["event"] == "agent_produced")

        assert [e.model_dump() for e in replayed] == produced_event["payload"]["edits"]
        assert render_diff(repo, replayed) == render_diff(repo, produced.edits)


# -- the atomic writer (both formats) ------------------------------------------


class TestAtomicWrites:
    def test_a_failed_replace_leaves_the_old_file_and_no_temporary(self, repo, monkeypatch):
        def refuse(src, dst):
            raise OSError("disk said no")

        monkeypatch.setattr(workspace.os, "replace", refuse)
        with pytest.raises(OSError):
            apply_edits(repo, [FileEdit(path=MODULE, new_content="replaced\n")])

        assert (repo / MODULE).read_text(encoding="utf-8") == TEXT
        assert sorted(p.name for p in (repo / "pkg").iterdir()) == ["mod.py"]

    def test_the_bytes_written_are_what_write_text_wrote(self, repo):
        apply_edits(repo, [FileEdit(path=MODULE, new_content="a\nb\n"),
                           FileEdit(path="pkg/new/deep.py", new_content="c")])

        assert (repo / MODULE).read_bytes() == b"a\nb\n"  # LF on Windows too
        assert (repo / "pkg" / "new" / "deep.py").read_bytes() == b"c"
        assert sorted(p.name for p in (repo / "pkg").iterdir()) == ["mod.py", "new"]


# -- the property test ---------------------------------------------------------

# About 11% of cases resolve to a real change, 20% to a no-op and 69% are refused,
# so 2000 gives a couple of hundred successful writes to interrupt.
CASES = 2000
ALPHABET = ["a", "b", "ab", " ", "\n", "x\n", "    ", "\\s", "("]


def oracle(text: str, blocks: list[SearchReplace]) -> str | None:
    """An independent statement of the rules for one file: the new text, or None.

    Brute-force occurrence search and a separate overlap test, written apart from
    `resolve_edits` so the two can disagree.
    """
    spans = []
    for b in blocks:
        if b.search == "":
            if text != "" or len(blocks) > 1:
                return None
            return b.replace
        hits = [i for i in range(len(text)) if text.startswith(b.search, i)]
        if len(hits) != 1:
            return None
        spans.append((hits[0], hits[0] + len(b.search), b.replace))
    spans.sort()
    if any(spans[i + 1][0] < spans[i][1] for i in range(len(spans) - 1)):
        return None
    out, cursor = "", 0
    for start, end, replace in spans:
        out += text[cursor:start] + replace
        cursor = end
    return out + text[cursor:]


def random_case(rng: random.Random) -> tuple[str, list[SearchReplace]]:
    text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 30)))
    blocks = []
    for _ in range(rng.randint(0, 4)):
        roll = rng.random()
        if roll < 0.6 and text:
            start = rng.randrange(len(text))
            search = text[start : start + rng.randint(1, 8)]
        elif roll < 0.8:
            search = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 3)))
        else:
            search = ""
        replace = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 4)))
        blocks.append(block(search, replace, path="p.py"))
    return text, blocks


class TestNoSequenceOfEditsCanPartiallyWriteAFile:
    def test_resolution_matches_the_oracle_and_every_file_is_old_or_new(self, tmp_path, monkeypatch):
        real_replace = workspace.os.replace
        for case in range(CASES):
            rng = random.Random(case)
            text, blocks = random_case(rng)
            root = tmp_path / str(case)
            root.mkdir()
            (root / "p.py").write_text(text, encoding="utf-8", newline="\n")
            (root / "q.py").write_text("untouched\n", encoding="utf-8", newline="\n")
            expected = oracle(text, blocks)

            try:
                edits = resolve_edits(root, blocks)
            except EditsNotApplicable:
                assert expected is None, f"case {case}: resolver refused what the oracle accepts"
                assert (root / "p.py").read_bytes() == text.encode(), f"case {case}"
                continue

            assert expected is not None, f"case {case}: resolver accepted what the oracle refuses"
            assert [e.new_content for e in edits] == ([] if expected == text else [expected]), f"case {case}"

            # Kill the write at a random point -- before, at, or after the replace --
            # and require every file to be exactly its old or exactly its new bytes.
            edits = edits + [FileEdit(path="q.py", new_content="changed\n")]
            fail_on = rng.randint(1, len(edits) + 1)
            calls = {"n": 0}

            def flaky(src, dst, fail_on=fail_on, calls=calls):
                calls["n"] += 1
                if calls["n"] == fail_on:
                    raise OSError("killed mid-apply")
                real_replace(src, dst)

            monkeypatch.setattr(workspace.os, "replace", flaky)
            try:
                apply_edits(root, edits)
            except OSError:
                pass
            finally:
                monkeypatch.setattr(workspace.os, "replace", real_replace)

            for edit in edits:
                on_disk = (root / edit.path).read_bytes()
                before = (text if edit.path == "p.py" else "untouched\n").encode()
                assert on_disk in (before, edit.new_content.encode()), f"case {case}: {edit.path} is partial"
            assert sorted(p.name for p in root.iterdir()) == ["p.py", "q.py"], f"case {case}: temp left"
