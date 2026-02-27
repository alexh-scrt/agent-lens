"""Pydantic models for AgentLens events and query schemas.

This module defines the core data structures used throughout AgentLens:

- ``EventType``: An enum of all recognized agent event categories.
- ``AgentEvent``: The primary event model representing a single observed action.
- ``EventFilter``: A query/filter schema for searching the event store.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class EventType(str, Enum):
    """Enumeration of all recognized agent event types.

    Each variant corresponds to a distinct observable action that an AI coding
    agent may perform on the local machine.
    """

    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    FILE_READ = "file_read"
    SHELL_COMMAND = "shell_command"
    NETWORK_CALL = "network_call"


class AgentEvent(BaseModel):
    """Represents a single observed action performed by an AI coding agent.

    Attributes:
        id: Unique identifier for the event (auto-generated UUID).
        event_type: The category of action observed.
        timestamp: UTC timestamp when the event was captured.
        path: Filesystem path associated with the event (file or directory).
        process_name: Name of the process that triggered the event.
        process_pid: PID of the process that triggered the event.
        command_line: Full command line string for shell_command events.
        remote_host: Remote host for network_call events.
        remote_port: Remote port for network_call events.
        metadata: Arbitrary additional key-value metadata.
    """

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique event identifier (UUID4)",
    )
    event_type: EventType = Field(
        ...,
        description="The category of action observed",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the event was captured",
    )
    path: Optional[str] = Field(
        default=None,
        description="Filesystem path associated with the event",
    )
    process_name: Optional[str] = Field(
        default=None,
        description="Name of the process that triggered the event",
    )
    process_pid: Optional[int] = Field(
        default=None,
        description="PID of the process that triggered the event",
        ge=1,
    )
    command_line: Optional[str] = Field(
        default=None,
        description="Full command line string (for shell_command events)",
    )
    remote_host: Optional[str] = Field(
        default=None,
        description="Remote host for network_call events",
    )
    remote_port: Optional[int] = Field(
        default=None,
        description="Remote port for network_call events",
        ge=1,
        le=65535,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary additional key-value metadata",
    )

    model_config = {
        "json_encoders": {
            datetime: lambda v: v.isoformat(),
        },
        "use_enum_values": True,
    }

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Ensure the timestamp is timezone-aware (UTC)."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
        return v

    @model_validator(mode="after")
    def validate_event_type_fields(self) -> "AgentEvent":
        """Validate that event-type-specific fields are appropriately populated."""
        if self.event_type in (
            EventType.FILE_CREATE,
            EventType.FILE_MODIFY,
            EventType.FILE_DELETE,
            EventType.FILE_READ,
        ):
            if not self.path:
                raise ValueError(
                    f"'path' is required for event type '{self.event_type}'"
                )
        if self.event_type == EventType.NETWORK_CALL:
            if not self.remote_host:
                raise ValueError(
                    "'remote_host' is required for 'network_call' events"
                )
        return self

    def to_sse_dict(self) -> dict[str, Any]:
        """Serialize the event to a dictionary suitable for SSE JSON payload.

        Returns:
            A JSON-serializable dictionary with ISO-formatted timestamps.
        """
        data = self.model_dump()
        data["timestamp"] = self.timestamp.isoformat()
        return data


class EventFilter(BaseModel):
    """Query and filter schema for searching the AgentLens event store.

    All fields are optional; omitting a field means no filtering on that axis.

    Attributes:
        event_type: Filter to events of this specific type.
        process_name: Filter to events from this process name.
        path_prefix: Filter to events whose path starts with this string.
        path_contains: Filter to events whose path contains this substring.
        since: Return only events at or after this UTC datetime.
        until: Return only events at or before this UTC datetime.
        limit: Maximum number of events to return (default 100, max 1000).
        offset: Number of events to skip for pagination.
    """

    event_type: Optional[EventType] = Field(
        default=None,
        description="Filter by event type",
    )
    process_name: Optional[str] = Field(
        default=None,
        description="Filter by process name (exact match)",
    )
    path_prefix: Optional[str] = Field(
        default=None,
        description="Filter by path prefix",
    )
    path_contains: Optional[str] = Field(
        default=None,
        description="Filter by path substring",
    )
    since: Optional[datetime] = Field(
        default=None,
        description="Return events at or after this UTC datetime",
    )
    until: Optional[datetime] = Field(
        default=None,
        description="Return events at or before this UTC datetime",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of events to return",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of events to skip for pagination",
    )

    @field_validator("since", "until", mode="before")
    @classmethod
    def ensure_aware(cls, v: Any) -> Optional[datetime]:
        """Ensure datetime filter fields are timezone-aware."""
        if v is None:
            return v
        if isinstance(v, str):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
