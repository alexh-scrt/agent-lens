"""Unit tests for the SQLite-backed event store.

Covers:
- Initialization and schema creation.
- Inserting single and multiple events.
- Querying with EventFilter (event_type, process_name, path_prefix, etc.).
- get_by_id retrieval.
- count() with and without filters.
- clear() operation.
- Thread-safety: concurrent inserts from multiple threads.
- Error handling: operating before initialization.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest

from agent_lens.models import AgentEvent, EventFilter, EventType
from agent_lens.store import EventStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> Generator[EventStore, None, None]:
    """Provide an initialized in-memory EventStore for each test."""
    s = EventStore(db_path=":memory:")
    s.initialize()
    yield s
    s.close()


def make_file_event(
    event_type: EventType = EventType.FILE_CREATE,
    path: str = "/tmp/test.py",
    process_name: str = "aider",
    process_pid: int = 1000,
    **kwargs,
) -> AgentEvent:
    """Factory helper to create a file-type AgentEvent."""
    return AgentEvent(
        event_type=event_type,
        path=path,
        process_name=process_name,
        process_pid=process_pid,
        **kwargs,
    )


def make_shell_event(
    command_line: str = "git status",
    process_name: str = "bash",
    process_pid: int = 2000,
    **kwargs,
) -> AgentEvent:
    """Factory helper to create a shell_command AgentEvent."""
    return AgentEvent(
        event_type=EventType.SHELL_COMMAND,
        command_line=command_line,
        process_name=process_name,
        process_pid=process_pid,
        **kwargs,
    )


def make_network_event(
    remote_host: str = "api.openai.com",
    remote_port: int = 443,
    process_name: str = "python",
    **kwargs,
) -> AgentEvent:
    """Factory helper to create a network_call AgentEvent."""
    return AgentEvent(
        event_type=EventType.NETWORK_CALL,
        remote_host=remote_host,
        remote_port=remote_port,
        process_name=process_name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestEventStoreInitialization:
    """Tests for store lifecycle (init/close)."""

    def test_uninitialized_insert_raises(self) -> None:
        """Inserting before initialize() should raise RuntimeError."""
        s = EventStore(db_path=":memory:")
        event = make_file_event()
        with pytest.raises(RuntimeError, match="initialize"):
            s.insert(event)

    def test_uninitialized_query_raises(self) -> None:
        """Querying before initialize() should raise RuntimeError."""
        s = EventStore(db_path=":memory:")
        with pytest.raises(RuntimeError, match="initialize"):
            s.query()

    def test_double_initialize_is_safe(self) -> None:
        """Calling initialize() twice should not raise."""
        s = EventStore(db_path=":memory:")
        s.initialize()
        s.initialize()  # second call should be a no-op
        s.close()

    def test_close_before_initialize_is_safe(self) -> None:
        """Calling close() on an uninitialized store should not raise."""
        s = EventStore(db_path=":memory:")
        s.close()  # should be a no-op

    def test_query_after_close_raises(self) -> None:
        """Querying after close() should raise RuntimeError."""
        s = EventStore(db_path=":memory:")
        s.initialize()
        s.close()
        with pytest.raises(RuntimeError, match="initialize"):
            s.query()


# ---------------------------------------------------------------------------
# Insert tests
# ---------------------------------------------------------------------------


class TestEventStoreInsert:
    """Tests for single and bulk insert operations."""

    def test_insert_single_event(self, store: EventStore) -> None:
        """Inserting one event should result in count() == 1."""
        event = make_file_event()
        store.insert(event)
        assert store.count() == 1

    def test_insert_returns_none(self, store: EventStore) -> None:
        """insert() should not return a value."""
        event = make_file_event()
        result = store.insert(event)
        assert result is None

    def test_insert_duplicate_id_raises(self, store: EventStore) -> None:
        """Inserting two events with the same id should raise."""
        event = make_file_event()
        store.insert(event)
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            store.insert(event)  # same id

    def test_insert_many_empty_list(self, store: EventStore) -> None:
        """insert_many([]) should succeed and not change the count."""
        store.insert_many([])
        assert store.count() == 0

    def test_insert_many_events(self, store: EventStore) -> None:
        """insert_many() should persist all provided events."""
        events = [make_file_event(path=f"/tmp/file{i}.py") for i in range(5)]
        store.insert_many(events)
        assert store.count() == 5

    def test_insert_all_event_types(self, store: EventStore) -> None:
        """Each EventType should be persistable."""
        events = [
            make_file_event(event_type=EventType.FILE_CREATE, path="/a"),
            make_file_event(event_type=EventType.FILE_MODIFY, path="/b"),
            make_file_event(event_type=EventType.FILE_DELETE, path="/c"),
            make_file_event(event_type=EventType.FILE_READ, path="/d"),
            make_shell_event(),
            make_network_event(),
        ]
        store.insert_many(events)
        assert store.count() == 6

    def test_insert_event_with_metadata(self, store: EventStore) -> None:
        """Events with metadata dicts should round-trip correctly."""
        meta = {"size_bytes": 512, "encoding": "utf-8"}
        event = make_file_event(metadata=meta)
        store.insert(event)
        retrieved = store.get_by_id(event.id)
        assert retrieved is not None
        assert retrieved.metadata == meta

    def test_insert_event_with_empty_metadata(self, store: EventStore) -> None:
        """An event with no metadata should store and retrieve an empty dict."""
        event = make_file_event()
        store.insert(event)
        retrieved = store.get_by_id(event.id)
        assert retrieved is not None
        assert retrieved.metadata == {}


# ---------------------------------------------------------------------------
# get_by_id tests
# ---------------------------------------------------------------------------


class TestEventStoreGetById:
    """Tests for single-event retrieval by UUID."""

    def test_get_existing_event(self, store: EventStore) -> None:
        """get_by_id() should return the matching event."""
        event = make_file_event(path="/unique/path.py")
        store.insert(event)
        result = store.get_by_id(event.id)
        assert result is not None
        assert result.id == event.id
        assert result.path == "/unique/path.py"

    def test_get_nonexistent_event(self, store: EventStore) -> None:
        """get_by_id() should return None for an unknown id."""
        result = store.get_by_id("does-not-exist")
        assert result is None

    def test_round_trip_all_fields(self, store: EventStore) -> None:
        """All fields of an event should survive a store/retrieve cycle."""
        ts = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        event = AgentEvent(
            id="round-trip-id",
            event_type=EventType.NETWORK_CALL,
            timestamp=ts,
            remote_host="example.com",
            remote_port=8080,
            process_name="curl",
            process_pid=7777,
            command_line=None,
            metadata={"tls": True},
        )
        store.insert(event)
        retrieved = store.get_by_id("round-trip-id")
        assert retrieved is not None
        assert retrieved.id == "round-trip-id"
        assert retrieved.event_type == EventType.NETWORK_CALL.value
        assert retrieved.remote_host == "example.com"
        assert retrieved.remote_port == 8080
        assert retrieved.process_name == "curl"
        assert retrieved.process_pid == 7777
        assert retrieved.metadata == {"tls": True}
        assert retrieved.timestamp.year == 2024
        assert retrieved.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# Query / filter tests
# ---------------------------------------------------------------------------


class TestEventStoreQuery:
    """Tests for query() with various EventFilter configurations."""

    def _seed_mixed_events(self, store: EventStore) -> None:
        """Insert a diverse set of events for filter testing."""
        base_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        events = [
            make_file_event(
                event_type=EventType.FILE_CREATE,
                path="/proj/src/main.py",
                process_name="aider",
                timestamp=base_time,
            ),
            make_file_event(
                event_type=EventType.FILE_MODIFY,
                path="/proj/src/utils.py",
                process_name="aider",
                timestamp=base_time + timedelta(seconds=1),
            ),
            make_file_event(
                event_type=EventType.FILE_DELETE,
                path="/proj/tests/old_test.py",
                process_name="python",
                timestamp=base_time + timedelta(seconds=2),
            ),
            make_shell_event(
                command_line="pip install -r requirements.txt",
                process_name="aider",
                timestamp=base_time + timedelta(seconds=3),
            ),
            make_network_event(
                remote_host="api.openai.com",
                process_name="python",
                timestamp=base_time + timedelta(seconds=4),
            ),
        ]
        store.insert_many(events)

    def test_query_no_filter_returns_all(self, store: EventStore) -> None:
        """query() with no filter should return all events (up to default limit)."""
        self._seed_mixed_events(store)
        results = store.query()
        assert len(results) == 5

    def test_query_returns_none_filter_uses_default(self, store: EventStore) -> None:
        """Passing None to query() should behave like an empty filter."""
        self._seed_mixed_events(store)
        results = store.query(None)
        assert len(results) == 5

    def test_query_by_event_type(self, store: EventStore) -> None:
        """Filtering by event_type should return only matching events."""
        self._seed_mixed_events(store)
        f = EventFilter(event_type=EventType.FILE_CREATE)
        results = store.query(f)
        assert len(results) == 1
        assert all(r.event_type == EventType.FILE_CREATE.value for r in results)

    def test_query_by_process_name(self, store: EventStore) -> None:
        """Filtering by process_name should return only matching events."""
        self._seed_mixed_events(store)
        f = EventFilter(process_name="aider")
        results = store.query(f)
        assert len(results) == 3  # FILE_CREATE, FILE_MODIFY, SHELL_COMMAND
        assert all(r.process_name == "aider" for r in results)

    def test_query_by_path_prefix(self, store: EventStore) -> None:
        """path_prefix filter should match events whose path starts with prefix."""
        self._seed_mixed_events(store)
        f = EventFilter(path_prefix="/proj/src")
        results = store.query(f)
        assert len(results) == 2
        for r in results:
            assert r.path is not None
            assert r.path.startswith("/proj/src")

    def test_query_by_path_contains(self, store: EventStore) -> None:
        """path_contains filter should match events whose path contains the substring."""
        self._seed_mixed_events(store)
        f = EventFilter(path_contains="tests")
        results = store.query(f)
        assert len(results) == 1
        assert results[0].path is not None
        assert "tests" in results[0].path

    def test_query_by_since(self, store: EventStore) -> None:
        """'since' filter should exclude events before the cutoff."""
        self._seed_mixed_events(store)
        cutoff = datetime(2024, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        f = EventFilter(since=cutoff)
        results = store.query(f)
        # Events at seconds 2, 3, 4 → 3 results
        assert len(results) == 3
        for r in results:
            assert r.timestamp >= cutoff

    def test_query_by_until(self, store: EventStore) -> None:
        """'until' filter should exclude events after the cutoff."""
        self._seed_mixed_events(store)
        cutoff = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        f = EventFilter(until=cutoff)
        results = store.query(f)
        # Events at seconds 0 and 1 → 2 results
        assert len(results) == 2
        for r in results:
            assert r.timestamp <= cutoff

    def test_query_since_and_until(self, store: EventStore) -> None:
        """Combined since+until should narrow to a time window."""
        self._seed_mixed_events(store)
        since = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        until = datetime(2024, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
        f = EventFilter(since=since, until=until)
        results = store.query(f)
        # Events at seconds 1, 2, 3 → 3 results
        assert len(results) == 3

    def test_query_results_ordered_by_timestamp(self, store: EventStore) -> None:
        """Results should be in ascending timestamp order."""
        self._seed_mixed_events(store)
        results = store.query()
        timestamps = [r.timestamp for r in results]
        assert timestamps == sorted(timestamps)

    def test_query_limit(self, store: EventStore) -> None:
        """Limit should cap the number of results returned."""
        self._seed_mixed_events(store)
        f = EventFilter(limit=2)
        results = store.query(f)
        assert len(results) == 2

    def test_query_offset(self, store: EventStore) -> None:
        """Offset should skip the specified number of results."""
        self._seed_mixed_events(store)
        all_results = store.query()
        f = EventFilter(offset=2)
        paged_results = store.query(f)
        assert len(paged_results) == 3
        assert paged_results[0].id == all_results[2].id

    def test_query_limit_and_offset(self, store: EventStore) -> None:
        """Limit and offset together should implement basic pagination."""
        self._seed_mixed_events(store)
        all_results = store.query()
        f = EventFilter(limit=2, offset=1)
        paged = store.query(f)
        assert len(paged) == 2
        assert paged[0].id == all_results[1].id
        assert paged[1].id == all_results[2].id

    def test_query_empty_store_returns_empty_list(self, store: EventStore) -> None:
        """Querying an empty store should return an empty list."""
        results = store.query()
        assert results == []

    def test_query_combined_type_and_process(self, store: EventStore) -> None:
        """Combining event_type and process_name should apply both filters."""
        self._seed_mixed_events(store)
        f = EventFilter(event_type=EventType.FILE_MODIFY, process_name="aider")
        results = store.query(f)
        assert len(results) == 1
        assert results[0].event_type == EventType.FILE_MODIFY.value
        assert results[0].process_name == "aider"

    def test_query_no_match_returns_empty(self, store: EventStore) -> None:
        """A filter that matches nothing should return an empty list."""
        self._seed_mixed_events(store)
        f = EventFilter(process_name="nonexistent_process")
        results = store.query(f)
        assert results == []


# ---------------------------------------------------------------------------
# Count tests
# ---------------------------------------------------------------------------


class TestEventStoreCount:
    """Tests for the count() method."""

    def test_count_empty_store(self, store: EventStore) -> None:
        """count() on empty store should return 0."""
        assert store.count() == 0

    def test_count_after_inserts(self, store: EventStore) -> None:
        """count() should reflect the number of inserted events."""
        for i in range(7):
            store.insert(make_file_event(path=f"/tmp/f{i}.py"))
        assert store.count() == 7

    def test_count_with_filter(self, store: EventStore) -> None:
        """count() with a filter should count only matching events."""
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/a"))
        store.insert(make_file_event(event_type=EventType.FILE_MODIFY, path="/b"))
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/c"))
        f = EventFilter(event_type=EventType.FILE_CREATE)
        assert store.count(f) == 2

    def test_count_none_filter(self, store: EventStore) -> None:
        """count(None) should count all events."""
        store.insert(make_file_event(path="/x"))
        store.insert(make_file_event(path="/y"))
        assert store.count(None) == 2


# ---------------------------------------------------------------------------
# Clear tests
# ---------------------------------------------------------------------------


class TestEventStoreClear:
    """Tests for the clear() operation."""

    def test_clear_empties_store(self, store: EventStore) -> None:
        """After clear(), count() should return 0."""
        store.insert(make_file_event(path="/a"))
        store.insert(make_file_event(path="/b"))
        assert store.count() == 2
        store.clear()
        assert store.count() == 0

    def test_clear_allows_reinsert(self, store: EventStore) -> None:
        """After clear(), new events can be inserted without conflict."""
        event = make_file_event(path="/a")
        store.insert(event)
        store.clear()
        # Re-insert the same event (same id) — should succeed after clear
        store.insert(event)
        assert store.count() == 1

    def test_clear_on_empty_store_is_safe(self, store: EventStore) -> None:
        """clear() on an already empty store should not raise."""
        store.clear()
        assert store.count() == 0


# ---------------------------------------------------------------------------
# Thread-safety tests
# ---------------------------------------------------------------------------


class TestEventStoreThreadSafety:
    """Tests for concurrent access from multiple threads."""

    def test_concurrent_inserts(self, store: EventStore) -> None:
        """Multiple threads inserting simultaneously should not cause data loss."""
        num_threads = 10
        events_per_thread = 20
        errors: list[Exception] = []

        def insert_batch(thread_id: int) -> None:
            try:
                for i in range(events_per_thread):
                    event = make_file_event(path=f"/tmp/t{thread_id}_f{i}.py")
                    store.insert(event)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=insert_batch, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent inserts: {errors}"
        assert store.count() == num_threads * events_per_thread

    def test_concurrent_read_write(self, store: EventStore) -> None:
        """Concurrent reads and writes should not raise exceptions."""
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for i in range(50):
                    store.insert(make_file_event(path=f"/tmp/rw{i}.py"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(50):
                    store.query(EventFilter(limit=10))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent read/write: {errors}"
