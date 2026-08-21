"""Tests for the LLM client wrapper. No API key, no network, no sleeping.

The SDK is replaced wherever it is reached for. `LLMClient.__init__` builds a
real `anthropic.Anthropic` -- which opens no connection -- and every test that
gets as far as a request swaps `client._sdk` for a fake. Reaching into a private
attribute is deliberate: the alternative is a constructor parameter for injecting
the transport, and CLAUDE.md's scope fence allows exactly one seam in v1 (the
approval gate). A test double does not need to become an architectural feature.

What is actually under test here is the half of this module that is ours:
the parse ladder, the repair turn, and the failure taxonomy. The SDK's retries
and backoff are the SDK's to test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from harness.llm import (
    RETRYABLE_STATUS,
    LLMClient,
    LLMError,
    LLMRefusalError,
    LLMResponseError,
    LLMTransportError,
    LLMTruncatedError,
    describe_problems,
    extract_json,
    repair_prompt,
)

# -- doubles -----------------------------------------------------------------


class Shape(BaseModel):
    """A stand-in schema. `extra="forbid"` matches every model in state.py."""

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block() -> SimpleNamespace:
    """What arrives at content[0] on a thinking model, with display omitted."""
    return SimpleNamespace(type="thinking", thinking="")


def envelope(
    text: str = "",
    *,
    stop_reason: str = "end_turn",
    blocks: list | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-opus-5",
        stop_reason=stop_reason,
        content=blocks if blocks is not None else [text_block(text)],
        usage=usage,
    )


class FakeSDK:
    """Hands out one scripted envelope per call, and records what it was sent.

    Scripted rather than smart, for the reason CLAUDE.md gives about the agent
    stubs: a fake that decided for itself when to return good JSON would make
    these tests a test of the fake.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"FakeSDK called {len(self.calls)} times with nothing left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def status_error(code: int) -> anthropic.APIStatusError:
    """A real SDK exception, built the way the SDK builds one."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(code, request=request, json={"error": {"message": "nope"}})
    return anthropic.APIStatusError("nope", response=response, body=None)


@pytest.fixture
def client():
    return LLMClient(api_key="test-key-not-used")


def with_sdk(client, *responses) -> FakeSDK:
    sdk = FakeSDK(*responses)
    client._sdk = sdk
    return sdk


# -- construction ------------------------------------------------------------


class TestConstruction:
    def test_a_missing_api_key_fails_immediately(self, monkeypatch):
        """Before a fixture is copied, not after a Planner call."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(LLMError) as caught:
            LLMClient()

        assert "ANTHROPIC_API_KEY" in str(caught.value)

    def test_the_environment_supplies_the_key_when_no_argument_does(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")

        assert LLMClient().model == "claude-opus-5"


# -- the parse ladder --------------------------------------------------------


class TestTheParseLadder:
    def test_step_one_reads_a_bare_json_object(self):
        value, how = extract_json('{"name": "a", "count": 1}')

        assert how == "direct"
        assert value == {"name": "a", "count": 1}

    def test_step_one_tolerates_surrounding_whitespace(self):
        value, how = extract_json('\n\n  {"name": "a", "count": 1}\n  ')

        assert how == "direct"
        assert value == {"name": "a", "count": 1}

    def test_step_two_reads_a_fenced_block(self):
        text = 'Here is the plan:\n\n```json\n{"name": "a", "count": 1}\n```\n'

        value, how = extract_json(text)

        assert how == "fenced"
        assert value == {"name": "a", "count": 1}

    def test_step_two_handles_a_fence_with_no_language_tag(self):
        value, how = extract_json('```\n{"name": "a", "count": 1}\n```')

        assert how == "fenced"
        assert value == {"name": "a", "count": 1}

    def test_step_three_slices_from_the_first_brace_to_the_last(self):
        text = 'Sure! {"name": "a", "count": 1} Let me know if you need more.'

        value, how = extract_json(text)

        assert how == "braces"
        assert value == {"name": "a", "count": 1}

    def test_the_steps_are_tried_in_order(self):
        """A response that is valid JSON on its own never reaches the fence
        handling, so a `{` inside a string cannot hijack the parse."""
        value, how = extract_json('{"name": "```json", "count": 1}')

        assert how == "direct"
        assert value["name"] == "```json"

    def test_nothing_json_shaped_raises_rather_than_guessing(self):
        with pytest.raises(LLMResponseError) as caught:
            extract_json("I'd be happy to help with that!")

        assert caught.value.stage == "extract"
        assert caught.value.raw_text == "I'd be happy to help with that!"

    def test_the_raw_text_is_kept_whole_on_failure(self):
        """It is the only evidence of what went wrong; a truncated copy is a
        debugging dead end."""
        text = "prose " * 500

        with pytest.raises(LLMResponseError) as caught:
            extract_json(text)

        assert caught.value.raw_text == text

    def test_an_empty_response_raises(self):
        with pytest.raises(LLMResponseError):
            extract_json("")

    def test_the_ladder_does_not_alter_the_json_it_finds(self):
        """It decides where the JSON starts and stops. It changes no bytes."""
        payload = {"name": "a\r\nb", "count": 1, "nested": {"unicode": "é"}}
        text = f"```json\n{json.dumps(payload)}\n```"

        value, _how = extract_json(text)

        assert value == payload


# -- the repair turn ---------------------------------------------------------


class TestRepairTurnConstruction:
    def validation_error(self, payload) -> ValidationError:
        with pytest.raises(ValidationError) as caught:
            Shape.model_validate(payload)
        return caught.value

    def test_a_missing_field_is_named(self):
        problems = describe_problems(self.validation_error({"name": "a"}))

        assert "count" in problems
        assert "required" in problems.lower()

    def test_a_wrong_type_is_named(self):
        problems = describe_problems(self.validation_error({"name": "a", "count": "many"}))

        assert "count" in problems

    def test_an_unexpected_key_is_named(self):
        """`extra="forbid"` everywhere means a helpfully-added key is a hard
        failure. Naming the key is the whole repair."""
        error = self.validation_error({"name": "a", "count": 1, "notes": "hope this helps"})

        problems = describe_problems(error)

        assert "notes" in problems

    def test_a_nested_location_renders_as_a_dotted_path(self):
        class Outer(BaseModel):
            model_config = ConfigDict(extra="forbid")
            inner: Shape

        with pytest.raises(ValidationError) as caught:
            Outer.model_validate({"inner": {"name": "a"}})

        assert "inner.count" in describe_problems(caught.value)

    def test_a_json_decode_error_reports_where_it_broke(self):
        try:
            json.loads("{oops}")
        except json.JSONDecodeError as exc:
            problems = describe_problems(exc)

        assert "not valid JSON" in problems
        assert "column" in problems

    def test_the_repair_prompt_carries_the_problems_verbatim(self):
        problems = describe_problems(self.validation_error({"name": "a"}))

        prompt = repair_prompt(problems)

        assert problems in prompt

    def test_the_repair_prompt_asks_for_json_only(self):
        prompt = repair_prompt("- count: Field required")

        assert "nothing else" in prompt
        assert "fence" in prompt


class TestTheRepairRoundTrip:
    def test_a_validation_failure_triggers_exactly_one_repair(self, client):
        sdk = with_sdk(
            client,
            envelope('{"name": "a"}'),
            envelope('{"name": "a", "count": 2}'),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.value == Shape(name="a", count=2)
        assert result.parse_attempts == 2
        assert len(sdk.calls) == 2

    def test_the_repair_call_carries_the_bad_response_and_the_error(self, client):
        sdk = with_sdk(
            client,
            envelope('{"name": "a"}'),
            envelope('{"name": "a", "count": 2}'),
        )

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        messages = sdk.calls[1]["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[0]["content"] == "u"
        assert messages[1]["content"] == '{"name": "a"}'
        assert "count" in messages[2]["content"]

    def test_a_second_failure_raises_rather_than_repairing_again(self, client):
        sdk = with_sdk(client, envelope("nope"), envelope("still nope"))

        with pytest.raises(LLMResponseError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 2
        assert caught.value.raw_text == "still nope"

    def test_a_first_time_success_costs_one_call(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.parse_attempts == 1
        assert len(sdk.calls) == 1

    def test_repairs_can_be_switched_off(self):
        client = LLMClient(api_key="k", max_parse_retries=0)
        sdk = with_sdk(client, envelope("prose"))

        with pytest.raises(LLMResponseError):
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 1


# -- stop reasons that must not be repaired ----------------------------------


class TestStopReasonsThatAreNotRepaired:
    def test_max_tokens_raises_without_a_second_call(self, client):
        """The repair would truncate at exactly the same place. The fix is a
        bigger budget, not another round trip."""
        sdk = with_sdk(client, envelope('{"name": "a", "cou', stop_reason="max_tokens"))

        with pytest.raises(LLMTruncatedError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=10)

        assert len(sdk.calls) == 1
        assert "max_tokens" in str(caught.value)

    def test_a_refusal_raises_without_a_second_call(self, client):
        sdk = with_sdk(client, envelope("", stop_reason="refusal", blocks=[]))

        with pytest.raises(LLMRefusalError):
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 1

    def test_both_are_response_errors_so_a_caller_can_catch_one_type(self):
        assert issubclass(LLMTruncatedError, LLMResponseError)
        assert issubclass(LLMRefusalError, LLMResponseError)

    def test_a_truncated_response_keeps_the_partial_text(self, client):
        with_sdk(client, envelope('{"name": "a", "cou', stop_reason="max_tokens"))

        with pytest.raises(LLMTruncatedError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=10)

        assert caught.value.raw_text == '{"name": "a", "cou'

    def test_a_response_with_no_text_block_raises(self, client):
        with_sdk(client, envelope(blocks=[thinking_block()]))

        with pytest.raises(LLMResponseError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.stage == "empty"


# -- transport ---------------------------------------------------------------


class TestStatusMapping:
    @pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS))
    def test_a_retryable_status_is_marked_retryable(self, client, code):
        with_sdk(client, status_error(code))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.status_code == code
        assert caught.value.retryable is True
        assert "unavailable" in str(caught.value)

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 413, 422])
    def test_a_non_retryable_status_says_the_request_is_the_problem(self, client, code):
        """Retrying a 400 sends the same bytes and fails identically. The
        message has to send you to the request, not to the network."""
        with_sdk(client, status_error(code))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.status_code == code
        assert caught.value.retryable is False
        assert "not retryable" in str(caught.value)

    def test_a_connection_failure_has_no_status_and_is_retryable(self, client):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        with_sdk(client, anthropic.APIConnectionError(request=request))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.status_code is None
        assert caught.value.retryable is True

    def test_a_transport_failure_is_never_repaired(self, client):
        """A repair turn answers a bad *response*. There was no response."""
        sdk = with_sdk(client, status_error(500), envelope('{"name": "a", "count": 1}'))

        with pytest.raises(LLMTransportError):
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 1

    def test_transport_and_response_errors_share_one_base(self):
        assert issubclass(LLMTransportError, LLMError)
        assert issubclass(LLMResponseError, LLMError)


# -- the request, and what comes back ----------------------------------------


class TestTheRequest:
    def test_the_schema_is_sent_as_a_structured_output_format(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        fmt = sdk.calls[0]["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert set(fmt["schema"]["properties"]) == {"name", "count"}

    def test_extra_forbid_becomes_additional_properties_false(self, client):
        """The models were written with `extra="forbid"` for the harness's own
        reasons; structured outputs happens to require exactly that."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert sdk.calls[0]["output_config"]["format"]["schema"]["additionalProperties"] is False

    def test_no_sampling_parameters_are_sent(self, client):
        """They are rejected outright on this model."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert not {"temperature", "top_p", "top_k"} & set(sdk.calls[0])

    def test_the_system_prompt_is_sent_separately_from_the_user_turn(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(
            system="the system prompt", user="the user turn", schema=Shape, max_tokens=100
        )

        assert sdk.calls[0]["system"] == "the system prompt"
        assert sdk.calls[0]["messages"] == [{"role": "user", "content": "the user turn"}]


class TestTheResult:
    def test_the_text_block_is_found_past_a_thinking_block(self, client):
        """Thinking is on by default, so `content[0]` is a thinking block whose
        text is empty. Indexing position zero is the easiest way to write a
        client that appears to receive nothing."""
        with_sdk(
            client,
            envelope(blocks=[thinking_block(), text_block('{"name": "a", "count": 7}')]),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.value.count == 7

    def test_usage_is_carried_out_for_the_agent_to_log(self, client):
        with_sdk(
            client,
            envelope(
                '{"name": "a", "count": 1}',
                usage=SimpleNamespace(input_tokens=120, output_tokens=30),
            ),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.usage == {"input_tokens": 120, "output_tokens": 30}

    def test_a_missing_usage_object_is_not_an_error(self, client):
        with_sdk(client, envelope('{"name": "a", "count": 1}'))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.usage == {}

    def test_the_log_payload_omits_the_produced_value(self, client):
        """The base class's `agent_produced` already carries it, and CLAUDE.md
        forbids a subclass re-logging what it produced."""
        with_sdk(client, envelope('{"name": "unmistakable-value", "count": 1}'))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )
        payload = result.log_payload()

        assert set(payload) == {"model", "stop_reason", "parse_attempts", "usage"}
        assert "unmistakable-value" not in json.dumps(payload)
