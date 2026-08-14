"""Append-only JSONL event log. One file per run: `logs/<task_id>.jsonl`.

Invariant 6 in CLAUDE.md: every step appends to the event log, no silent state
changes. That makes this the harness's audit trail, so it is deliberately dull —
one public method, `append`, and no way to rewrite history.

Everything is inlined: full plans, full diffs, full tracebacks. No references to
external blobs. A run's log is self-contained and replayable on its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# An unserialisable payload is recorded, not raised. Its repr is capped so one
# bad event cannot produce a multi-megabyte line.
PAYLOAD_REPR_LIMIT = 2000


def _json_default(value: object) -> object:
    """Fallback encoder for values `json` cannot handle on its own.

    Pydantic models are the reason this exists — the log carries whole `Plan`,
    `TaskState`, and evidence objects. `mode="json"` resolves enums and any
    nested models to plain JSON types in one pass.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable")


class EventLog:
    """Append-only writer for one run's log.

    Each `append` opens the file, writes one line, and closes it. Holding the
    handle open would be marginally faster and materially worse: a crash mid-run
    could lose buffered events, and two `EventLog` instances for the same
    `task_id` would fight over the file. A run produces dozens of events, not
    millions, so there is nothing here worth optimising.
    """

    def __init__(self, task_id: str, log_dir: str | Path = "logs") -> None:
        self.task_id = task_id
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{task_id}.jsonl"

    def append(
        self,
        *,
        attempt: int,
        agent: str,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """Append one event. `agent` is an agent name, or "harness" for harness steps.

        Keyword-only because `attempt`, `agent`, and `event` are three adjacent
        arguments that would be easy to transpose positionally and impossible to
        notice afterwards.
        """
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "task_id": self.task_id,
            "attempt": attempt,
            "agent": agent,
            "event": event,
            "payload": payload,
        }

        # Serialise before opening the file. A payload that cannot be encoded
        # must not take the event down with it: the envelope is still written,
        # with the payload replaced by a marker. Dropping the event would breach
        # invariant 6, and raising would kill a run over a logging bug.
        try:
            line = json.dumps(record, default=_json_default, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            record["payload"] = {
                "_serialization_error": f"{type(exc).__name__}: {exc}",
                "_payload_repr": repr(payload)[:PAYLOAD_REPR_LIMIT],
            }
            line = json.dumps(record, ensure_ascii=False)

        # newline="\n" because Windows text mode would otherwise translate to
        # CRLF and put stray \r at the end of every JSON line.
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
