"""Watchdog-based filesystem monitor for AgentLens.

This module provides ``FileSystemWatcher``, a wrapper around the ``watchdog``
library that monitors one or more directory trees for filesystem events
(create, modify, delete) and enqueues them as ``AgentEvent`` instances into
the ``EventBus``.

The watcher runs in a background thread (managed by watchdog's
``Observer``) and is intentionally decoupled from asyncio — it calls
the thread-safe ``EventBus.publish`` method to hand events over to the
async world.

Example usage::

    bus = EventBus(store=store)
    await bus.start()

    watcher = FileSystemWatcher(
        paths=["/home/user/my-project"],
        event_bus=bus,
        process_filter=["aider", "python"],
    )
    watcher.start()
    # ... later ...
    watcher.stop()
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    DirMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from agent_lens.event_bus import EventBus
from agent_lens.models import AgentEvent, EventType

logger = logging.getLogger(__name__)


class AgentLensEventHandler(FileSystemEventHandler):
    """Watchdog event handler that converts filesystem events to ``AgentEvent``.

    Each observed filesystem change is wrapped in an ``AgentEvent`` and
    published to the provided ``EventBus``.

    Args:
        event_bus: The bus to publish events to.
        process_filter: Optional list of process names.  When provided, only
            events that can be attributed to one of these processes are
            forwarded.  Currently this is a best-effort heuristic — watchdog
            does not expose process attribution, so filtering is applied at
            the path level only (reserved for future enhancement).
        ignored_patterns: Optional list of glob-like path substrings to ignore.
            Events whose path contains any of these strings are silently
            dropped.  Common IDE temp files and ``__pycache__`` are always
            ignored.
    """

    # Substrings of paths that are always silently ignored.
    _DEFAULT_IGNORED: tuple[str, ...] = (
        "__pycache__",
        ".pyc",
        ".pyo",
        ".git",
        ".DS_Store",
        "~",
        ".swp",
        ".swo",
        ".tmp",
        ".lock",
    )

    def __init__(
        self,
        event_bus: EventBus,
        process_filter: Optional[List[str]] = None,
        ignored_patterns: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self._event_bus = event_bus
        self._process_filter = process_filter or []
        extra_ignored = ignored_patterns or []
        self._ignored_patterns: tuple[str, ...] = (
            self._DEFAULT_IGNORED + tuple(extra_ignored)
        )

    # ------------------------------------------------------------------
    # Watchdog callbacks
    # ------------------------------------------------------------------

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """Handle file/directory creation events."""
        if isinstance(event, (DirCreatedEvent,)):
            return  # We only care about files for now
        self._handle(event.src_path, EventType.FILE_CREATE)

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """Handle file/directory modification events."""
        if isinstance(event, (DirModifiedEvent,)):
            return
        self._handle(event.src_path, EventType.FILE_MODIFY)

    def on_deleted(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """Handle file/directory deletion events."""
        if isinstance(event, (DirDeletedEvent,)):
            return
        self._handle(event.src_path, EventType.FILE_DELETE)

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """Handle file/directory move events.

        A move is translated into a DELETE for the source path and a CREATE
        for the destination path.
        """
        if isinstance(event, DirMovedEvent):
            return
        if hasattr(event, "src_path"):
            self._handle(event.src_path, EventType.FILE_DELETE)
        if hasattr(event, "dest_path"):
            self._handle(event.dest_path, EventType.FILE_CREATE)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_ignore(self, path: str) -> bool:
        """Return ``True`` if the path matches any ignore pattern.

        Args:
            path: The absolute or relative path string to check.

        Returns:
            ``True`` if the path should be silently dropped.
        """
        for pattern in self._ignored_patterns:
            if pattern in path:
                return True
        return False

    def _handle(self, path: str, event_type: EventType) -> None:
        """Translate a raw watchdog path+type into an ``AgentEvent`` and publish it.

        Args:
            path: The filesystem path that changed.
            event_type: The type of change.
        """
        if self._should_ignore(path):
            logger.debug("Ignoring path %s", path)
            return

        # Normalise to absolute path
        abs_path = str(Path(path).resolve())

        agent_event = AgentEvent(
            event_type=event_type,
            path=abs_path,
            metadata={"raw_path": path},
        )
        logger.debug("Publishing %s for %s", event_type.value, abs_path)
        self._event_bus.publish(agent_event)


class FileSystemWatcher:
    """Watches one or more directory trees and publishes events to the bus.

    This class manages a watchdog ``Observer`` and one
    ``AgentLensEventHandler`` per watched path.  It can be started and
    stopped independently of the asyncio event loop.

    Args:
        paths: List of directory paths to watch recursively.  Non-existent
            paths are skipped with a warning.
        event_bus: The ``EventBus`` to publish captured events to.
        process_filter: Optional list of process names to restrict monitoring
            to (forwarded to ``AgentLensEventHandler``).
        ignored_patterns: Optional path substrings to ignore.
        recursive: Whether to watch directories recursively.  Defaults to
            ``True``.
    """

    def __init__(
        self,
        paths: List[str | Path],
        event_bus: EventBus,
        process_filter: Optional[List[str]] = None,
        ignored_patterns: Optional[List[str]] = None,
        recursive: bool = True,
    ) -> None:
        self._paths = [Path(p).resolve() for p in paths]
        self._event_bus = event_bus
        self._process_filter = process_filter
        self._ignored_patterns = ignored_patterns
        self._recursive = recursive
        self._observer: Optional[Observer] = None
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the filesystem observer in a background thread.

        Paths that do not exist on the filesystem are skipped with a warning
        rather than raising an exception, so the watcher degrades gracefully
        when directories are created after startup.

        Raises:
            RuntimeError: If the watcher has already been started.
        """
        with self._lock:
            if self._started:
                raise RuntimeError("FileSystemWatcher is already running")

            self._observer = Observer()
            handler = AgentLensEventHandler(
                event_bus=self._event_bus,
                process_filter=self._process_filter,
                ignored_patterns=self._ignored_patterns,
            )

            scheduled_any = False
            for path in self._paths:
                if not path.exists():
                    logger.warning(
                        "Watch path does not exist, skipping: %s", path
                    )
                    continue
                if not path.is_dir():
                    logger.warning(
                        "Watch path is not a directory, skipping: %s", path
                    )
                    continue
                self._observer.schedule(
                    handler, str(path), recursive=self._recursive
                )
                logger.info(
                    "Watching path (recursive=%s): %s", self._recursive, path
                )
                scheduled_any = True

            if not scheduled_any:
                logger.warning("No valid watch paths found; observer not started")
                self._observer = None
                return

            self._observer.start()
            self._started = True
            logger.info("FileSystemWatcher started")

    def stop(self) -> None:
        """Stop the filesystem observer and wait for its thread to exit.

        This method is idempotent; calling it on an already-stopped watcher
        is safe.
        """
        with self._lock:
            if not self._started or self._observer is None:
                return
            logger.info("Stopping FileSystemWatcher")
            self._observer.stop()
            self._observer.join(timeout=10)
            self._observer = None
            self._started = False
            logger.info("FileSystemWatcher stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the observer is currently active."""
        with self._lock:
            return self._started and self._observer is not None

    @property
    def watched_paths(self) -> List[Path]:
        """Return the list of resolved paths this watcher is monitoring."""
        return list(self._paths)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "FileSystemWatcher":
        """Start the watcher when used as a context manager."""
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """Stop the watcher when leaving the context manager block."""
        self.stop()
