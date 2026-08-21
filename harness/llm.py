"""The LLM client wrapper: one text-in, validated-object-out call.

**The split this module draws, and why.** Transport is the official `google-genai`
SDK: connection handling, HTTP retries with exponential backoff and jitter, and
typed error classes. Hand-rolling that over `urllib` would cost a session and
demonstrate nothing about agentic systems -- it is plumbing every project needs
and no project should write twice.

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

That is also why this asks for `response_json_schema` and then parses the text
itself, rather than reading the SDK's `response.parsed` convenience field.
`parsed` would do the extraction and validation for us, and those are exactly the
two steps this phase exists to understand.

**The provider sits behind `_create` and `_text_of`.** Those two methods and the
constants above them are the entire surface that knows which API this is. The
swap from Anthropic to Gemini in 3A.1 changed nothing else in this file, and
nothing at all outside it -- see CLAUDE.md, "The transport swap".

**Where retries live.** CLAUDE.md's "Retry vs. backoff" split holds, with a third
case added by this module:

- *Agent retries* (Reviewer rejection, Tester failure) -- the control loop's job.
  A bad diff. Never here.
- *HTTP retries* (408, 429, 5xx, connection errors) -- the SDK's job, but only
  because this module asks for it: `google-genai` does **no** retrying unless
  `http_options.retry_options` is set, so `_RETRY_OPTIONS` below is what turns it
  on. Exponential backoff with jitter, because the server needs time.
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

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

# Free tier available, and the best quality-per-quota of the models that have
# one -- see CLAUDE.md, "Free-tier limits are the real constraint". Swap to
# `gemini-2.5-pro` for a harder fixture and a much tighter daily quota.
#
# Thinking is on by default on the 2.5 family with a dynamic budget, and thinking
# tokens are drawn from `max_output_tokens`. That is why callers pass generous
# budgets: too small a budget is spent thinking and returns a `MAX_TOKENS` finish
# with no text at all.
DEFAULT_MODEL = "gemini-3.6-flash"

# HTTP attempts beyond the first, handled inside the SDK with backoff.
DEFAULT_MAX_RETRIES = 3

# Per HTTP attempt, not per call, and in **seconds** -- this module's own unit.
# `HttpOptions.timeout` is milliseconds, converted at the boundary in `__init__`.
DEFAULT_TIMEOUT = 120.0

# Parse attempts beyond the first. One repair turn, then give up -- a model that
# cannot produce the object twice in a row is not going to on the third try, and
# each round trip costs the full prompt again.
DEFAULT_MAX_PARSE_RETRIES = 1

# What the SDK retries on our behalf, once `_RETRY_OPTIONS` has switched retrying
# on. Restated here for one purpose: telling a caller whether the failure they
# are holding was already retried three times (network or capacity -- try later)
# or was returned immediately (the request itself is wrong -- fix it). Those send
# you to completely different files.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Gemini finish reasons that mean the model stopped for a reason no amount of
# asking again will change. Grouped as one set because the response is identical
# for all of them: raise, do not repair.
REFUSAL_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }
)

# The one finish reason that means "there was more to say and no room to say it".
TRUNCATED_FINISH_REASON = "MAX_TOKENS"

# A 429 is retryable when it is a per-minute rate limit and effectively is not
# when the daily quota is gone. Both arrive as `RESOURCE_EXHAUSTED`, so the only
# thing separating them is the quota id in the message. Sniffing a message is
# brittle and this deliberately does not change any behaviour -- the SDK has
# already finished retrying by the time we classify -- it only changes whether
# the error says "try again shortly" or "you are done for the day", which is the
# difference between waiting thirty seconds and waiting until tomorrow.
_DAILY_QUOTA_MARKERS = ("perday", "per day", "requests per day", "daily limit")

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
    """The response ran out of output budget mid-object (`MAX_TOKENS`).

    Deliberately not repaired. The repair turn would run into the same ceiling at
    the same place and burn a second full-prompt round trip to produce the same
    truncated text. The fix is a larger `max_tokens`, which is a config change,
    not a runtime recovery -- and distinguishing it from ordinary malformed JSON
    is the difference between an error that tells you what to do and one that
    sends you hunting.

    On Gemini this fires more readily than the name suggests, because thinking is
    on by default and thinking tokens come out of the same budget. A `MAX_TOKENS`
    finish with *no* text at all usually means the whole budget went on thinking,
    and the fix is the same one: raise `max_tokens`.
    """


class LLMRefusalError(LLMResponseError):
    """The model declined, or its output was blocked. Nothing to parse.

    Covers both ends of the request on Gemini: a `finish_reason` in
    `REFUSAL_FINISH_REASONS` (the output was blocked after generation) and a
    `prompt_feedback.block_reason` (the input was blocked before it, so there is
    no candidate at all). Anthropic had no equivalent of the second case; both
    mean the same thing here.

    Not repaired: rephrasing a refusal is not this module's business, and a
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
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError(
                "no API key: pass api_key= or set GEMINI_API_KEY. "
                "Phases 1-2 make zero API calls; only Phase 3 needs this."
            )

        self.model = model
        self.max_parse_retries = max_parse_retries
        # This is the HTTP half of CLAUDE.md's retry/backoff split, and on this
        # SDK it is opt-in: `google-genai` retries nothing unless `retry_options`
        # is set. `attempts` counts the original request, so +1 keeps
        # `max_retries` meaning "attempts beyond the first", as it did before.
        #
        # `timeout` is milliseconds here and seconds in this module's signature.
        # Converting at the boundary keeps the public signature the unit a caller
        # would expect, and keeps the SDK's unit from leaking upward.
        self._sdk = genai.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(
                timeout=int(timeout * 1000),
                retry_options=genai_types.HttpRetryOptions(attempts=max_retries + 1),
            ),
        )

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

            # The one line in this method that knows anything about the provider,
            # and it knows it only by delegating: reading a served model name, a
            # stop reason and a token count out of a response envelope is
            # transport work, and the shape of all three moved with the SDK.
            return LLMResult(
                value=value,
                parse_attempts=parse_attempt,
                **self._metadata_of(envelope),
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

        The provider-neutral `messages` this receives -- `role` of `user` or
        `assistant`, a `content` string -- are translated to Gemini's shape here
        and nowhere else. That is what let `complete_structured` survive the
        provider swap without a character changing: the repair turn still appends
        an `assistant` turn, and this method knows that Gemini spells it `model`.

        `response_json_schema` takes Pydantic's `model_json_schema()` **as-is**.
        No transform is needed: every keyword Pydantic emits for these models
        (`type`, `properties`, `required`, `items`, `$defs`, `$ref`, `title`,
        `description`, `additionalProperties`) is on Gemini's supported list.
        `additionalProperties` in particular survives, which is what keeps
        `extra="forbid"` load-bearing for the repair path -- an unexpected key
        still fails validation and still gets named back to the model.

        `thinking_config` is left unset: it is dynamic by default on the 2.5
        family, and naming it would only be a chance to name it wrong.
        `temperature` and its relatives are not sent at all -- prompting is the
        steering mechanism here, and a sampling knob would be one more thing to
        keep aligned across two agents.
        """
        try:
            return self._sdk.models.generate_content(
                model=self.model,
                contents=[_as_content(message) for message in messages],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json",
                    response_json_schema=json_schema,
                ),
            )
        except genai_errors.APIError as exc:
            raise _transport_error(exc) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            # `google-genai` lets transport failures through as httpx/socket
            # errors rather than wrapping them, so the catch is on the stdlib
            # bases those inherit from rather than on an SDK class that does not
            # exist.
            raise LLMTransportError(
                f"could not reach the API after retries: {type(exc).__name__}: {exc}",
                status_code=None,
                retryable=True,
            ) from exc

    def _metadata_of(self, envelope: Any) -> dict[str, Any]:
        """What `LLMResult` records about the call, read out of the envelope.

        Sits down here with `_create` and `_text_of` because every field it
        reads is spelled by the provider: Gemini says `model_version` where
        Anthropic said `model`, reports the finish reason on the candidate
        rather than the message, and calls the token counts `usage_metadata`.

        `LLMResult`'s own field names do not change, which is the point --
        `stop_reason` still means "why the model stopped", and an event log from
        before the swap still lines up with one from after.
        """
        candidate = _first_candidate(envelope)
        return {
            "model": getattr(envelope, "model_version", None) or self.model,
            "stop_reason": _finish_reason_of(candidate),
            "usage": _usage_of(getattr(envelope, "usage_metadata", None)),
        }

    def _text_of(self, envelope: Any) -> str:
        """The model's text, or an exception explaining why there is none.

        Three checks before the text, in the order the response can fail:

        1. **The prompt was blocked** -- `prompt_feedback.block_reason`. There is
           no candidate at all, so anything that reached for one would report a
           shape problem instead of the real one.
        2. **The output was blocked** -- a `finish_reason` in
           `REFUSAL_FINISH_REASONS`.
        3. **The output ran out of room** -- `MAX_TOKENS`.

        Only then is the text assembled, and it is assembled by *walking the
        parts*, not by indexing the first one. Thinking is on by default on this
        model and thinking arrives as parts flagged `thought=True`; `parts[0]` is
        the single easiest way to write a client that appears to receive nothing.
        The SDK's `response.text` convenience property is skipped for the same
        reason it was skipped on the previous provider -- it hides exactly the
        distinction these three checks are drawing.
        """
        blocked = _prompt_block_reason(envelope)
        if blocked:
            raise LLMRefusalError(
                f"the prompt was blocked before generation ({blocked})",
                stage="refusal",
                raw_text="",
                problems="- your request was blocked and produced no response",
            )

        candidate = _first_candidate(envelope)
        finish_reason = _finish_reason_of(candidate)
        partial = _text_from_parts(candidate)

        if finish_reason in REFUSAL_FINISH_REASONS:
            raise LLMRefusalError(
                f"the model stopped with {finish_reason}, which no retry changes",
                stage="refusal",
                raw_text=partial,
                problems="- the model declined to answer",
            )

        if finish_reason == TRUNCATED_FINISH_REASON:
            raise LLMTruncatedError(
                "the response hit max_output_tokens and is truncated; raise "
                "max_tokens rather than retrying, since the retry truncates "
                "identically. With no text at all, the budget went on thinking",
                stage="truncated",
                raw_text=partial,
                problems="- the response was cut off before it was complete",
            )

        if not partial:
            raise LLMResponseError(
                f"the response carried no text part (finish_reason="
                f"{finish_reason!r})",
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


def _as_content(message: dict[str, Any]) -> genai_types.Content:
    """One provider-neutral message dict as a Gemini `Content`.

    The whole of the role translation lives here. `complete_structured` speaks
    `user` and `assistant` because that is what it spoke before the swap; Gemini
    spells the second one `model`, and the difference stops at this function.
    """
    role = "model" if message["role"] == "assistant" else "user"
    return genai_types.Content(
        role=role,
        parts=[genai_types.Part.from_text(text=message["content"])],
    )


def _transport_error(exc: genai_errors.APIError) -> LLMTransportError:
    """Classify a status error so the message says where to go looking.

    `APIError.code` is the HTTP status; `.status` is Google's string code
    (`RESOURCE_EXHAUSTED`, `INVALID_ARGUMENT`, ...). Both go in the message,
    because the string is often the more useful half.
    """
    status = getattr(exc, "code", None)
    label = getattr(exc, "status", None)
    retryable = status in RETRYABLE_STATUS

    if retryable and _is_daily_quota(exc):
        # See `_DAILY_QUOTA_MARKERS`: still a 429, still already retried, but
        # calling it retryable would tell the reader to wait thirty seconds for
        # something that resets tomorrow.
        retryable = False
        detail = (
            f"HTTP {status} ({label}) and the message names a per-day quota, so "
            f"retrying will not help until the daily allowance resets"
        )
    elif retryable:
        detail = (
            f"HTTP {status} ({label}) after the client's retries were exhausted; "
            f"the API is unavailable rather than the request being wrong"
        )
    else:
        detail = (
            f"HTTP {status} ({label}), which is not retryable -- the request "
            f"itself is the problem, and sending it again would fail identically"
        )

    return LLMTransportError(f"{detail}: {exc}", status_code=status, retryable=retryable)


def _is_daily_quota(exc: genai_errors.APIError) -> bool:
    """Whether a `RESOURCE_EXHAUSTED` names a daily allowance rather than a rate."""
    haystack = f"{getattr(exc, 'message', '')} {getattr(exc, 'details', '')}".lower()
    return any(marker in haystack for marker in _DAILY_QUOTA_MARKERS)


def _prompt_block_reason(envelope: Any) -> str | None:
    """The reason the *input* was blocked, if it was. `None` when it was not."""
    feedback = getattr(envelope, "prompt_feedback", None)
    reason = getattr(feedback, "block_reason", None) if feedback is not None else None
    return _enum_name(reason)


def _first_candidate(envelope: Any) -> Any:
    """The first candidate, or `None`. Only one is ever requested."""
    candidates = list(getattr(envelope, "candidates", None) or [])
    return candidates[0] if candidates else None


def _finish_reason_of(candidate: Any) -> str | None:
    if candidate is None:
        return None
    return _enum_name(getattr(candidate, "finish_reason", None))


def _text_from_parts(candidate: Any) -> str:
    """Every non-thought text part, joined.

    Joined rather than "the first one", because a long JSON object can arrive
    split across several text parts and taking only the first would truncate it
    into a parse failure that looks like the model's fault.

    `thought=True` parts are skipped: that is where the model's reasoning
    arrives, and it is not the answer.
    """
    if candidate is None:
        return ""

    content = getattr(candidate, "content", None)
    parts = list(getattr(content, "parts", None) or []) if content is not None else []

    texts = [
        part.text
        for part in parts
        if not getattr(part, "thought", False) and getattr(part, "text", None)
    ]
    return "".join(texts)


def _enum_name(value: Any) -> str | None:
    """An SDK enum as its bare name, tolerating a plain string or `None`."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _usage_of(usage: Any) -> dict[str, int]:
    """Token counts as a plain dict, under this module's own names.

    Gemini's field names are normalised to the ones `LLMResult` already used, so
    that an event log written before the provider swap and one written after are
    still comparable. The keys are part of what a caller sees; the provider's
    spelling of them is not.
    """
    if usage is None:
        return {}

    wanted = {
        "prompt_token_count": "input_tokens",
        "candidates_token_count": "output_tokens",
        "cached_content_token_count": "cache_read_input_tokens",
        "thoughts_token_count": "thinking_tokens",
    }
    counted = {}
    for source, name in wanted.items():
        value = getattr(usage, source, None)
        if isinstance(value, int):
            counted[name] = value
    return counted
