"""Psutil-based process and network monitor for AgentLens.

This module provides ``ProcessMonitor``, a background thread that periodically
polls running processes using ``psutil`` to:

- Detect new process invocations (shell commands spawned by AI agents).
- Capture outbound network connections established by monitored processes.
- Attribute events to specific process names when a filter list is provided.

All captured events are published to the ``EventBus`` via the thread-safe
:meth:`EventBus.publish` method.

The monitor maintains an internal snapshot of known process PIDs and
connection tuples so it only emits *new* events on each poll cycle,
avoiding duplicate noise.

Example usage::

    bus = EventBus(store=store)
    await bus.start()

    monitor = ProcessMonitor(
        event_bus=bus,
        process_filter=["aider", "python"],
        poll_interval=1.0,
    )
    monitor.start()
    # ... later ...
    monitor.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

import psutil

from agent_lens.event_bus import EventBus
from agent_lens.models import AgentEvent, EventType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


class _ProcessSnapshot(NamedTuple):
    """Lightweight immutable snapshot of a single running process."""

    pid: int
    name: str
    cmdline: str  # joined command-line string
    create_time: float  # epoch seconds — uniquely identifies a PID lifetime


class _ConnectionKey(NamedTuple):
    """Uniquely identifies a single network connection."""

    pid: int
    local_addr: str
    local_port: int
    remote_addr: str
    remote_port: int
    status: str


# ---------------------------------------------------------------------------
# ProcessMonitor
# ---------------------------------------------------------------------------


class ProcessMonitor:
    """Background thread that polls psutil for new processes and connections.

    The monitor wakes up every ``poll_interval`` seconds, scans all running
    processes (or only those matching ``process_filter``), compares against
    its last snapshot, and publishes ``AgentEvent`` instances to the bus for
    each new discovery.

    Args:
        event_bus: The ``EventBus`` to publish captured events to.
        process_filter: Optional list of process *names* (case-insensitive
            substring match) to restrict monitoring to.  When the list is
            empty or ``None`` every process is considered.
        poll_interval: Seconds between each poll cycle.  Defaults to ``1.0``.
        track_network: Whether to monitor outbound network connections.
            Defaults to ``True``.
        track_processes: Whether to monitor new process invocations.
            Defaults to ``True``.
    """

    def __init__(
        self,
        event_bus: EventBus,
        process_filter: Optional[List[str]] = None,
        poll_interval: float = 1.0,
        track_network: bool = True,
        track_processes: bool = True,
    ) -> None:
        self._event_bus = event_bus
        self._process_filter: List[str] = [
            name.lower() for name in (process_filter or [])
        ]
        self._poll_interval = max(0.1, poll_interval)  # enforce minimum
        self._track_network = track_network
        self._track_processes = track_processes

        # State tracking
        self._known_process_keys: Set[Tuple[int, float]] = set()  # (pid, create_time)
        self._known_connection_keys: Set[_ConnectionKey] = set()

        # Thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread.

        Performs an initial snapshot so that processes already running at
        startup are not reported as new events.

        Raises:
            RuntimeError: If the monitor has already been started.
        """
        with self._lock:
            if self._started:
                raise RuntimeError("ProcessMonitor is already running")

            # Seed the known-process and known-connection sets from the
            # current system state so we don't flood events on startup.
            self._seed_initial_state()

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="agent_lens_process_monitor",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            logger.info(
                "ProcessMonitor started (interval=%.1fs, filter=%s)",
                self._poll_interval,
                self._process_filter or "<all>",
            )

    def stop(self) -> None:
        """Stop the background polling thread.

        Blocks until the thread has exited (up to ``poll_interval + 5``
        seconds).  Idempotent — safe to call on an already-stopped monitor.
        """
        with self._lock:
            if not self._started:
                return
            logger.info("Stopping ProcessMonitor")
            self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 5.0)
            if self._thread.is_alive():
                logger.warning("ProcessMonitor thread did not exit within timeout")

        with self._lock:
            self._started = False
            self._thread = None
        logger.info("ProcessMonitor stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the polling thread is active."""
        with self._lock:
            return self._started and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProcessMonitor":
        """Start the monitor when used as a context manager."""
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """Stop the monitor when leaving the context manager block."""
        self.stop()

    # ------------------------------------------------------------------
    # Internal: initialisation
    # ------------------------------------------------------------------

    def _seed_initial_state(self) -> None:
        """Capture the current system state to avoid startup noise.

        All processes and connections visible right now are recorded as
        "already known" so that only *changes* after startup generate events.
        """
        try:
            for proc in psutil.process_iter(["pid", "name", "create_time"]):
                try:
                    info = proc.info
                    pid = info.get("pid")
                    create_time = info.get("create_time", 0.0)
                    if pid is not None:
                        self._known_process_keys.add((pid, create_time))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to seed process state: %s", exc)

        if self._track_network:
            try:
                conns = psutil.net_connections(kind="inet")
                for conn in conns:
                    key = _connection_to_key(conn)
                    if key is not None:
                        self._known_connection_keys.add(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to seed network connection state: %s", exc)

    # ------------------------------------------------------------------
    # Internal: poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Main loop executed in the background thread.

        Runs until ``_stop_event`` is set, sleeping ``_poll_interval``
        between iterations.
        """
        logger.debug("ProcessMonitor poll loop started")
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected error in ProcessMonitor poll cycle: %s", exc)

            # Use wait() so that stop() can interrupt the sleep immediately
            self._stop_event.wait(timeout=self._poll_interval)

        logger.debug("ProcessMonitor poll loop exited")

    def _poll_once(self) -> None:
        """Execute a single poll cycle: check processes and connections."""
        if self._track_processes:
            self._check_processes()
        if self._track_network:
            self._check_network_connections()

    # ------------------------------------------------------------------
    # Internal: process tracking
    # ------------------------------------------------------------------

    def _check_processes(self) -> None:
        """Compare running processes against the last snapshot.

        Any process not previously seen is published as a
        ``SHELL_COMMAND`` event.
        """
        try:
            current_procs: Dict[Tuple[int, float], _ProcessSnapshot] = {}

            for proc in psutil.process_iter(
                ["pid", "name", "cmdline", "create_time"]
            ):
                snapshot = _safe_process_snapshot(proc)
                if snapshot is None:
                    continue

                if not self._matches_filter(snapshot.name):
                    continue

                key = (snapshot.pid, snapshot.create_time)
                current_procs[key] = snapshot

            # Determine newly appeared processes
            new_keys = set(current_procs.keys()) - self._known_process_keys

            for key in new_keys:
                snapshot = current_procs[key]
                self._emit_process_event(snapshot)

            # Update the known set to the full current set (filtered procs)
            # We merge rather than replace so non-filtered procs aren't
            # treated as new next cycle when filter is in use.
            self._known_process_keys.update(current_procs.keys())

            # Prune PIDs that no longer exist to keep memory bounded
            self._known_process_keys = {
                k for k in self._known_process_keys
                if psutil.pid_exists(k[0])
            }

        except Exception as exc:  # noqa: BLE001
            logger.debug("Error during process check: %s", exc)

    def _matches_filter(self, process_name: str) -> bool:
        """Return ``True`` if the process name passes the filter.

        When no filter is configured every name matches.

        Args:
            process_name: The process name to test.

        Returns:
            ``True`` if the process should be monitored.
        """
        if not self._process_filter:
            return True
        name_lower = process_name.lower()
        return any(f in name_lower for f in self._process_filter)

    def _emit_process_event(self, snapshot: _ProcessSnapshot) -> None:
        """Publish a ``SHELL_COMMAND`` event for a newly seen process.

        Args:
            snapshot: The process snapshot to emit as an event.
        """
        event = AgentEvent(
            event_type=EventType.SHELL_COMMAND,
            process_name=snapshot.name,
            process_pid=snapshot.pid,
            command_line=snapshot.cmdline or snapshot.name,
            metadata={
                "create_time": snapshot.create_time,
            },
        )
        logger.debug(
            "New process detected: pid=%d name=%s cmd=%s",
            snapshot.pid,
            snapshot.name,
            snapshot.cmdline[:80] if snapshot.cmdline else "",
        )
        self._event_bus.publish(event)

    # ------------------------------------------------------------------
    # Internal: network connection tracking
    # ------------------------------------------------------------------

    def _check_network_connections(self) -> None:
        """Compare active network connections against the last snapshot.

        Each newly seen *outbound* connection is published as a
        ``NETWORK_CALL`` event.  Connections without a valid remote address
        (e.g. listening sockets) are silently ignored.
        """
        try:
            current_keys: Set[_ConnectionKey] = set()

            # Build a PID → process info map for attribution
            pid_info: Dict[int, _ProcessSnapshot] = {}
            if self._process_filter:
                for proc in psutil.process_iter(
                    ["pid", "name", "cmdline", "create_time"]
                ):
                    snap = _safe_process_snapshot(proc)
                    if snap and self._matches_filter(snap.name):
                        pid_info[snap.pid] = snap

            conns = psutil.net_connections(kind="inet")
            for conn in conns:
                # Only care about ESTABLISHED outbound-style connections
                if not _is_outbound_connection(conn):
                    continue

                # If a process filter is active, restrict to matching PIDs
                if self._process_filter:
                    pid = getattr(conn, "pid", None)
                    if pid not in pid_info:
                        continue

                key = _connection_to_key(conn)
                if key is None:
                    continue

                current_keys.add(key)

            new_keys = current_keys - self._known_connection_keys

            for key in new_keys:
                # Try to attribute to a process
                proc_snap: Optional[_ProcessSnapshot] = None
                if key.pid and key.pid in pid_info:
                    proc_snap = pid_info[key.pid]
                elif key.pid:
                    proc_snap = _safe_process_snapshot_by_pid(key.pid)

                self._emit_network_event(key, proc_snap)

            # Update known connections; connections that are no longer
            # ESTABLISHED will naturally fall off on the next cycle.
            self._known_connection_keys = current_keys

        except Exception as exc:  # noqa: BLE001
            logger.debug("Error during network check: %s", exc)

    def _emit_network_event(
        self,
        key: _ConnectionKey,
        proc: Optional[_ProcessSnapshot],
    ) -> None:
        """Publish a ``NETWORK_CALL`` event for a newly seen connection.

        Args:
            key: The connection key describing the network tuple.
            proc: Optional process snapshot for attribution.
        """
        event = AgentEvent(
            event_type=EventType.NETWORK_CALL,
            remote_host=key.remote_addr,
            remote_port=key.remote_port,
            process_name=proc.name if proc else None,
            process_pid=proc.pid if proc else (key.pid if key.pid else None),
            command_line=proc.cmdline if proc else None,
            metadata={
                "local_addr": key.local_addr,
                "local_port": key.local_port,
                "status": key.status,
            },
        )
        logger.debug(
            "New network connection: %s:%d → %s:%d (pid=%s)",
            key.local_addr,
            key.local_port,
            key.remote_addr,
            key.remote_port,
            key.pid,
        )
        self._event_bus.publish(event)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _safe_process_snapshot(proc: psutil.Process) -> Optional[_ProcessSnapshot]:
    """Attempt to build a ``_ProcessSnapshot`` from a psutil process.

    Returns ``None`` if the process has disappeared or access is denied.

    Args:
        proc: A ``psutil.Process`` instance.

    Returns:
        A ``_ProcessSnapshot`` or ``None`` on failure.
    """
    try:
        info = proc.info  # type: ignore[attr-defined]
        pid: int = info.get("pid") or proc.pid
        name: str = info.get("name") or ""
        create_time: float = info.get("create_time") or 0.0
        raw_cmdline = info.get("cmdline") or []
        cmdline = " ".join(raw_cmdline) if raw_cmdline else name
        return _ProcessSnapshot(
            pid=pid,
            name=name,
            cmdline=cmdline,
            create_time=create_time,
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unexpected error reading process info: %s", exc)
        return None


def _safe_process_snapshot_by_pid(pid: int) -> Optional[_ProcessSnapshot]:
    """Attempt to build a ``_ProcessSnapshot`` for a specific PID.

    Args:
        pid: The process ID to look up.

    Returns:
        A ``_ProcessSnapshot`` or ``None`` if the process cannot be accessed.
    """
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        create_time = proc.create_time()
        try:
            cmdline_parts = proc.cmdline()
            cmdline = " ".join(cmdline_parts) if cmdline_parts else name
        except (psutil.AccessDenied, psutil.ZombieProcess):
            cmdline = name
        return _ProcessSnapshot(
            pid=pid,
            name=name,
            cmdline=cmdline,
            create_time=create_time,
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unexpected error reading process %d info: %s", pid, exc)
        return None


def _connection_to_key(conn: psutil.pconn) -> Optional[_ConnectionKey]:  # type: ignore[name-defined]
    """Convert a ``psutil`` connection object to a ``_ConnectionKey``.

    Returns ``None`` if the connection lacks a valid remote address (e.g. a
    listening socket with no established peer).

    Args:
        conn: A connection object returned by ``psutil.net_connections()``.

    Returns:
        A ``_ConnectionKey`` or ``None``.
    """
    raddr = getattr(conn, "raddr", None)
    laddr = getattr(conn, "laddr", None)

    # Remote address must be present and non-empty
    if not raddr or not getattr(raddr, "ip", None):
        return None
    if not laddr:
        return None

    remote_ip: str = raddr.ip
    remote_port: int = raddr.port
    local_ip: str = getattr(laddr, "ip", "")
    local_port: int = getattr(laddr, "port", 0)
    status: str = getattr(conn, "status", "") or ""
    pid: int = getattr(conn, "pid", 0) or 0

    # Sanity-check ports
    if remote_port <= 0 or remote_port > 65535:
        return None

    return _ConnectionKey(
        pid=pid,
        local_addr=local_ip,
        local_port=local_port,
        remote_addr=remote_ip,
        remote_port=remote_port,
        status=status,
    )


def _is_outbound_connection(conn: psutil.pconn) -> bool:  # type: ignore[name-defined]
    """Return ``True`` if the connection looks like an outbound connection.

    We consider a connection "outbound" when:
    - It has a valid remote address.
    - Its status is ``ESTABLISHED`` or empty (UDP connections have no status).
    - The remote port is non-zero.

    Listening sockets (``LISTEN`` status, no remote address) are excluded.

    Args:
        conn: A connection object returned by ``psutil.net_connections()``.

    Returns:
        ``True`` if the connection qualifies as outbound.
    """
    raddr = getattr(conn, "raddr", None)
    if not raddr or not getattr(raddr, "ip", None):
        return False

    status: str = getattr(conn, "status", "") or ""
    # ESTABLISHED covers TCP; empty string covers UDP and some platforms
    if status and status not in ("ESTABLISHED", "SYN_SENT", "TIME_WAIT", ""):
        return False

    remote_port: int = getattr(raddr, "port", 0)
    return remote_port > 0
