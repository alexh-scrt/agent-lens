"""Integration tests for the filesystem watcher and event bus.

Covers:
- ``AgentLensEventHandler``: ignore patterns, event type mapping, path normalisation.
- ``FileSystemWatcher``: lifecycle (start/stop), invalid paths, context manager.
- ``EventBus``: publish/subscribe fan-out, persistence to store, stop sentinel.
- End-to-end: watcher → bus → subscriber queue using temporary directories.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from agent_lens.event_bus import EventBus
from agent_lens.models import AgentEvent, EventType
from agent_lens.watcher import AgentLensEventHandler, FileSystemWatcher


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _CollectingBus:
    """Minimal EventBus stand-in that records published events synchronously."""

    def __init__(self) -> None:
        self.events: List[AgentEvent] = []

    def publish(self, event: AgentEvent) -> None:  # noqa: D401
        """Record the event."""
        self.events.append(event)


@pytest.fixture()
def collecting_bus() -> _CollectingBus:
    """Return a synchronous collecting bus for handler unit-tests."""
    return _CollectingBus()


@pytest.fixture()
def handler(collecting_bus: _CollectingBus) -> AgentLensEventHandler:
    """Return an ``AgentLensEventHandler`` wired to the collecting bus."""
    # Cast to EventBus to satisfy the type checker — duck-typing is fine here.
    return AgentLensEventHandler(event_bus=collecting_bus)  # type: ignore[arg-type]


@pytest.fixture()
def tmp_watch_dir(tmp_path: Path) -> Path:
    """Return a temporary directory suitable for watcher tests."""
    d = tmp_path / "watch_root"
    d.mkdir()
    return d


@pytest.fixture()
async def running_bus() -> EventBus:
    """Return a started EventBus without a store."""
    bus = EventBus()
    await bus.start()
    yield bus
    await bus.stop()


# ---------------------------------------------------------------------------
# AgentLensEventHandler unit tests
# ---------------------------------------------------------------------------


class TestAgentLensEventHandler:
    """Unit tests for the watchdog event handler."""

    # ------------------------------------------------------------------
    # Ignore patterns
    # ------------------------------------------------------------------

    def test_ignores_pycache(self, handler: AgentLensEventHandler, collecting_bus: _CollectingBus) -> None:
        """Events inside __pycache__ directories should be silently dropped."""
        handler._handle("/project/__pycache__/foo.pyc", EventType.FILE_CREATE)
        assert len(collecting_bus.events) == 0

    def test_ignores_pyc_extension(self, handler: AgentLensEventHandler, collecting_bus: _CollectingBus) -> None:
        """Events for .pyc files should be ignored."""
        handler._handle("/project/main.pyc", EventType.FILE_MODIFY)
        assert len(collecting_bus.events) == 0

    def test_ignores_git_directory(self, handler: AgentLensEventHandler, collecting_bus: _CollectingBus) -> None:
        """Events inside .git should be ignored."""
        handler._handle("/project/.git/COMMIT_EDITMSG", EventType.FILE_MODIFY)
        assert len(collecting_bus.events) == 0

    def test_ignores_ds_store(self, handler: AgentLensEventHandler, collecting_bus: _CollectingBus) -> None:
        """macOS .DS_Store events should be ignored."""
        handler._handle("/project/.DS_Store", EventType.FILE_CREATE)
        assert len(collecting_bus.events) == 0

    def test_ignores_swap_files(self, handler: AgentLensEventHandler, collecting_bus: _CollectingBus) -> None:
        """Vim swap files (.swp) should be ignored."""
        handler._handle("/project/.main.py.swp", EventType.FILE_MODIFY)
        assert len(collecting_bus.events) == 0

    def test_does_not_ignore_regular_python_file(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """Regular .py files should NOT be ignored."""
        p = tmp_path / "main.py"
        p.touch()
        handler._handle(str(p), EventType.FILE_CREATE)
        assert len(collecting_bus.events) == 1

    def test_custom_ignored_patterns(self, collecting_bus: _CollectingBus) -> None:
        """Custom ignore patterns should suppress matching events."""
        h = AgentLensEventHandler(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            ignored_patterns=[".venv"],
        )
        h._handle("/project/.venv/lib/python3.11/site.py", EventType.FILE_MODIFY)
        assert len(collecting_bus.events) == 0

    # ------------------------------------------------------------------
    # Event type mapping
    # ------------------------------------------------------------------

    def test_on_created_maps_to_file_create(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """on_created() should emit a FILE_CREATE event."""
        p = tmp_path / "new.py"
        p.touch()
        from watchdog.events import FileCreatedEvent
        handler.on_created(FileCreatedEvent(str(p)))
        assert len(collecting_bus.events) == 1
        assert collecting_bus.events[0].event_type == EventType.FILE_CREATE.value

    def test_on_modified_maps_to_file_modify(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """on_modified() should emit a FILE_MODIFY event."""
        p = tmp_path / "existing.py"
        p.touch()
        from watchdog.events import FileModifiedEvent
        handler.on_modified(FileModifiedEvent(str(p)))
        assert len(collecting_bus.events) == 1
        assert collecting_bus.events[0].event_type == EventType.FILE_MODIFY.value

    def test_on_deleted_maps_to_file_delete(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """on_deleted() should emit a FILE_DELETE event."""
        from watchdog.events import FileDeletedEvent
        handler.on_deleted(FileDeletedEvent("/some/gone/file.py"))
        assert len(collecting_bus.events) == 1
        assert collecting_bus.events[0].event_type == EventType.FILE_DELETE.value

    def test_on_moved_emits_delete_and_create(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """on_moved() should emit FILE_DELETE for src and FILE_CREATE for dest."""
        src = tmp_path / "old.py"
        dst = tmp_path / "new.py"
        dst.touch()  # dest must exist for resolve
        from watchdog.events import FileMovedEvent
        handler.on_moved(FileMovedEvent(str(src), str(dst)))
        assert len(collecting_bus.events) == 2
        types = {e.event_type for e in collecting_bus.events}
        assert EventType.FILE_DELETE.value in types
        assert EventType.FILE_CREATE.value in types

    def test_on_created_dir_event_ignored(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """Directory create events should be silently ignored."""
        from watchdog.events import DirCreatedEvent
        handler.on_created(DirCreatedEvent(str(tmp_path)))
        assert len(collecting_bus.events) == 0

    def test_on_modified_dir_event_ignored(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """Directory modify events should be silently ignored."""
        from watchdog.events import DirModifiedEvent
        handler.on_modified(DirModifiedEvent(str(tmp_path)))
        assert len(collecting_bus.events) == 0

    def test_on_deleted_dir_event_ignored(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """Directory delete events should be silently ignored."""
        from watchdog.events import DirDeletedEvent
        handler.on_deleted(DirDeletedEvent(str(tmp_path)))
        assert len(collecting_bus.events) == 0

    # ------------------------------------------------------------------
    # Path normalisation
    # ------------------------------------------------------------------

    def test_path_is_normalised_to_absolute(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """The path in the published event should be an absolute, resolved path."""
        p = tmp_path / "a.py"
        p.touch()
        handler._handle(str(p), EventType.FILE_CREATE)
        assert len(collecting_bus.events) == 1
        assert os.path.isabs(collecting_bus.events[0].path)

    def test_metadata_contains_raw_path(
        self,
        handler: AgentLensEventHandler,
        collecting_bus: _CollectingBus,
        tmp_path: Path,
    ) -> None:
        """The event metadata should contain the original raw path."""
        p = tmp_path / "b.py"
        p.touch()
        handler._handle(str(p), EventType.FILE_MODIFY)
        assert "raw_path" in collecting_bus.events[0].metadata


# ---------------------------------------------------------------------------
# FileSystemWatcher lifecycle tests
# ---------------------------------------------------------------------------


class TestFileSystemWatcherLifecycle:
    """Tests for FileSystemWatcher start/stop behaviour."""

    def test_start_and_stop(self, tmp_watch_dir: Path, collecting_bus: _CollectingBus) -> None:
        """Starting and stopping the watcher should complete without errors."""
        watcher = FileSystemWatcher(
            paths=[tmp_watch_dir],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        watcher.start()
        assert watcher.is_running
        watcher.stop()
        assert not watcher.is_running

    def test_double_start_raises(self, tmp_watch_dir: Path, collecting_bus: _CollectingBus) -> None:
        """Calling start() twice should raise RuntimeError."""
        watcher = FileSystemWatcher(
            paths=[tmp_watch_dir],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        watcher.start()
        try:
            with pytest.raises(RuntimeError):
                watcher.start()
        finally:
            watcher.stop()

    def test_stop_without_start_is_safe(
        self, tmp_watch_dir: Path, collecting_bus: _CollectingBus
    ) -> None:
        """Calling stop() before start() should not raise."""
        watcher = FileSystemWatcher(
            paths=[tmp_watch_dir],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        watcher.stop()  # should be a no-op

    def test_nonexistent_path_is_skipped(
        self, tmp_path: Path, collecting_bus: _CollectingBus
    ) -> None:
        """A non-existent watch path should be skipped without raising."""
        watcher = FileSystemWatcher(
            paths=[tmp_path / "does_not_exist"],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        # Should not raise even though path doesn't exist
        watcher.start()
        # Since no valid paths, observer may not have started
        watcher.stop()

    def test_file_path_instead_of_dir_is_skipped(
        self, tmp_path: Path, collecting_bus: _CollectingBus
    ) -> None:
        """Providing a file path (not a directory) should be skipped gracefully."""
        f = tmp_path / "not_a_dir.txt"
        f.touch()
        watcher = FileSystemWatcher(
            paths=[f],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        watcher.start()
        watcher.stop()

    def test_context_manager_starts_and_stops(
        self, tmp_watch_dir: Path, collecting_bus: _CollectingBus
    ) -> None:
        """Using FileSystemWatcher as a context manager should auto start/stop."""
        watcher = FileSystemWatcher(
            paths=[tmp_watch_dir],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        with watcher:
            assert watcher.is_running
        assert not watcher.is_running

    def test_watched_paths_property(
        self, tmp_watch_dir: Path, collecting_bus: _CollectingBus
    ) -> None:
        """watched_paths should return the resolved list of paths."""
        watcher = FileSystemWatcher(
            paths=[tmp_watch_dir],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        assert tmp_watch_dir in watcher.watched_paths

    def test_multiple_paths(
        self, tmp_path: Path, collecting_bus: _CollectingBus
    ) -> None:
        """Watcher should accept and schedule multiple paths."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        watcher = FileSystemWatcher(
            paths=[dir_a, dir_b],
            event_bus=collecting_bus,  # type: ignore[arg-type]
        )
        with watcher:
            assert watcher.is_running


