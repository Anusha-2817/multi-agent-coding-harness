"""Tests for llm_openrouter.py. Fake HTTP only (`httpx.MockTransport`): no network.

What these cannot show, because only a live call can: that OpenRouter accepts
this `response_format` for Google, how `reasoning.effort` reaches Gemini, and
whether the real usage fields look like the ones scripted here.
"""

import json
from pathlib import Path

import httpx
import pytest

from harness.agents.planner import Planner
from harness.events import EventLog
from harness.llm import LLMError, LLMRefusalError, LLMResponseError, LLMTransportError, LLMTruncatedError
from harness.state import Plan, TaskState
from llm_openrouter import MODEL, PROVIDER, URL, OpenRouterClient

KEY = "sk-or-test-SECRET-123"
FIXTURE = Path(__file__).resolve().parent.parent / "tasks" / "fixture_repo_1"
PLAN_JSON = json.dumps({"summary": "s", "steps": ["a"], "target_files": ["pricing/discounts.py"],
                        "constraints": []})
USAGE = {"prompt_tokens": 1000, "completion_tokens": 300,
         "prompt_tokens_details": {"cached_tokens": 50},
         "completion_tokens_details": {"reasoning_tokens": 200}}


def ok(content=PLAN_JSON, finish="stop", native="STOP", usage=USAGE, model="google/gemini-3.6-flash-001"):
    return httpx.Response(200, json={"model": model, "usage": usage, "choices": [
        {"message": {"role": "assistant", "content": content},
         "finish_reason": finish, "native_finish_reason": native}]})


def error(status, message="boom", headers=None):
    return httpx.Response(status, json={"error": {"code": status, "message": message}}, headers=headers)


