"""The LLM client wrapper: one text-in, validated-object-out call.

**The split this module draws, and why.** Transport is the official `anthropic`
SDK: connection pooling, HTTP retries with exponential backoff, `retry-after`
parsing, and typed error classes. Hand-rolling that over `urllib` would cost a
session and demonstrate nothing about agentic systems -- it is plumbing every
project needs and no project should write twice.

The scope fence bans **agent frameworks** -- LangGraph, LangChain, CrewAI --
because they would build the control loop, which is the whole point of this
project. A provider HTTP client builds nothing of the sort. The distinction is
between a dependency that would do the interesting work for us and one that
saves us from re-implementing sockets.

So the interesting half stays hand-rolled, right here:

- **The parse ladder** -- three deterministic steps from response text to a JSON
  value (`extract_json`).
- **The repair turn** -- one bounded retry that hands the model its own
  validation error and asks for a correction (`repair_prompt`).
- **The failure taxonomy** -- which failures are worth retrying, which are worth
  repairing, and which are worth neither.

That is also why this calls `messages.create` and not the SDK's `messages.parse`.
`parse` would do the extraction and validation for us, and those are exactly the
two steps this phase exists to understand.

**Where retries live.** CLAUDE.md's "Retry vs. backoff" split holds, with a third
case added by this module:

- *Agent retries* (Reviewer rejection, Tester failure) -- the control loop's job.
  A bad diff. Never here.
- *HTTP retries* (429, 5xx, connection errors) -- the SDK's job, configured here
  via `max_retries`. Exponential backoff, because the server needs time.
- *Parse repairs* (malformed JSON, failed validation) -- this module's job. The
  model returned text that is not the object we asked for. It never touches
  `attempt_count`: no diff was produced, so nothing happened that the loop's
  counter is counting.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

# Thinking is on by default on this model and `max_tokens` caps thinking *plus*
# response text, which is why callers pass generous budgets.
DEFAULT_MODEL = "claude-opus-5"

# HTTP attempts beyond the first, handled inside the SDK with backoff.
DEFAULT_MAX_RETRIES = 3

# Per HTTP attempt, not per call. Worst case is roughly
# `timeout * (max_retries + 1)` plus the SDK's own backoff sleeps.
DEFAULT_TIMEOUT = 120.0

# Parse attempts beyond the first. One repair turn, then give up -- a model that
# cannot produce the object twice in a row is not going to on the third try, and
# each round trip costs the full prompt again.
DEFAULT_MAX_PARSE_RETRIES = 1

# What the SDK retries on our behalf. Duplicated here for one purpose: telling a
# caller whether the failure they are holding was already retried three times
# (network or capacity -- try later) or was returned immediately (the request
# itself is wrong -- fix it). Those send you to completely different files.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# ```json ... ``` or ``` ... ```, the wrapper a model reaches for when it decides
# to be helpful. Non-greedy so several fenced blocks stay separate.
_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """Base for everything this module raises."""


class LLMTransportError(LLMError):
    """The request never came back with a usable response.

    Carries `status_code` (None for a connection-level failure) and `retryable`,
    which says whether the SDK already spent its retries on this before giving
    up. A retryable failure that still arrived here means the API is genuinely
    unwell; a non-retryable one means the request is malformed and retrying it
    would fail identically.
    """

    def __init__(self, detail: str, *, status_code: int | None, retryable: bool) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(detail)


class LLMResponseError(LLMError):
    """A response came back, but it is not the object that was asked for.

    `raw_text` is the model's text verbatim -- it is the only evidence of what
    went wrong, and a truncated version of it is a debugging dead end.
    `problems` is the same failure rendered for the *model* to read, which is
    what `repair_prompt` puts in the repair turn.

    Fatal by design: it propagates out of `_run`, out of `run_task`, and ends the
    run in a stack trace rather than a `Status`. A model that cannot emit its own
    schema twice is neither a task outcome (the run produced no diff to judge)
    nor recoverable by another attempt. Phase 4 owns escalation output and is the
    right place to decide whether this deserves a status of its own; adding one
    now would mean a seventh `Status` justified by nothing but a guess about
    frequency.
    """

    def __init__(self, detail: str, *, stage: str, raw_text: str, problems: str = "") -> None:
        self.stage = stage
        self.raw_text = raw_text
        self.problems = problems
        super().__init__(detail)


class LLMTruncatedError(LLMResponseError):
    """The response hit `max_tokens` mid-object.

    Deliberately not repaired. The repair turn would run into the same ceiling at
    the same place and burn a second full-prompt round trip to produce the same
    truncated text. The fix is a larger `max_tokens`, which is a config change,
    not a runtime recovery -- and distinguishing it from ordinary malformed JSON
    is the difference between an error that tells you what to do and one that
    sends you hunting.
    """


class LLMRefusalError(LLMResponseError):
    """The model declined. Content is empty or partial; there is nothing to parse.

    Also not repaired: rephrasing a refusal is not this module's business, and a
    repair turn would only ask a model that just declined to decline again.
    """


@dataclass(frozen=True)
class LLMResult:
    """One completed call: the validated object, plus what it cost to get it.

    The metadata is here because this module cannot log. `EventLog.append` needs
    `attempt` and `agent` -- loop identity that a transport wrapper has no
    business knowing, and that `Agent.log` already has for free. So the client
    reports and the agent logs. Without it, the repair turns and HTTP retries
    would be the only part of a run that leaves no trace, and they are the part
    worth tracing.
    """

    value: BaseModel
    model: str
    stop_reason: str | None
    parse_attempts: int
    usage: dict[str, int] = field(default_factory=dict)

    def log_payload(self) -> dict[str, Any]:
        """What an agent should log about this call, minus the produced value.

        The value is omitted on purpose: the base class's `agent_produced` event
        already carries it, and CLAUDE.md is explicit that a subclass must not
        re-log what it produced.
        """
        return {
            "model": self.model,
            "stop_reason": self.stop_reason,
            "parse_attempts": self.parse_attempts,
            "usage": self.usage,
        }


# -- the parse ladder --------------------------------------------------------


def extract_json(text: str) -> tuple[Any, str]:
    """Find the JSON value in a model's response. Returns `(value, how)`.

    Three steps, cheapest first, each deterministic:

    1. `direct` -- the whole response is JSON. What structured outputs should
       produce, and what it does produce nearly always.
    2. `fenced` -- the JSON is inside a ``` block. The single most common thing
       a model does when it decides to be helpful about it.
    3. `braces` -- from the first `{` to the last `}`. The catch-all for a
       leading "Here's the plan:" or a trailing "Let me know if...".

    This is envelope handling, not sanitising model output. The distinction
    matters and CLAUDE.md draws it elsewhere: `render_diff` passes CRLF through
    untouched because the line endings *are part of the change being reviewed*.
    Nothing here alters a single byte of the JSON -- it only decides where the
    JSON starts and stops. Being strict instead would spend a repair round trip,
    at full prompt cost, on a wrapper that costs fifteen lines to see through.

    `how` is returned rather than discarded so an agent can log which step was
    needed. A run that is always reaching step 3 is telling you the prompt has
    stopped working.
    """
    for how, candidate in _candidates(text):
        try:
            return json.loads(candidate), how
        except (json.JSONDecodeError, ValueError):
            continue

    raise LLMResponseError(
        "no JSON value could be read from the response, by any of "
        "direct / fenced / braces",
        stage="extract",
        raw_text=text,
        problems="- your response did not contain a JSON object",
    )


def _candidates(text: str) -> list[tuple[str, str]]:
    """The three ladder steps as `(label, text)`, in order, skipping empty ones."""
    stripped = text.strip()
    found: list[tuple[str, str]] = []

    if stripped:
        found.append(("direct", stripped))

    found.extend(("fenced", block.strip()) for block in _FENCE.findall(text) if block.strip())

    opened, closed = stripped.find("{"), stripped.rfind("}")
    if opened != -1 and closed > opened:
        found.append(("braces", stripped[opened : closed + 1]))

    return found


def describe_problems(exc: Exception) -> str:
    """Render a parse or validation failure for the *model* to read.

    Pydantic's `ValidationError` is unusually good input here: it names the
    field, and it distinguishes missing from wrong-typed from *unexpected*. That
    last one is not an edge case. Every model in `state.py` sets
    `extra="forbid"`, so a Planner that helpfully adds a `"notes"` key fails
    validation outright -- correctly, since an extra key means it misunderstood
    its contract, but often enough that naming the offending key is the whole
    repair.
    """
    if isinstance(exc, ValidationError):
        lines = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "(whole object)"
            lines.append(f"- {where}: {error['msg']}")
        return "\n".join(lines)

    if isinstance(exc, json.JSONDecodeError):
        return f"- the response was not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"

    return f"- {type(exc).__name__}: {exc}"


REPAIR_PROMPT = """\
Your previous response could not be used. These problems were found in it:

{problems}

Send the corrected JSON object and nothing else — no explanation, no markdown
fence, no commentary before or after it. Include every required field, and no
fields beyond the ones the schema names.\
"""


def repair_prompt(problems: str) -> str:
    """The user turn that follows a bad response.

    Sent with no delay before it, unlike the HTTP backoff one layer down. The
    backoff exists because the *server* needs time to recover; the model is
    stateless and holds nothing that waiting would improve. This is also not a
    resend -- it is a different, better-informed request, carrying an error
    message the first one could not have had.
    """
    return REPAIR_PROMPT.format(problems=problems)


# -- the client --------------------------------------------------------------


class LLMClient:
    """Text in, validated Pydantic object out.

    One public method. Agents hold one of these as a constructor dependency,
    exactly as the Tester holds its `timeout` -- see CLAUDE.md, "The agent
    signature".
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        max_parse_retries: int = DEFAULT_MAX_PARSE_RETRIES,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                "no API key: pass api_key= or set ANTHROPIC_API_KEY. "
                "Phases 1-2 make zero API calls; only Phase 3 needs this."
            )

        self.model = model
        self.max_parse_retries = max_parse_retries
        # `max_retries` and `timeout` on the SDK client are the HTTP half of
        # CLAUDE.md's retry/backoff split -- 408/409/429/5xx and connection
        # errors, with exponential backoff and `retry-after` honoured.
        self._sdk = anthropic.Anthropic(api_key=key, max_retries=max_retries, timeout=timeout)

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
    ) -> LLMResult:
        """Ask for one object of `schema`'s shape, and return it validated.

        `schema` must describe a JSON *object*, because that is what the API's
        structured outputs accepts at the root. `Plan` already is one; a producer
        of a bare list wraps it in a one-field envelope -- see
        `implementer.ImplementerResponse`.
        """
        json_schema = schema.model_json_schema()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

        for parse_attempt in range(1, self.max_parse_retries + 2):
            envelope = self._create(
                system=system,
                messages=messages,
                json_schema=json_schema,
                max_tokens=max_tokens,
            )
            # Outside the try below, and that placement is the whole mechanism:
            # a truncation or a refusal raises straight past the repair loop,
            # because neither is repairable by asking again more politely.
            text = self._text_of(envelope)

            try:
                value = _validate(text, schema)
            except LLMResponseError as exc:
                if parse_attempt > self.max_parse_retries:
                    raise
                messages = [
                    *messages,
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": repair_prompt(exc.problems)},
                ]
                continue

            return LLMResult(
                value=value,
                model=getattr(envelope, "model", self.model),
                stop_reason=getattr(envelope, "stop_reason", None),
                parse_attempts=parse_attempt,
                usage=_usage_of(getattr(envelope, "usage", None)),
            )

        # Unreachable: the loop either returns or raises on its last pass.
        raise AssertionError("complete_structured fell out of its retry loop")

    # -- transport -----------------------------------------------------------

    def _create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        json_schema: dict[str, Any],
        max_tokens: int,
    ) -> Any:
        """One API call, with SDK exceptions mapped onto this module's two.

        `thinking` is left unset: it is adaptive by default on this model, and
        naming it would only be a chance to name it wrong. `temperature` and its
        relatives are not sent at all -- they are rejected outright on this
        model, and prompting is the steering mechanism here anyway.
        """
        try:
            return self._sdk.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": json_schema}},
            )
        except anthropic.APIStatusError as exc:
            raise _transport_error(exc) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransportError(
                f"could not reach the API after retries: {type(exc).__name__}: {exc}",
                status_code=None,
                retryable=True,
            ) from exc

    def _text_of(self, envelope: Any) -> str:
        """The model's text, or an exception explaining why there is none.

        `stop_reason` is checked before the content, because a refusal can arrive
        with an empty `content` list and indexing it would report a shape problem
        instead of the real one.

        The text is then *searched for*, not indexed at position zero: thinking is
        on by default on this model, so `content[0]` is a thinking block whose
        text is empty under the default display setting. `content[0].text` is the
        single easiest way to write a client that appears to receive nothing.
        """
        stop_reason = getattr(envelope, "stop_reason", None)
        blocks = list(getattr(envelope, "content", None) or [])
        partial = _first_text(blocks)

        if stop_reason == "refusal":
            raise LLMRefusalError(
                "the model declined the request",
                stage="refusal",
                raw_text=partial,
                problems="- the model declined to answer",
            )

        if stop_reason == "max_tokens":
            raise LLMTruncatedError(
                "the response hit max_tokens and is truncated; raise max_tokens "
                "rather than retrying, since the retry truncates identically",
                stage="truncated",
                raw_text=partial,
                problems="- the response was cut off before it was complete",
            )

        if not partial:
            raise LLMResponseError(
                f"the response carried no text block (stop_reason={stop_reason!r}, "
                f"{len(blocks)} block(s))",
                stage="empty",
                raw_text="",
                problems="- your response contained no text",
            )

        return partial


# -- helpers -----------------------------------------------------------------


def _validate(text: str, schema: type[BaseModel]) -> BaseModel:
    """Text -> JSON value -> validated model. Both failures land as one type."""
    payload, _how = extract_json(text)
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise LLMResponseError(
            f"the response was JSON but not a valid {schema.__name__}",
            stage="validate",
            raw_text=text,
            problems=describe_problems(exc),
        ) from exc


def _transport_error(exc: anthropic.APIStatusError) -> LLMTransportError:
    """Classify a status error so the message says where to go looking."""
    status = getattr(exc, "status_code", None)
    retryable = status in RETRYABLE_STATUS

    if retryable:
        detail = (
            f"HTTP {status} after the client's retries were exhausted; "
            f"the API is unavailable rather than the request being wrong"
        )
    else:
        detail = (
            f"HTTP {status}, which is not retryable -- the request itself is the "
            f"problem, and sending it again would fail identically"
        )

    return LLMTransportError(f"{detail}: {exc}", status_code=status, retryable=retryable)


def _first_text(blocks: list[Any]) -> str:
    """The first `text` block's text, or "" if there is none."""
    for block in blocks:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


def _usage_of(usage: Any) -> dict[str, int]:
    """Token counts as a plain dict, tolerating fields the SDK may not send."""
    if usage is None:
        return {}

    wanted = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    counted = {}
    for name in wanted:
        value = getattr(usage, name, None)
        if isinstance(value, int):
            counted[name] = value
    return counted
