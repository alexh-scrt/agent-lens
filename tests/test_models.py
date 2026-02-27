"""Unit tests for Pydantic model validation and EventType serialization.

Covers:
- EventType enum values and string coercion.
- AgentEvent field defaults, validation, and serialization.
- EventFilter validation and pagination constraints.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent_lens.models import AgentEvent, EventFilter, EventType


# ---------------------------------------------------------------------------
# EventType tests
# ---------------------------------------------------------------------------


class TestEventType:
    """Tests for the EventType enumeration."""

    def test_all_members_are_strings(self) -> None:
        """Every EventType value should be a non-empty lowercase string."""
        for member in EventType:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()

    def test_expected_members_exist(self) -> None:
        """Verify the expected six event types are defined."""
        expected = {
            "file_create",
            "file_modify",
            "file_delete",
            "file_read",
            "shell_command",
            "network_call",
        }
        actual = {m.value for m in EventType}
        assert actual == expected

    def test_construct_from_string(self) -> None:
        """EventType should be constructable from its string value."""
        assert EventType("file_create") is EventType.FILE_CREATE
        assert EventType("network_call") is EventType.NETWORK_CALL

    def test_invalid_string_raises(self) -> None:
        """Constructing EventType from an unknown string should raise ValueError."""
        with pytest.raises(ValueError):
            EventType("unknown_type")

    def test_str_representation(self) -> None:
        """The str() of an EventType should be its value string."""
        assert str(EventType.FILE_MODIFY) == "file_modify"


# ---------------------------------------------------------------------------
# AgentEvent tests
# ---------------------------------------------------------------------------


class TestAgentEvent:
    """Tests for AgentEvent model validation and serialization."""

    def test_minimal_file_event(self) -> None:
        """A file event with only required fields should be valid."""
        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/foo.py")
        assert event.path == "/tmp/foo.py"
        assert event.event_type == EventType.FILE_CREATE.value
        assert isinstance(event.id, str)
        assert len(event.id) == 36  # UUID4 length with hyphens
        assert event.metadata == {}

    def test_id_is_unique_uuid(self) -> None:
        """Each event should receive a unique auto-generated UUID."""
        e1 = AgentEvent(event_type=EventType.FILE_READ, path="/a")
        e2 = AgentEvent(event_type=EventType.FILE_READ, path="/a")
        assert e1.id != e2.id
        # Both should be valid UUID4s
        uuid.UUID(e1.id, version=4)
        uuid.UUID(e2.id, version=4)

    def test_timestamp_defaults_to_utc_now(self) -> None:
        """Auto-generated timestamps should be timezone-aware UTC."""
        before = datetime.now(timezone.utc)
        event = AgentEvent(event_type=EventType.FILE_MODIFY, path="/tmp/x")
        after = datetime.now(timezone.utc)
        assert event.timestamp.tzinfo is not None
        assert before <= event.timestamp <= after

    def test_explicit_timestamp_naive_becomes_utc(self) -> None:
        """A naive datetime passed as timestamp should be treated as UTC."""
        naive = datetime(2024, 1, 15, 12, 0, 0)
        event = AgentEvent(
            event_type=EventType.FILE_DELETE,
            path="/tmp/gone",
            timestamp=naive,
        )
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.year == 2024

    def test_timestamp_from_iso_string(self) -> None:
        """A timestamp passed as an ISO string should be parsed correctly."""
        iso = "2024-06-01T10:00:00+00:00"
        event = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/tmp/f",
            timestamp=iso,
        )
        assert event.timestamp.year == 2024
        assert event.timestamp.month == 6

    def test_explicit_id_is_preserved(self) -> None:
        """A custom id should not be overridden."""
        custom_id = "my-custom-id-123"
        event = AgentEvent(
            id=custom_id,
            event_type=EventType.SHELL_COMMAND,
            command_line="ls -la",
        )
        assert event.id == custom_id

    def test_shell_command_event(self) -> None:
        """A shell_command event does not require a path."""
        event = AgentEvent(
            event_type=EventType.SHELL_COMMAND,
            command_line="git status",
            process_name="bash",
            process_pid=12345,
        )
        assert event.command_line == "git status"
        assert event.process_name == "bash"
        assert event.process_pid == 12345
        assert event.path is None

    def test_network_call_event(self) -> None:
        """A network_call event requires remote_host."""
        event = AgentEvent(
            event_type=EventType.NETWORK_CALL,
            remote_host="api.openai.com",
            remote_port=443,
            process_name="python",
        )
        assert event.remote_host == "api.openai.com"
        assert event.remote_port == 443

    def test_network_call_missing_remote_host_raises(self) -> None:
        """A network_call event without remote_host must fail validation."""
        with pytest.raises(ValidationError) as exc_info:
            AgentEvent(event_type=EventType.NETWORK_CALL)
        assert "remote_host" in str(exc_info.value)

    def test_file_event_missing_path_raises(self) -> None:
        """File-type events without a path must fail validation."""
        for event_type in (
            EventType.FILE_CREATE,
            EventType.FILE_MODIFY,
            EventType.FILE_DELETE,
            EventType.FILE_READ,
        ):
            with pytest.raises(ValidationError):
                AgentEvent(event_type=event_type)  # no path

    def test_invalid_process_pid_raises(self) -> None:
        """A process_pid of zero or negative must fail validation."""
        with pytest.raises(ValidationError):
            AgentEvent(
                event_type=EventType.SHELL_COMMAND,
                command_line="ls",
                process_pid=0,
            )

    def test_invalid_remote_port_raises(self) -> None:
        """A remote_port outside 1-65535 must fail validation."""
        with pytest.raises(ValidationError):
            AgentEvent(
                event_type=EventType.NETWORK_CALL,
                remote_host="example.com",
                remote_port=99999,
            )

    def test_metadata_stores_arbitrary_data(self) -> None:
        """Metadata field should accept arbitrary JSON-serializable values."""
        meta = {"size_bytes": 1024, "encoding": "utf-8", "tags": ["a", "b"]}
        event = AgentEvent(
            event_type=EventType.FILE_MODIFY,
            path="/tmp/big.py",
            metadata=meta,
        )
        assert event.metadata == meta

    def test_to_sse_dict_contains_all_fields(self) -> None:
        """to_sse_dict() should return a dict with all expected top-level keys."""
        event = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/tmp/new.py",
            process_name="aider",
            process_pid=999,
        )
        d = event.to_sse_dict()
        expected_keys = {
            "id", "event_type", "timestamp", "path", "process_name",
            "process_pid", "command_line", "remote_host", "remote_port", "metadata",
        }
        assert expected_keys.issubset(d.keys())

    def test_to_sse_dict_timestamp_is_iso_string(self) -> None:
        """The timestamp in to_sse_dict() should be an ISO 8601 string."""
        event = AgentEvent(event_type=EventType.FILE_READ, path="/tmp/r")
        d = event.to_sse_dict()
        assert isinstance(d["timestamp"], str)
        # Should be parseable
        parsed = datetime.fromisoformat(d["timestamp"])
        assert parsed.tzinfo is not None

    def test_model_dump_event_type_is_string(self) -> None:
        """Because use_enum_values=True, event_type in model_dump should be a str."""
        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/x")
        d = event.model_dump()
        assert isinstance(d["event_type"], str)
        assert d["event_type"] == "file_create"


# ---------------------------------------------------------------------------
# EventFilter tests
# ---------------------------------------------------------------------------


class TestEventFilter:
    """Tests for the EventFilter query schema."""

    def test_default_filter_has_sensible_defaults(self) -> None:
        """An EventFilter with no arguments should have safe defaults."""
        f = EventFilter()
        assert f.limit == 100
        assert f.offset == 0
        assert f.event_type is None
        assert f.process_name is None
        assert f.path_prefix is None
        assert f.since is None
        assert f.until is None

    def test_limit_upper_bound(self) -> None:
        """Limit must not exceed 1000."""
        with pytest.raises(ValidationError):
            EventFilter(limit=1001)

    def test_limit_lower_bound(self) -> None:
        """Limit must be at least 1."""
        with pytest.raises(ValidationError):
            EventFilter(limit=0)

    def test_offset_non_negative(self) -> None:
        """Offset must be non-negative."""
        with pytest.raises(ValidationError):
            EventFilter(offset=-1)

    def test_event_type_string_coercion(self) -> None:
        """event_type should accept a string and coerce it to EventType."""
        f = EventFilter(event_type="file_write")  # type: ignore[arg-type]
        # Pydantic will raise if the string is not a valid EventType
        # (file_write does not exist) — let's use a valid one

    def test_event_type_valid_string(self) -> None:
        """A valid event_type string should be accepted."""
        f = EventFilter(event_type="file_create")  # type: ignore[arg-type]
        assert f.event_type == EventType.FILE_CREATE

    def test_event_type_invalid_string_raises(self) -> None:
        """An invalid event_type string should raise ValidationError."""
        with pytest.raises(ValidationError):
            EventFilter(event_type="not_a_real_type")  # type: ignore[arg-type]

    def test_since_naive_datetime_becomes_utc(self) -> None:
        """A naive datetime for 'since' should be treated as UTC."""
        naive = datetime(2024, 3, 1, 0, 0, 0)
        f = EventFilter(since=naive)
        assert f.since is not None
        assert f.since.tzinfo is not None

    def test_until_iso_string_parsed(self) -> None:
        """An ISO string for 'until' should be parsed into a datetime."""
        f = EventFilter(until="2024-12-31T23:59:59+00:00")  # type: ignore[arg-type]
        assert f.until is not None
        assert f.until.year == 2024
        assert f.until.month == 12

    def test_all_fields_accepted(self) -> None:
        """All filter fields should be settable without error."""
        f = EventFilter(
            event_type=EventType.SHELL_COMMAND,
            process_name="aider",
            path_prefix="/home/user",
            path_contains="src",
            since=datetime(2024, 1, 1, tzinfo=timezone.utc),
            until=datetime(2024, 12, 31, tzinfo=timezone.utc),
            limit=50,
            offset=10,
        )
        assert f.limit == 50
        assert f.offset == 10
        assert f.process_name == "aider"
