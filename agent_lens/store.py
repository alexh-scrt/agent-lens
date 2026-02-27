"""SQLite-backed event store for AgentLens.

This module provides a thread-safe, persistent event store built on Python's
built-in ``sqlite3`` module. It supports:

- Inserting ``AgentEvent`` instances.
- Querying events with optional filtering via ``EventFilter``.
- Retrieving a single event by its UUID.
- Counting stored events.
- Clearing all stored events (useful for testing).

The store is designed to be safe for concurrent access from multiple threads.
All public methods acquire a reentrant lock around database operations.

Example usage::

    store = EventStore(db_path="agent_lens.db")
    store.initialize()

    event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/test.py")
    store.insert(event)

    events = store.query(EventFilter(event_type=EventType.FILE_CREATE))
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from agent_lens.models import AgentEvent, EventFilter, EventType

logger = logging.getLogger(__name__)

# DDL for the events table
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    path        TEXT,
    process_name TEXT,
    process_pid INTEGER,
    command_line TEXT,
    remote_host TEXT,
    remote_port INTEGER,
    metadata    TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_INDEX_TIMESTAMP_SQL = """
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
"""

_CREATE_INDEX_EVENT_TYPE_SQL = """
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);
"""

_CREATE_INDEX_PROCESS_SQL = """
CREATE INDEX IF NOT EXISTS idx_events_process_name ON events (process_name);
"""

_INSERT_SQL = """
INSERT INTO events (
    id, event_type, timestamp, path, process_name, process_pid,
    command_line, remote_host, remote_port, metadata
) VALUES (
    :id, :event_type, :timestamp, :path, :process_name, :process_pid,
    :command_line, :remote_host, :remote_port, :metadata
);
"""


class EventStore:
    """Thread-safe SQLite-backed store for ``AgentEvent`` records.

    Args:
        db_path: Path to the SQLite database file. Pass ``":memory:"`` for an
            in-memory database (useful for testing).
    """

    def __init__(self, db_path: str | Path = "agent_lens.db") -> None:
        """Initialize the store with the given database path.

        Does *not* open a database connection; call :meth:`initialize` first.

        Args:
            db_path: Filesystem path to the SQLite file, or ``":memory:"``.
        """
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the database connection and create the schema if needed.

        This method is idempotent; calling it multiple times is safe.

        Raises:
            sqlite3.Error: If the database cannot be opened or the schema
                cannot be created.
        """
        with self._lock:
            if self._conn is not None:
                return
            logger.info("Opening event store at %s", self._db_path)
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._apply_schema()

    def close(self) -> None:
        """Close the database connection.

        After calling this method the store must not be used further without
        calling :meth:`initialize` again.
        """
        with self._lock:
            if self._conn is not None:
                logger.info("Closing event store")
                self._conn.close()
                self._conn = None

    def _apply_schema(self) -> None:
        """Create database tables and indexes if they do not already exist."""
        conn = self._require_connection()
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_INDEX_TIMESTAMP_SQL)
        conn.execute(_CREATE_INDEX_EVENT_TYPE_SQL)
        conn.execute(_CREATE_INDEX_PROCESS_SQL)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that wraps operations in an explicit transaction.

        Yields:
            The open :class:`sqlite3.Connection`.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.Error: On any database error (triggers rollback).
        """
        conn = self._require_connection()
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _require_connection(self) -> sqlite3.Connection:
        """Return the active connection or raise if not initialized.

        Returns:
            The active :class:`sqlite3.Connection`.

        Raises:
            RuntimeError: If :meth:`initialize` has not been called.
        """
        if self._conn is None:
            raise RuntimeError(
                "EventStore has not been initialized. Call initialize() first."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert(self, event: AgentEvent) -> None:
        """Persist a single ``AgentEvent`` to the store.

        Args:
            event: The event to store. Its ``id`` field must be unique.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.IntegrityError: If an event with the same ``id`` already exists.
            sqlite3.Error: On any other database error.
        """
        row = _event_to_row(event)
        with self._transaction() as conn:
            conn.execute(_INSERT_SQL, row)
        logger.debug("Inserted event %s (%s)", event.id, event.event_type)

    def insert_many(self, events: list[AgentEvent]) -> None:
        """Persist multiple ``AgentEvent`` instances in a single transaction.

        Args:
            events: The events to store.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.IntegrityError: If any event id collides with an existing one.
            sqlite3.Error: On any other database error.
        """
        if not events:
            return
        rows = [_event_to_row(e) for e in events]
        with self._transaction() as conn:
            conn.executemany(_INSERT_SQL, rows)
        logger.debug("Inserted %d events", len(events))

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def query(self, event_filter: Optional[EventFilter] = None) -> list[AgentEvent]:
        """Query stored events with optional filtering.

        Events are returned in ascending timestamp order.

        Args:
            event_filter: An :class:`~agent_lens.models.EventFilter` specifying
                filter criteria and pagination.  If ``None``, up to 100 events
                are returned.

        Returns:
            A list of matching :class:`~agent_lens.models.AgentEvent` instances.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.Error: On any database error.
        """
        if event_filter is None:
            event_filter = EventFilter()

        sql, params = _build_query(event_filter)
        with self._lock:
            conn = self._require_connection()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        return [_row_to_event(row) for row in rows]

    def get_by_id(self, event_id: str) -> Optional[AgentEvent]:
        """Retrieve a single event by its UUID.

        Args:
            event_id: The UUID string of the event to retrieve.

        Returns:
            The matching :class:`~agent_lens.models.AgentEvent`, or ``None`` if
            no event with that ID exists.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.Error: On any database error.
        """
        with self._lock:
            conn = self._require_connection()
            cursor = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return _row_to_event(row)

    def count(self, event_filter: Optional[EventFilter] = None) -> int:
        """Count stored events matching an optional filter.

        Args:
            event_filter: Optional filter criteria. If ``None``, counts all events.

        Returns:
            The number of matching events.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.Error: On any database error.
        """
        if event_filter is None:
            event_filter = EventFilter(limit=1, offset=0)

        # Build the WHERE clause from the filter, then wrap in COUNT
        _, params_with_pagination = _build_query(event_filter)
        where_sql, where_params = _build_where_clause(event_filter)

        sql = f"SELECT COUNT(*) FROM events{where_sql}"  # noqa: S608
        with self._lock:
            conn = self._require_connection()
            cursor = conn.execute(sql, where_params)
            row = cursor.fetchone()

        return row[0] if row else 0

    def clear(self) -> None:
        """Delete all events from the store.

        This is primarily intended for use in tests.

        Raises:
            RuntimeError: If the store has not been initialized.
            sqlite3.Error: On any database error.
        """
        with self._transaction() as conn:
            conn.execute("DELETE FROM events")
        logger.debug("Cleared all events from store")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _event_to_row(event: AgentEvent) -> dict:
    """Convert an ``AgentEvent`` to a flat dictionary for SQLite insertion.

    Args:
        event: The event to convert.

    Returns:
        A dictionary whose keys match the ``events`` table columns.
    """
    return {
        "id": event.id,
        "event_type": event.event_type if isinstance(event.event_type, str) else event.event_type.value,
        "timestamp": event.timestamp.isoformat(),
        "path": event.path,
        "process_name": event.process_name,
        "process_pid": event.process_pid,
        "command_line": event.command_line,
        "remote_host": event.remote_host,
        "remote_port": event.remote_port,
        "metadata": json.dumps(event.metadata),
    }


def _row_to_event(row: sqlite3.Row) -> AgentEvent:
    """Reconstruct an ``AgentEvent`` from a SQLite row.

    Args:
        row: A :class:`sqlite3.Row` object from the ``events`` table.

    Returns:
        The deserialized :class:`~agent_lens.models.AgentEvent`.

    Raises:
        ValueError: If the row contains invalid data that fails Pydantic validation.
    """
    metadata_raw = row["metadata"]
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        logger.warning("Failed to parse metadata JSON for event %s", row["id"])
        metadata = {}

    timestamp_str = row["timestamp"]
    try:
        timestamp = datetime.fromisoformat(timestamp_str)
    except ValueError:
        logger.warning("Unrecognized timestamp format '%s'; using now()", timestamp_str)
        timestamp = datetime.now(timezone.utc)

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return AgentEvent(
        id=row["id"],
        event_type=EventType(row["event_type"]),
        timestamp=timestamp,
        path=row["path"],
        process_name=row["process_name"],
        process_pid=row["process_pid"],
        command_line=row["command_line"],
        remote_host=row["remote_host"],
        remote_port=row["remote_port"],
        metadata=metadata,
    )


def _build_where_clause(f: EventFilter) -> tuple[str, list]:
    """Build the WHERE clause and parameter list from an ``EventFilter``.

    Args:
        f: The filter to translate into SQL predicates.

    Returns:
        A tuple of ``(where_sql, params)`` where ``where_sql`` is either an
        empty string or a string beginning with " WHERE", and ``params`` is
        the list of positional bind parameters.
    """
    clauses: list[str] = []
    params: list = []

    if f.event_type is not None:
        val = f.event_type if isinstance(f.event_type, str) else f.event_type.value
        clauses.append("event_type = ?")
        params.append(val)

    if f.process_name is not None:
        clauses.append("process_name = ?")
        params.append(f.process_name)

    if f.path_prefix is not None:
        clauses.append("path LIKE ?")
        # Escape special LIKE characters in the prefix itself
        escaped = f.path_prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        params.append(f"{escaped}%")

    if f.path_contains is not None:
        clauses.append("path LIKE ? ESCAPE '!'")
        escaped = f.path_contains.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        params.append(f"%{escaped}%")

    if f.since is not None:
        clauses.append("timestamp >= ?")
        params.append(f.since.isoformat())

    if f.until is not None:
        clauses.append("timestamp <= ?")
        params.append(f.until.isoformat())

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


def _build_query(f: EventFilter) -> tuple[str, list]:
    """Build a complete SELECT query from an ``EventFilter``.

    Args:
        f: The filter specifying criteria and pagination.

    Returns:
        A tuple of ``(sql, params)`` ready for ``sqlite3.Connection.execute``.
    """
    where_sql, params = _build_where_clause(f)
    sql = f"SELECT * FROM events{where_sql} ORDER BY timestamp ASC LIMIT ? OFFSET ?"  # noqa: S608
    params.append(f.limit)
    params.append(f.offset)
    return sql, params
