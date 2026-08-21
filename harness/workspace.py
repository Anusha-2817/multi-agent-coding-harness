"""Owns the run directory.

The Tester does not create, reset, or delete the directory it tests -- it is
handed a prepared `repo_path` and reads nothing else. Keeping that ownership
here is what lets "every attempt starts from a fresh copy of the fixture" be a
property of one small module rather than an agreement between several.

The fixture directory is only ever read from. Nothing in this module writes to
it, and the copy is where all work -- apply, test, reset -- happens.

`apply_edits` lives here for the same reason: it is the one function in the
harness that writes files, and keeping it beside the reset it undoes means the
whole lifecycle of the run directory is readable in one place.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from harness.state import FileEdit

RUNS_ROOT = "runs"

# Copied fixtures never carry stale bytecode or a stale pytest cache into a run.
# The Tester clears these again inside the run directory before invoking pytest,
# because the previous attempt's run generated its own.
_CACHE_DIR_NAMES = ("__pycache__", ".pytest_cache")
IGNORED = shutil.ignore_patterns(*_CACHE_DIR_NAMES)


def normalize_path(path: str) -> str:
    """One spelling for a repo-relative path: posix separators, no leading `./`.

    An LLM Implementer on Windows will emit `pricing\\discounts.py` sooner or
    later, and a plan written by an LLM Planner may say `./pricing/discounts.py`.
    Neither is a scope violation -- they are the same file -- and rejecting them
    as one would spend a retry on a path-separator bug while the log claimed the
    model had gone outside its plan. That is exactly the plumbing-versus-model
    ambiguity the harness exists to remove.

    Used in two places that must agree: the control loop's scope check, and
    `apply_edits` below. If only the check normalised, it could accept a spelling
    that the write then resolved somewhere else.

    `..` is deliberately *not* resolved away. Normalising it would turn a path
    that escapes the run directory into one that looks legitimate; leaving it
    means `apply_edits`'s containment check still sees it.
    """
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def run_dir_for(task_id: str, runs_root: str | Path = RUNS_ROOT) -> str:
    """Where this task's working copy lives. Does not create anything."""
    return str(Path(runs_root) / task_id)


def prepare_run_dir(
    task_id: str,
    fixture_path: str | Path,
    runs_root: str | Path = RUNS_ROOT,
) -> str:
    """Copy the fixture to `runs/<task_id>/` and return that path.

    Raises `FileExistsError` if the directory is already there. Because
    `task_id` carries a timestamp, a collision means either a stale directory
    from a crashed run or a genuine id clash -- both worth surfacing rather than
    silently overwriting. Use `reset_run_dir` to deliberately replace one.
    """
    target = Path(runs_root) / task_id
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture_path, target, ignore=IGNORED)
    return str(target)


def reset_run_dir(
    task_id: str,
    fixture_path: str | Path,
    runs_root: str | Path = RUNS_ROOT,
) -> str:
    """Delete the run directory and re-copy the fixture. Return the path.

    This is the whole of baseline reset: no git, no patch reversal, no cleanup
    logic to get wrong. Every attempt is diffed against identical ground because
    the ground is a fresh copy, not a repaired one.
    """
    target = Path(runs_root) / task_id
    shutil.rmtree(target, ignore_errors=True)
    return prepare_run_dir(task_id, fixture_path, runs_root)


def list_repo_files(repo_path: str | Path) -> list[str]:
    """Every file in the run directory, as sorted normalised relative paths.

    Reading lives here for the same reason writing does: this module owns the
    run directory, so the agents that need to see it do not each grow their own
    `Path` arithmetic and their own idea of which directories to skip.

    Sorted, so a prompt built from this is byte-stable across runs -- the same
    reason `render_diff` sorts its edits. Cache directories are skipped, matching
    what `copytree` already ignored; a `.pyc` in a prompt is pure noise.
    """
    root = Path(repo_path)
    skip = set(_CACHE_DIR_NAMES)
    found = [
        normalize_path(str(path.relative_to(root)))
        for path in root.rglob("*")
        if path.is_file() and not skip.intersection(path.parts)
    ]
    return sorted(found)


def read_repo_file(repo_path: str | Path, relative: str) -> str:
    """Read one file out of the run directory, by repo-relative path.

    Its own containment check rather than a shared one with `apply_edits`: the
    two raise for different reasons and a reader that borrowed the writer's
    error message would misdescribe what went wrong. What they do share is
    `normalize_path`, which is the part that has to agree -- if the reader
    accepted a spelling the scope check would reject, the Implementer would be
    shown a file it is not allowed to edit.

    Raises `ValueError` if the path escapes `repo_path`. Reachable from a plan
    whose `target_files` names `../something`, which the scope check would
    happily accept: `normalize_path` deliberately does not resolve `..` away, so
    a plan can name an escaping path and an Implementer can faithfully target it.
    Catching it on the read means it never reaches the write.
    """
    root = Path(repo_path).resolve()
    target = (root / normalize_path(relative)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(
            f"path {relative!r} resolves to {target}, which is outside "
            f"the run directory {root}"
        )
    return target.read_text(encoding="utf-8")


def apply_edits(repo_path: str | Path, edits: list[FileEdit]) -> list[str]:
    """Write `edits` into the run directory. Returns the paths written.

    The apply step, and the only place in the harness that writes a file the
    Implementer produced. The control loop calls it in exactly one position:
    after the Reviewer has approved and after the human has approved *that*
    diff. Invariant 1 is a property of that call site, not of this function --
    which is why nothing here checks for a verdict. Do not add a second caller.

    `FileEdit` is full file replacement, so each edit is one `write_text`. Paths
    are created if missing: a plan may legitimately name a file that does not
    exist yet.

    Raises `ValueError` if an edit resolves outside `repo_path`. That should be
    unreachable -- the scope check has already confined every path to
    `plan.target_files` -- so reaching it means either the check let something
    through or the plan itself named an escaping path. Both are worth a stack
    trace rather than a quiet write into the fixture, or somewhere worse. Fatal
    for the same reason `AgentContractError` is: it is a harness bug, not a task
    outcome, and `Status` has no value for it.
    """
    root = Path(repo_path).resolve()
    written: list[str] = []

    for edit in edits:
        relative = normalize_path(edit.path)
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError(
                f"edit path {edit.path!r} resolves to {target}, which is outside "
                f"the run directory {root}"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" so the bytes on disk are the bytes the diff was rendered
        # from. Windows text mode would translate to CRLF and make the applied
        # file differ from the diff the human approved.
        target.write_text(edit.new_content, encoding="utf-8", newline="\n")
        written.append(relative)

    return written
