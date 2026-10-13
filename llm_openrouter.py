"""An OpenRouter backend with the same interface as `harness.llm.LLMClient`.

Not part of the harness: nothing in `harness/` imports it. `driver.py --backend
openrouter` hands one to the arms in place of `LLMClient`, and the agents cannot
tell the difference -- same `complete_structured` signature, same `LLMResult`,
same error types.

**Reused from `harness.llm`, not reimplemented:** the parse ladder and
validation (`_validate`), the repair prompt, the error classes, `LLMResult`, and
the refusal finish reasons. Only the transport differs.

**Provider pinning.** The model is `google/gemini-3.6-flash`, routed only to
Google AI Studio (`order=["google-ai-studio"]`, `allow_fallbacks=False`), the
same service the Google backend calls. `require_parameters=True` refuses any
provider that would silently drop `response_format` or `reasoning`.

**`thinking_level` -> `reasoning.effort`**, lower-cased: MINIMAL -> "minimal",
LOW -> "low", MEDIUM -> "medium", HIGH -> "high"; None sends no `reasoning` field,
leaving the provider default, as the Google backend does. Reasoning text is
excluded from the response (`exclude=True`); its tokens are still counted.
Whether OpenRouter passes "low" through as Gemini's thinking level or turns it
into a token budget is not documented for this model and has not been measured.

**Usage**, in `LLMResult.usage`'s existing key names: `input_tokens` =
`prompt_tokens`; `thinking_tokens` = `completion_tokens_details.reasoning_tokens`;
`output_tokens` = `completion_tokens` minus the reasoning tokens (assumed to be
included in `completion_tokens`, so the split matches Gemini's separate counts);
`cache_read_input_tokens` = `prompt_tokens_details.cached_tokens`. `model` is
the model name the response reports.

**Errors.** 401 (bad, revoked or missing key) and 402 (credits exhausted) is `LLMTransportError(retryable=False)`,
both of which stop the driver like the daily quota. 408, 429 and 5xx are retried here
with exponential backoff and jitter (Retry-After honoured), then raised as
`retryable=True` -- infrastructure, to the driver. Other 4xx are non-retryable.

**The key** is read from `OPENROUTER_API_KEY`, held only in the request
header, and never put in a message, a payload, or a repr. Error text from
the API is scrubbed of it before it reaches an exception, in case a response
ever echoes it back.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx
from pydantic import BaseModel

from harness.llm import (
    DEFAULT_MAX_PARSE_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    REFUSAL_FINISH_REASONS,
    LLMError,
    LLMRefusalError,
    LLMResponseError,
    LLMResult,
    LLMTransportError,
    LLMTruncatedError,
    _validate,
    repair_prompt,
)

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-3.6-flash"
PROVIDER = {"order": ["google-ai-studio"], "allow_fallbacks": False, "require_parameters": True}
KEY_ENV = "OPENROUTER_API_KEY"
UNAUTHORIZED = 401
CREDITS_EXHAUSTED = 402
REDACTED = "[redacted]"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
EFFORT = {"MINIMAL": "minimal", "LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0


class OpenRouterClient:
    """Text in, validated Pydantic object out, over OpenRouter's chat completions API."""

    def __init__(
        self,
        *,
        model: str = MODEL,
        api_key: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        max_parse_retries: int = DEFAULT_MAX_PARSE_RETRIES,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        key = api_key or os.environ.get(KEY_ENV)
        if not key:
            raise LLMError(f"no API key: pass api_key= or set {KEY_ENV}.")
        self.model = model
        self.max_retries = max_retries
        self.max_parse_retries = max_parse_retries
        self._sleep = sleep
        self._key = key  # kept only so error text can be scrubbed of it
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    def __repr__(self) -> str:  # never show the key
        return f"OpenRouterClient(model={self.model!r})"

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> LLMResult:
        messages: list[dict[str, str]] = [{"role": "user", "content": user}]
        for parse_attempt in range(1, self.max_parse_retries + 2):
            body = self._post(self.request_body(system, messages, schema, max_tokens, thinking_level))
            metadata = metadata_of(body)
            # Outside the repair try, as in LLMClient: neither is repairable.
            try:
                text = text_of(body)
            except LLMTruncatedError as exc:
                exc.call_payload = {**metadata, "parse_attempts": parse_attempt}
                raise

            try:
                value = _validate(text, schema)
            except LLMResponseError as exc:
                if parse_attempt > self.max_parse_retries:
                    raise
                messages = [*messages, {"role": "assistant", "content": text},
                            {"role": "user", "content": repair_prompt(exc.problems)}]
                continue
            return LLMResult(value=value, parse_attempts=parse_attempt, **metadata)
        raise AssertionError("complete_structured fell out of its retry loop")

    def request_body(self, system, messages, schema, max_tokens, thinking_level) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True,
                                "schema": schema.model_json_schema()},
            },
            "provider": PROVIDER,
        }
        if thinking_level is not None:
            body["reasoning"] = {"effort": EFFORT[thinking_level.upper()], "exclude": True}
        return body

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One request with HTTP retries. Returns the parsed body or raises LLMTransportError."""
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.post(URL, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    self._sleep(_backoff(attempt, None))
                    continue
                raise LLMTransportError(
                    f"connection failed after {attempt + 1} attempt(s): {type(exc).__name__}",
                    status_code=None, retryable=True) from None

            status = response.status_code
            body = _json_or_none(response)
            # An error can also arrive inside a 200, as a body-level `error`.
            if status == 200 and isinstance(body, dict) and "error" in body:
                status = _int_or_none(body["error"].get("code")) or 502
            if status == 200 and isinstance(body, dict):
                return body

            detail = self._scrub(_error_message(body) or response.reason_phrase)
            if status in RETRYABLE_STATUS and attempt < self.max_retries:
                self._sleep(_backoff(attempt, response.headers.get("retry-after")))
                continue
            if status == UNAUTHORIZED:
                raise LLMTransportError(
                    f"HTTP 401: OpenRouter rejected the API key -- it is wrong, revoked or "
                    f"missing. Check {KEY_ENV} and run again: {detail}",
                    status_code=status, retryable=False)
            if status == CREDITS_EXHAUSTED:
                raise LLMTransportError(
                    f"HTTP 402: OpenRouter credits exhausted; nothing will succeed until they "
                    f"are topped up: {detail}", status_code=status, retryable=False)
            if status in RETRYABLE_STATUS:
                raise LLMTransportError(
                    f"HTTP {status} after {attempt + 1} attempt(s); the API is unavailable "
                    f"rather than the request being wrong: {detail}", status_code=status, retryable=True)
            raise LLMTransportError(
                f"HTTP {status}, which is not retryable: {detail}", status_code=status, retryable=False)
        raise AssertionError("_post fell out of its retry loop")

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, REDACTED) if self._key else text


# -- response reading --------------------------------------------------------


def metadata_of(body: dict[str, Any]) -> dict[str, Any]:
    """`model`, `stop_reason` and `usage`, in `LLMResult`'s names."""
    choice = (body.get("choices") or [{}])[0]
    return {
        "model": body.get("model") or MODEL,
        "stop_reason": choice.get("native_finish_reason") or choice.get("finish_reason"),
        "usage": usage_of(body.get("usage")),
    }