class FakeAPI:
    """Hands out scripted responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index=0) -> dict:
        return json.loads(self.requests[index].content)


def client(api, **kwargs):
    sleeps = []
    c = OpenRouterClient(api_key=KEY, transport=httpx.MockTransport(api), sleep=sleeps.append, **kwargs)
    return c, sleeps


def ask(c, thinking_level="LOW"):
    return c.complete_structured(system="sys", user="usr", schema=Plan, max_tokens=16000,
                                 thinking_level=thinking_level)


class TestRequest:
    def test_body_pins_google_and_maps_reasoning(self):
        api = FakeAPI(ok())
        ask(client(api)[0])

        request, body = api.requests[0], api.body()
        assert str(request.url) == URL
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert body["model"] == MODEL == "google/gemini-3.6-flash"
        assert body["provider"] == PROVIDER == {"order": ["google-ai-studio"], "allow_fallbacks": False,
                                                "require_parameters": True}
        assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
        assert body["max_tokens"] == 16000
        assert body["response_format"]["json_schema"]["schema"] == Plan.model_json_schema()
        assert body["reasoning"] == {"effort": "low", "exclude": True}

    @pytest.mark.parametrize("level, effort", [("MINIMAL", "minimal"), ("MEDIUM", "medium"), ("HIGH", "high")])
    def test_each_thinking_level_maps_to_an_effort(self, level, effort):
        api = FakeAPI(ok())
        ask(client(api)[0], thinking_level=level)
        assert api.body()["reasoning"]["effort"] == effort

    def test_no_thinking_level_sends_no_reasoning(self):
        api = FakeAPI(ok())
        ask(client(api)[0], thinking_level=None)
        assert "reasoning" not in api.body()


class TestResult:
    def test_value_model_and_usage_in_the_log_payload_shape(self):
        result = ask(client(FakeAPI(ok()))[0])

        assert result.value == Plan.model_validate_json(PLAN_JSON)
        assert result.log_payload() == {
            "model": "google/gemini-3.6-flash-001",  # what the response reports
            "stop_reason": "STOP",
            "parse_attempts": 1,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 50,
                      "thinking_tokens": 200},
        }

    def test_one_repair_turn_then_success(self):
        api = FakeAPI(ok(content="not json"), ok())
        result = ask(client(api)[0])

        assert result.parse_attempts == 2
        roles = [m["role"] for m in api.body(1)["messages"]]
        assert roles == ["system", "user", "assistant", "user"]

    def test_two_bad_responses_raise(self):
        with pytest.raises(LLMResponseError):
            ask(client(FakeAPI(ok(content="nope"), ok(content="still nope")))[0])

    def test_truncation_raises_with_its_usage_and_is_not_repaired(self):
        api = FakeAPI(ok(content='{"summ', finish="length", native="MAX_TOKENS"))
        with pytest.raises(LLMTruncatedError) as caught:
            ask(client(api)[0])
        assert caught.value.call_payload["usage"]["thinking_tokens"] == 200
        assert caught.value.call_payload["stop_reason"] == "MAX_TOKENS"
        assert len(api.requests) == 1

    def test_a_blocked_response_is_a_refusal(self):
        with pytest.raises(LLMRefusalError):
            ask(client(FakeAPI(ok(content="", finish="content_filter", native="SAFETY")))[0])


class TestErrors:
    def test_402_is_not_retryable_and_not_retried(self):
        api = FakeAPI(error(402, "insufficient credits"))
        with pytest.raises(LLMTransportError) as caught:
            ask(client(api)[0])
        assert (caught.value.status_code, caught.value.retryable) == (402, False)
        assert len(api.requests) == 1

    def test_401_names_the_key_problem_without_the_key_and_is_not_retried(self):
        api = FakeAPI(error(401, f"No user for key {KEY}"))
        with pytest.raises(LLMTransportError) as caught:
            ask(client(api)[0])
        assert (caught.value.status_code, caught.value.retryable) == (401, False)
        assert "rejected the API key" in str(caught.value) and "OPENROUTER_API_KEY" in str(caught.value)
        assert KEY not in str(caught.value) and "[redacted]" in str(caught.value)
        assert len(api.requests) == 1

    def test_429_is_retried_with_retry_after_then_succeeds(self):
        api = FakeAPI(error(429, headers={"retry-after": "7"}), ok())
        c, sleeps = client(api)
        assert ask(c).parse_attempts == 1
        assert sleeps == [7.0]

    @pytest.mark.parametrize("status", [429, 503])
    def test_retryable_statuses_exhaust_into_an_infrastructure_error(self, status):
        api = FakeAPI(*[error(status)] * 4)
        c, sleeps = client(api, max_retries=3)
        with pytest.raises(LLMTransportError) as caught:
            ask(c)
        assert (caught.value.status_code, caught.value.retryable) == (status, True)
        assert len(api.requests) == 4 and len(sleeps) == 3

    def test_an_error_inside_a_200_is_treated_by_its_code(self):
        api = FakeAPI(*[httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}})] * 2)
        with pytest.raises(LLMTransportError) as caught:
            ask(client(api, max_retries=1)[0])
        assert (caught.value.status_code, caught.value.retryable) == (502, True)

    def test_400_is_not_retried(self):
        api = FakeAPI(error(400))
        with pytest.raises(LLMTransportError) as caught:
            ask(client(api)[0])
        assert caught.value.retryable is False and len(api.requests) == 1

    def test_a_connection_failure_is_retryable_with_no_status(self):
        api = FakeAPI(*[httpx.ConnectError("refused")] * 2)
        with pytest.raises(LLMTransportError) as caught:
            ask(client(api, max_retries=1)[0])
        assert (caught.value.status_code, caught.value.retryable) == (None, True)


class TestTheKey:
    def test_missing_key_is_an_llm_error(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            OpenRouterClient()

    def test_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
        api = FakeAPI(ok())
        OpenRouterClient(transport=httpx.MockTransport(api)).complete_structured(
            system="s", user="u", schema=Plan, max_tokens=10)
        assert api.requests[0].headers["authorization"] == f"Bearer {KEY}"

    def test_never_in_repr_errors_or_the_event_log(self, tmp_path):
        c, _ = client(FakeAPI(ok(), error(402), error(400)))
        assert KEY not in repr(c)

        # A real agent drives it, writing its normal llm_request/llm_response events.
        log = EventLog("openrouter_key_check", log_dir=tmp_path)
        state = TaskState(task_id="t", repo_path=str(FIXTURE), task_description="d", failure_input="f")
        assert Planner(log, c).run(state).plan is not None
        for _ in range(2):
            with pytest.raises(LLMTransportError) as caught:
                ask(c)
            assert KEY not in str(caught.value)

        text = log.path.read_text(encoding="utf-8")
        assert "llm_response" in text and KEY not in text
