"""Owns the run directory.

The Tester does not create, reset, or delete the directory it tests -- it is
handed a prepared `repo_path` and reads nothing else. Keeping that ownership
here is what lets "every attempt starts from a fresh copy of the fixture" be a
property of one small module rather than an agreement between several.

The fixture directory is only ever read from. Nothing in this module writes to
it, and the copy is where all work -- apply, test, reset -- happens.
"""

from __future__ import annotations

import shutil
from pathlib import Path

RUNS_ROOT = "runs"

# Copied fixtures never carry stale bytecode or a stale pytest cache into a run.
# The Tester clears these again inside the run directory before invoking pytest,
# because the previous attempt's run generated its own.
IGNORED = shutil.ignore_patterns("__pycache__", ".pytest_cache")


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