def usage_of(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    completion = usage.get("completion_tokens")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    counted = {}
    if isinstance(usage.get("prompt_tokens"), int):
        counted["input_tokens"] = usage["prompt_tokens"]
    if isinstance(completion, int):
        counted["output_tokens"] = completion - (reasoning if isinstance(reasoning, int) else 0)
    if isinstance(cached, int):
        counted["cache_read_input_tokens"] = cached
    if isinstance(reasoning, int):
        counted["thinking_tokens"] = reasoning
    return counted


def text_of(body: dict[str, Any]) -> str:
    """The message text, or the right error for a truncated or blocked response."""
    choices = body.get("choices") or []
    if not choices:
        raise LLMResponseError("the response had no choices", stage="response", raw_text="")
    choice = choices[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    native = choice.get("native_finish_reason")
    if finish == "length" or native == "MAX_TOKENS":
        raise LLMTruncatedError(
            f"the response hit max_tokens ({native or finish}); thinking shares the budget, so "
            f"raise max_tokens", stage="response", raw_text=text)
    if finish == "content_filter" or native in REFUSAL_FINISH_REASONS:
        raise LLMRefusalError(f"the response was blocked ({native or finish})",
                              stage="response", raw_text=text)
    return text


def _backoff(attempt: int, retry_after: str | None) -> float:
    try:
        if retry_after is not None:
            return min(float(retry_after), BACKOFF_CAP)
    except ValueError:
        pass
    return min(BACKOFF_BASE * 2 ** attempt, BACKOFF_CAP) * random.uniform(0.5, 1.0)


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _error_message(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message", ""))[:300]
    return ""