# ---------------------------------------------------------------------------
# EventBus unit tests
# ---------------------------------------------------------------------------


class TestEventBus:
    """Tests for the async EventBus in isolation."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        """EventBus should start and stop cleanly."""
        bus = EventBus()
        await bus.start()
        assert bus._running
        await bus.stop()
        assert not bus._running

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self) -> None:
        """Calling start() twice should not raise."""
        bus = EventBus()
        await bus.start()
        await bus.start()  # second call should be a no-op
        await bus.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_noop(self) -> None:
        """Calling stop() twice should not raise."""
        bus = EventBus()
        await bus.start()
        await bus.stop()
        await bus.stop()  # second call should be a no-op

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self) -> None:
        """An event published to the bus should appear in a subscriber's queue."""
        bus = EventBus()
        await bus.start()
        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/x.py")

        received: Optional[AgentEvent] = None
        async with bus.subscribe() as queue:
            await bus.publish_async(event)
            # Allow dispatcher to run
            await asyncio.sleep(0.05)
            received = queue.get_nowait()

        await bus.stop()
        assert received is not None
        assert received.id == event.id

    @pytest.mark.asyncio
    async def test_publish_async_delivers_to_subscriber(self) -> None:
        """publish_async() should fan-out the event like publish()."""
        bus = EventBus()
        await bus.start()
        event = AgentEvent(event_type=EventType.FILE_MODIFY, path="/tmp/y.py")

        async with bus.subscribe() as queue:
            await bus.publish_async(event)
            await asyncio.sleep(0.05)
            received = queue.get_nowait()

        await bus.stop()
        assert received.id == event.id

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive_event(self) -> None:
        """Every active subscriber should receive each published event."""
        bus = EventBus()
        await bus.start()
        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/z.py")

        async with bus.subscribe() as q1, bus.subscribe() as q2:
            await bus.publish_async(event)
            await asyncio.sleep(0.05)
            e1 = q1.get_nowait()
            e2 = q2.get_nowait()

        await bus.stop()
        assert e1.id == event.id
        assert e2.id == event.id

    @pytest.mark.asyncio
    async def test_subscriber_count(self) -> None:
        """subscriber_count should reflect the number of active subscribers."""
        bus = EventBus()
        await bus.start()
        assert bus.subscriber_count == 0

        async with bus.subscribe():
            assert bus.subscriber_count == 1
            async with bus.subscribe():
                assert bus.subscriber_count == 2
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0

        await bus.stop()

    @pytest.mark.asyncio
    async def test_publish_before_start_drops_event(self) -> None:
        """Publishing before start() should not raise (event is dropped)."""
        bus = EventBus()
        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/a.py")
        # Should silently drop without error
        bus.publish(event)

    @pytest.mark.asyncio
    async def test_stop_sends_sentinel_to_subscribers(self) -> None:
        """After bus.stop(), active subscribers should receive a None sentinel."""
        bus = EventBus()
        await bus.start()

        received_sentinel = False
        async with bus.subscribe() as queue:
            await bus.stop()
            # Drain the queue looking for the sentinel
            for _ in range(20):
                try:
                    item = queue.get_nowait()
                    if item is None:
                        received_sentinel = True
                        break
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)

        assert received_sentinel

    @pytest.mark.asyncio
    async def test_bus_persists_to_store(self) -> None:
        """Events published to a bus with a store should be persisted."""
        from agent_lens.store import EventStore

        store = EventStore(db_path=":memory:")
        store.initialize()

        bus = EventBus(store=store)
        await bus.start()

        event = AgentEvent(event_type=EventType.FILE_CREATE, path="/tmp/store_test.py")
        await bus.publish_async(event)
        await asyncio.sleep(0.1)

        await bus.stop()

        stored = store.query()
        assert len(stored) == 1
        assert stored[0].id == event.id
        store.close()

    @pytest.mark.asyncio
    async def test_bus_multiple_events_in_order(self) -> None:
        """Multiple events should be delivered to subscribers in publish order."""
        bus = EventBus()
        await bus.start()

        events = [
            AgentEvent(event_type=EventType.FILE_CREATE, path=f"/tmp/f{i}.py")
            for i in range(5)
        ]

        async with bus.subscribe() as queue:
            for e in events:
                await bus.publish_async(e)
            await asyncio.sleep(0.1)

            received = []
            for _ in range(5):
                try:
                    received.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

        await bus.stop()
        assert len(received) == 5
        assert [e.id for e in received] == [e.id for e in events]


