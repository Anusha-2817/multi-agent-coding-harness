"""Tests for the LLM client wrapper. No API key, no network, no sleeping.

The SDK is replaced wherever it is reached for. `LLMClient.__init__` builds a
real `genai.Client` -- which opens no connection -- and every test that gets as
far as a request swaps `client._sdk` for a fake. Reaching into a private
attribute is deliberate: the alternative is a constructor parameter for injecting
the transport, and CLAUDE.md's scope fence allows exactly one seam in v1 (the
approval gate). A test double does not need to become an architectural feature.

What is actually under test here is the half of this module that is ours:
the parse ladder, the repair turn, and the failure taxonomy. The SDK's retries
and backoff are the SDK's to test.

**Only this file changed when the provider did.** The doubles below are Gemini
shapes now -- candidates, parts, finish reasons -- and the assertions about the
request name Gemini's fields. Everything above them is the same test it was
under Anthropic, which is the check that the seam at `_create` held.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
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


def part(text: str, *, thought: bool = False) -> SimpleNamespace:
    return SimpleNamespace(text=text, thought=thought)


def thought_part() -> SimpleNamespace:
    """Where a 2.5-family model's reasoning arrives. Not the answer."""
    return SimpleNamespace(text="weighing the options", thought=True)


def envelope(
    text: str = "",
    *,
    finish_reason: str = "STOP",
    parts: list | None = None,
    usage: SimpleNamespace | None = None,
    block_reason: str | None = None,
    candidates: list | None = None,
) -> SimpleNamespace:
    """A `GenerateContentResponse` in the shape the client actually reads."""
    if candidates is None:
        candidate = SimpleNamespace(
            finish_reason=finish_reason,
            content=SimpleNamespace(parts=parts if parts is not None else [part(text)]),
        )
        candidates = [candidate]

    feedback = SimpleNamespace(block_reason=block_reason) if block_reason else None
    return SimpleNamespace(
        model_version="gemini-2.5-flash",
        candidates=candidates,
        prompt_feedback=feedback,
        usage_metadata=usage,
    )


def usage_metadata(**counts) -> SimpleNamespace:
    """Gemini's token counts, under Gemini's field names."""
    return SimpleNamespace(**counts)


