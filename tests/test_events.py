"""Tests for harness/events.py.

The log is the harness's audit trail, so these cover the properties that make it
trustworthy: events land in order, every line is independently parseable, the
envelope is always complete, Pydantic payloads survive the trip, and nothing
truncates or drops what was already written.
"""

import json
from datetime import datetime

import pytest

from harness.events import PAYLOAD_REPR_LIMIT, EventLog
from harness.state import Plan, Status, TaskState, TesterFailure

TASK_ID = "fixture_repo_1_20260814T120000Z"


@pytest.fixture
def log(tmp_path) -> EventLog:
    return EventLog(TASK_ID, log_dir=tmp_path / "logs")


def read_lines(log: EventLog) -> list[dict]:
    """Parse the log the way a replay would: one JSON object per line."""
    text = log.path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class TestFileCreation:
    def test_creates_the_log_directory_if_absent(self, tmp_path):
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()

        EventLog(TASK_ID, log_dir=log_dir)

        assert log_dir.is_dir()

    def test_creates_nested_directories(self, tmp_path):
        EventLog(TASK_ID, log_dir=tmp_path / "a" / "b" / "logs")

        assert (tmp_path / "a" / "b" / "logs").is_dir()

    def test_log_file_is_named_for_the_task_id(self, log):
        assert log.path.name == f"{TASK_ID}.jsonl"

    def test_file_is_created_on_first_append(self, log):
        assert not log.path.exists()

        log.append(attempt=1, agent="harness", event="run_started", payload={})

        assert log.path.is_file()
        assert len(read_lines(log)) == 1

    def test_existing_directory_is_not_an_error(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        EventLog(TASK_ID, log_dir=log_dir)  # must not raise

        assert log_dir.is_dir()


class TestAppendOrder:
    def test_events_append_in_order_and_each_line_parses(self, log):
        for i in range(5):
            log.append(attempt=i, agent="planner", event=f"event_{i}", payload={"i": i})

        records = read_lines(log)

        assert len(records) == 5
        assert [r["event"] for r in records] == [f"event_{i}" for i in range(5)]
        assert [r["payload"]["i"] for r in records] == list(range(5))

    def test_timestamps_are_non_decreasing(self, log):
        for i in range(5):
            log.append(attempt=1, agent="harness", event=f"e{i}", payload={})

        stamps = [datetime.fromisoformat(r["ts"]) for r in read_lines(log)]

        assert stamps == sorted(stamps)

    def test_one_line_per_event_even_with_multiline_payloads(self, log):
        traceback = "Traceback:\n  line one\n  line two\nAssertionError\n"

        log.append(attempt=1, agent="tester", event="test_failed", payload={"tb": traceback})

        assert len(read_lines(log)) == 1
        assert read_lines(log)[0]["payload"]["tb"] == traceback

    def test_append_is_the_only_public_mutator(self):
        public = {name for name in vars(EventLog) if not name.startswith("_")}

        assert public == {"append"}


class TestEnvelope:
    def test_all_six_fields_present_with_correct_types(self, log):
        log.append(
            attempt=3,
            agent="implementer",
            event="edits_produced",
            payload={"count": 2},
        )

        record = read_lines(log)[0]

        assert set(record) == {"ts", "task_id", "attempt", "agent", "event", "payload"}
        assert isinstance(record["ts"], str)
        assert isinstance(record["task_id"], str)
        assert isinstance(record["attempt"], int)
        assert isinstance(record["agent"], str)
        assert isinstance(record["event"], str)
        assert isinstance(record["payload"], dict)

    def test_field_values_are_carried_through(self, log):
        log.append(attempt=3, agent="implementer", event="edits_produced", payload={"count": 2})

        record = read_lines(log)[0]

        assert record["task_id"] == TASK_ID
        assert record["attempt"] == 3
        assert record["agent"] == "implementer"
        assert record["event"] == "edits_produced"
        assert record["payload"] == {"count": 2}

    def test_ts_is_utc_and_iso_parseable(self, log):
        log.append(attempt=1, agent="harness", event="run_started", payload={})

        parsed = datetime.fromisoformat(read_lines(log)[0]["ts"])

        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0

    def test_empty_payload_is_allowed(self, log):
        log.append(attempt=1, agent="harness", event="run_started", payload={})

        assert read_lines(log)[0]["payload"] == {}


class TestPydanticPayloads:
    def test_plan_round_trips(self, log):
        plan = Plan(
            summary="Correct the off-by-one in add()",
            steps=["Read calculator.py", "Fix the return expression"],
            target_files=["calculator.py"],
            constraints=["Do not modify the tests"],
        )

        log.append(attempt=1, agent="planner", event="plan_produced", payload={"plan": plan})

        restored = Plan.model_validate(read_lines(log)[0]["payload"]["plan"])
        assert restored == plan

    def test_evidence_round_trips_with_its_discriminator(self, log):
        evidence = TesterFailure(
            failed_tests=["tests/test_calculator.py::test_add"],
            traceback="AssertionError: assert 5 == 6",
            stdout_tail="1 failed, 3 passed",
        )

        log.append(attempt=2, agent="harness", event="test_failed", payload={"evidence": evidence})

        logged = read_lines(log)[0]["payload"]["evidence"]
        assert logged["kind"] == "tester_failure"
        assert TesterFailure.model_validate(logged) == evidence

    def test_whole_task_state_round_trips(self, log):
        state = TaskState(
            task_id=TASK_ID,
            repo_path=f"runs/{TASK_ID}",
            task_description="Fix the failing test",
            failure_input="AssertionError: assert 5 == 6",
            status=Status.RUNNING,
        )

        log.append(attempt=1, agent="harness", event="state_snapshot", payload={"state": state})

        restored = TaskState.model_validate(read_lines(log)[0]["payload"]["state"])
        assert restored == state

    def test_status_enum_serialises_to_its_string_value(self, log):
        log.append(attempt=1, agent="harness", event="run_finished", payload={"status": Status.SUCCEEDED})

        assert read_lines(log)[0]["payload"]["status"] == "succeeded"

    def test_models_nested_in_lists_serialise(self, log):
        plans = [
            Plan(summary=f"p{i}", steps=[], target_files=[], constraints=[])
            for i in range(2)
        ]

        log.append(attempt=1, agent="planner", event="plans", payload={"plans": plans})

        logged = read_lines(log)[0]["payload"]["plans"]
        assert [p["summary"] for p in logged] == ["p0", "p1"]


class TestReopening:
    """Two EventLog instances for the same task_id must append, not truncate."""

    def test_second_instance_appends_rather_than_truncating(self, tmp_path):
        log_dir = tmp_path / "logs"

        first = EventLog(TASK_ID, log_dir=log_dir)
        first.append(attempt=1, agent="planner", event="first", payload={})

        second = EventLog(TASK_ID, log_dir=log_dir)
        second.append(attempt=1, agent="implementer", event="second", payload={})

        records = read_lines(second)
        assert [r["event"] for r in records] == ["first", "second"]

    def test_the_two_instances_address_the_same_file(self, tmp_path):
        log_dir = tmp_path / "logs"

        assert EventLog(TASK_ID, log_dir=log_dir).path == EventLog(TASK_ID, log_dir=log_dir).path

    def test_different_task_ids_get_different_files(self, tmp_path):
        log_dir = tmp_path / "logs"

        a = EventLog("run_a", log_dir=log_dir)
        b = EventLog("run_b", log_dir=log_dir)
        a.append(attempt=1, agent="harness", event="only_a", payload={})
        b.append(attempt=1, agent="harness", event="only_b", payload={})

        assert [r["event"] for r in read_lines(a)] == ["only_a"]
        assert [r["event"] for r in read_lines(b)] == ["only_b"]


class TestUnserialisablePayloads:
    """A payload that cannot be encoded must degrade the line, never drop the event."""

    def test_event_is_still_written(self, log):
        log.append(attempt=1, agent="harness", event="bad_payload", payload={"obj": object()})

        records = read_lines(log)

        assert len(records) == 1
        assert records[0]["event"] == "bad_payload"

    def test_envelope_survives_intact(self, log):
        log.append(attempt=4, agent="tester", event="bad_payload", payload={"obj": object()})

        record = read_lines(log)[0]

        assert set(record) == {"ts", "task_id", "attempt", "agent", "event", "payload"}
        assert record["attempt"] == 4
        assert record["agent"] == "tester"
        assert record["task_id"] == TASK_ID

    def test_failure_is_recorded_in_the_payload(self, log):
        log.append(attempt=1, agent="harness", event="bad_payload", payload={"obj": object()})

        payload = read_lines(log)[0]["payload"]

        assert "TypeError" in payload["_serialization_error"]
        assert "object" in payload["_payload_repr"]

    def test_append_does_not_raise(self, log):
        log.append(attempt=1, agent="harness", event="bad_payload", payload={"obj": object()})

    def test_payload_repr_is_capped(self, log):
        payload = {"junk": [object()] * 5000}

        log.append(attempt=1, agent="harness", event="bad_payload", payload=payload)

        assert len(read_lines(log)[0]["payload"]["_payload_repr"]) <= PAYLOAD_REPR_LIMIT

    def test_later_events_still_write_normally(self, log):
        log.append(attempt=1, agent="harness", event="bad_payload", payload={"obj": object()})
        log.append(attempt=1, agent="harness", event="good_payload", payload={"ok": True})

        records = read_lines(log)

        assert [r["event"] for r in records] == ["bad_payload", "good_payload"]
        assert records[1]["payload"] == {"ok": True}