# ---------------------------------------------------------------------------
# End-to-end integration tests: watcher → bus → subscriber
# ---------------------------------------------------------------------------


class TestWatcherToBusIntegration:
    """End-to-end tests that drive the real watchdog observer."""

    @pytest.mark.asyncio
    async def test_file_create_reaches_subscriber(self, tmp_watch_dir: Path) -> None:
        """Creating a file in the watched directory should deliver a FILE_CREATE event."""
        bus = EventBus()
        await bus.start()

        watcher = FileSystemWatcher(paths=[tmp_watch_dir], event_bus=bus)
        watcher.start()

        received_event: Optional[AgentEvent] = None

        async with bus.subscribe() as queue:
            # Give the observer a moment to set up inotify/kqueue watches
            await asyncio.sleep(0.2)

            # Trigger a file creation
            new_file = tmp_watch_dir / "hello.py"
            new_file.write_text("print('hello')")

            # Wait for the event to propagate
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    candidate = queue.get_nowait()
                    if candidate is not None and candidate.event_type == EventType.FILE_CREATE.value:
                        received_event = candidate
                        break
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)

        watcher.stop()
        await bus.stop()

        assert received_event is not None, "No FILE_CREATE event received within timeout"
        assert "hello.py" in (received_event.path or "")

    @pytest.mark.asyncio
    async def test_file_modify_reaches_subscriber(self, tmp_watch_dir: Path) -> None:
        """Modifying a watched file should deliver a FILE_MODIFY event."""
        bus = EventBus()
        await bus.start()

        # Pre-create the file so we only watch for modification
        existing = tmp_watch_dir / "existing.txt"
        existing.write_text("original content")

        watcher = FileSystemWatcher(paths=[tmp_watch_dir], event_bus=bus)
        watcher.start()

        received_modify: Optional[AgentEvent] = None

        async with bus.subscribe() as queue:
            await asyncio.sleep(0.2)

            # Modify the file
            existing.write_text("updated content")

            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    candidate = queue.get_nowait()
                    if candidate is not None and candidate.event_type == EventType.FILE_MODIFY.value:
                        received_modify = candidate
                        break
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)

        watcher.stop()
        await bus.stop()

        assert received_modify is not None, "No FILE_MODIFY event received within timeout"

    @pytest.mark.asyncio
    async def test_file_delete_reaches_subscriber(self, tmp_watch_dir: Path) -> None:
        """Deleting a watched file should deliver a FILE_DELETE event."""
        bus = EventBus()
        await bus.start()

        to_delete = tmp_watch_dir / "delete_me.txt"
        to_delete.write_text("bye")

        watcher = FileSystemWatcher(paths=[tmp_watch_dir], event_bus=bus)
        watcher.start()

        received_delete: Optional[AgentEvent] = None

        async with bus.subscribe() as queue:
            await asyncio.sleep(0.2)
            to_delete.unlink()

            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    candidate = queue.get_nowait()
                    if candidate is not None and candidate.event_type == EventType.FILE_DELETE.value:
                        received_delete = candidate
                        break
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)

        watcher.stop()
        await bus.stop()

        assert received_delete is not None, "No FILE_DELETE event received within timeout"

    @pytest.mark.asyncio
    async def test_pycache_events_not_forwarded(self, tmp_watch_dir: Path) -> None:
        """Events inside __pycache__ should not appear in the subscriber queue."""
        bus = EventBus()
        await bus.start()

        pycache = tmp_watch_dir / "__pycache__"
        pycache.mkdir()

        watcher = FileSystemWatcher(paths=[tmp_watch_dir], event_bus=bus)
        watcher.start()

        collected: List[AgentEvent] = []

        async with bus.subscribe() as queue:
            await asyncio.sleep(0.2)
            # Write a .pyc file — should be ignored
            (pycache / "module.cpython-311.pyc").write_bytes(b"\x00" * 16)
            await asyncio.sleep(0.3)

            # Drain queue
            while True:
                try:
                    item = queue.get_nowait()
                    if item is not None:
                        collected.append(item)
                except asyncio.QueueEmpty:
                    break

        watcher.stop()
        await bus.stop()

        # None of the collected events should be from __pycache__
        for e in collected:
            assert "__pycache__" not in (e.path or ""), (
                f"Unexpected __pycache__ event: {e.path}"
            )