class FakeSDK:
    """Hands out one scripted envelope per call, and records what it was sent.

    Scripted rather than smart, for the reason CLAUDE.md gives about the agent
    stubs: a fake that decided for itself when to return good JSON would make
    these tests a test of the fake.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"FakeSDK called {len(self.calls)} times with nothing left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def status_error(code: int, message: str = "nope") -> genai_errors.APIError:
    """A real SDK exception, built the way the SDK builds one.

    `ClientError` for 4xx and `ServerError` for 5xx, matching what the SDK
    raises -- the classification under test reads `.code`, so a stand-in with the
    wrong class would still pass and prove nothing.
    """
    status = "RESOURCE_EXHAUSTED" if code == 429 else "ERROR"
    body = {"error": {"code": code, "message": message, "status": status}}
    kind = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return kind(code, body)


@pytest.fixture(scope="module")
def client():
    """One client for the whole module, for the reason `test_loop.py` shares its
    scenarios: constructing one is expensive and nothing here mutates it.

    `genai.Client` costs about a second to build -- it sets up a trust store --
    and a function-scoped fixture made this module take a minute instead of a
    second. Sharing is safe because every test that issues a request calls
    `with_sdk` first, which replaces `_sdk` outright; a test needing different
    client *settings* (see `test_repairs_can_be_switched_off`) builds its own and
    pays the second.
    """
    return LLMClient(api_key="test-key-not-used")


def with_sdk(client, *responses) -> FakeSDK:
    sdk = FakeSDK(*responses)
    client._sdk = sdk
    return sdk


# -- construction ------------------------------------------------------------


class TestConstruction:
    def test_a_missing_api_key_fails_immediately(self, monkeypatch):
        """Before a fixture is copied, not after a Planner call."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        with pytest.raises(LLMError) as caught:
            LLMClient()

        assert "GEMINI_API_KEY" in str(caught.value)

    def test_the_environment_supplies_the_key_when_no_argument_does(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "from-the-environment")

        assert LLMClient().model == "gemini-2.5-flash"


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

        contents = sdk.calls[1]["contents"]
        # `assistant` on the way in, `model` on the wire: the role translation
        # lives in `_create` and nowhere else, which is what let
        # `complete_structured` survive the provider swap unchanged.
        assert [c.role for c in contents] == ["user", "model", "user"]
        assert contents[0].parts[0].text == "u"
        assert contents[1].parts[0].text == '{"name": "a"}'
        assert "count" in contents[2].parts[0].text

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
        sdk = with_sdk(client, envelope('{"name": "a", "cou', finish_reason="MAX_TOKENS"))

        with pytest.raises(LLMTruncatedError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=10)

        assert len(sdk.calls) == 1
        assert "max_output_tokens" in str(caught.value)

    def test_a_max_tokens_finish_with_no_text_still_raises_truncated(self, client):
        """The Gemini-specific shape of this: thinking is on by default and comes
        out of the same budget, so a too-small budget is spent thinking and
        returns MAX_TOKENS with nothing in it. Same diagnosis, same fix."""
        with_sdk(client, envelope(finish_reason="MAX_TOKENS", parts=[thought_part()]))

        with pytest.raises(LLMTruncatedError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=10)

        assert "thinking" in str(caught.value)

    @pytest.mark.parametrize(
        "finish_reason", ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"]
    )
    def test_a_blocked_output_raises_without_a_second_call(self, client, finish_reason):
        sdk = with_sdk(client, envelope("", finish_reason=finish_reason, parts=[]))

        with pytest.raises(LLMRefusalError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 1
        assert finish_reason in str(caught.value)

    def test_a_blocked_prompt_raises_without_a_second_call(self, client):
        """Gemini can block the *input*, which leaves no candidate at all.
        Anthropic had no equivalent; both mean the same thing here."""
        sdk = with_sdk(client, envelope(block_reason="SAFETY", candidates=[]))

        with pytest.raises(LLMRefusalError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert len(sdk.calls) == 1
        assert "blocked before generation" in str(caught.value)

    def test_a_finish_reason_we_do_not_recognise_takes_the_normal_path(self, client):
        """The rule is "reasons that recur identically raise". OTHER is not one
        of those -- it is unknown -- so it is not claimed to be."""
        with_sdk(client, envelope('{"name": "a", "count": 1}', finish_reason="OTHER"))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.value.count == 1

    def test_both_are_response_errors_so_a_caller_can_catch_one_type(self):
        assert issubclass(LLMTruncatedError, LLMResponseError)
        assert issubclass(LLMRefusalError, LLMResponseError)

    def test_a_truncated_response_keeps_the_partial_text(self, client):
        with_sdk(client, envelope('{"name": "a", "cou', finish_reason="MAX_TOKENS"))

        with pytest.raises(LLMTruncatedError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=10)

        assert caught.value.raw_text == '{"name": "a", "cou'

    def test_a_response_with_no_text_part_raises(self, client):
        with_sdk(client, envelope(parts=[thought_part()]))

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

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 413, 422])
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
        """`google-genai` does not wrap transport failures in an SDK class, so
        the catch is on the stdlib bases they inherit from."""
        with_sdk(client, ConnectionError("connection reset"))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.status_code is None
        assert caught.value.retryable is True

    def test_a_rate_limit_429_is_retryable(self, client):
        with_sdk(client, status_error(429, "Quota exceeded for requests per minute"))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.retryable is True

    def test_a_daily_quota_429_is_reported_as_not_worth_retrying(self, client):
        """Both arrive as RESOURCE_EXHAUSTED with code 429. The only thing
        separating "wait thirty seconds" from "wait until tomorrow" is the quota
        id in the message, so the message is what gets read."""
        with_sdk(
            client,
            status_error(429, "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"),
        )

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert caught.value.status_code == 429
        assert caught.value.retryable is False
        assert "daily allowance resets" in str(caught.value)

    def test_the_google_status_string_reaches_the_message(self, client):
        """`RESOURCE_EXHAUSTED` is often more informative than `429`."""
        with_sdk(client, status_error(429, "slow down"))

        with pytest.raises(LLMTransportError) as caught:
            client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert "RESOURCE_EXHAUSTED" in str(caught.value)

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
    def config_of(self, sdk, index=0):
        return sdk.calls[index]["config"]

    def test_the_schema_is_sent_as_a_response_json_schema(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        config = self.config_of(sdk)
        assert config.response_mime_type == "application/json"
        assert set(config.response_json_schema["properties"]) == {"name", "count"}

    def test_pydantics_schema_is_sent_unmodified(self, client):
        """No transform is needed: every keyword Pydantic emits for these models
        is on Gemini's supported list. If that stops being true, this is where
        it shows up."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert self.config_of(sdk).response_json_schema == Shape.model_json_schema()

    def test_extra_forbid_survives_as_additional_properties_false(self, client):
        """`additionalProperties` is on Gemini's supported list, so
        `extra="forbid"` stays load-bearing: an unexpected key still fails
        validation, and the repair turn still gets to name it."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert self.config_of(sdk).response_json_schema["additionalProperties"] is False

    def test_response_schema_is_not_also_set(self, client):
        """The SDK rejects both at once; `response_json_schema` is the one that
        takes raw JSON Schema."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert self.config_of(sdk).response_schema is None

    def test_no_sampling_parameters_are_sent(self, client):
        """Prompting is the steering mechanism here; a sampling knob would be one
        more thing to keep aligned across two agents."""
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        config = self.config_of(sdk)
        assert config.temperature is None
        assert config.top_p is None
        assert config.top_k is None

    def test_thinking_is_left_at_the_models_default(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert self.config_of(sdk).thinking_config is None

    def test_max_tokens_becomes_max_output_tokens(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=4321)

        assert self.config_of(sdk).max_output_tokens == 4321

    def test_the_system_prompt_is_sent_as_a_system_instruction(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(
            system="the system prompt", user="the user turn", schema=Shape, max_tokens=100
        )

        contents = sdk.calls[0]["contents"]
        assert self.config_of(sdk).system_instruction == "the system prompt"
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "the user turn"

    def test_the_model_id_is_sent(self, client):
        sdk = with_sdk(client, envelope('{"name": "a", "count": 1}'))

        client.complete_structured(system="s", user="u", schema=Shape, max_tokens=100)

        assert sdk.calls[0]["model"] == "gemini-2.5-flash"


class TestTheResult:
    def test_the_text_is_found_past_a_thought_part(self, client):
        """Thinking is on by default on the 2.5 family and arrives as parts
        flagged `thought=True`. Indexing `parts[0]` is the easiest way to write a
        client that appears to receive nothing."""
        with_sdk(
            client,
            envelope(parts=[thought_part(), part('{"name": "a", "count": 7}')]),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.value.count == 7

    def test_text_split_across_several_parts_is_joined(self, client):
        """A long JSON object can arrive in pieces. Taking only the first would
        truncate it into a parse failure that looks like the model's fault."""
        with_sdk(
            client,
            envelope(parts=[part('{"name": "a",'), part(' "count": 3}')]),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.value.count == 3

    def test_the_finish_reason_is_carried_out_as_the_stop_reason(self, client):
        """`LLMResult.stop_reason` keeps its name and meaning across the provider
        swap; only where it is read from moved."""
        with_sdk(client, envelope('{"name": "a", "count": 1}', finish_reason="STOP"))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.stop_reason == "STOP"

    def test_the_served_model_version_is_preferred_over_the_configured_id(self, client):
        with_sdk(client, envelope('{"name": "a", "count": 1}'))

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.model == "gemini-2.5-flash"

    def test_usage_is_carried_out_for_the_agent_to_log(self, client):
        """Gemini's field names are normalised to the ones `LLMResult` already
        used, so a log written before the provider swap still lines up with one
        written after."""
        with_sdk(
            client,
            envelope(
                '{"name": "a", "count": 1}',
                usage=usage_metadata(
                    prompt_token_count=120,
                    candidates_token_count=30,
                    thoughts_token_count=44,
                ),
            ),
        )

        result = client.complete_structured(
            system="s", user="u", schema=Shape, max_tokens=100
        )

        assert result.usage == {
            "input_tokens": 120,
            "output_tokens": 30,
            "thinking_tokens": 44,
        }

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
