"""Tests for the psutil-based ProcessMonitor.

Covers:
- Lifecycle: start, stop, double-start, double-stop, context manager.
- Filter matching: process name substring matching.
- Process detection: new process events are published; pre-existing processes
  are not emitted on startup.
- Network detection: new outbound connections are published as NETWORK_CALL events.
- Internal helpers: ``_connection_to_key``, ``_is_outbound_connection``,
  ``_safe_process_snapshot``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import psutil
import pytest

from agent_lens.event_bus import EventBus
from agent_lens.models import AgentEvent, EventType
from agent_lens.process_monitor import (
    ProcessMonitor,
    _ConnectionKey,
    _ProcessSnapshot,
    _connection_to_key,
    _is_outbound_connection,
    _safe_process_snapshot,
    _safe_process_snapshot_by_pid,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _CollectingBus:
    """Minimal EventBus stand-in that records published events synchronously."""

    def __init__(self) -> None:
        self.events: List[AgentEvent] = []
        self._lock = threading.Lock()

    def publish(self, event: AgentEvent) -> None:  # noqa: D401
        with self._lock:
            self.events.append(event)

    def events_of_type(self, et: EventType) -> List[AgentEvent]:
        with self._lock:
            return [e for e in self.events if e.event_type == et.value]


@pytest.fixture()
def collecting_bus() -> _CollectingBus:
    """Return a collecting event bus for synchronous tests."""
    return _CollectingBus()


@pytest.fixture()
def monitor(collecting_bus: _CollectingBus) -> ProcessMonitor:
    """Return an un-started ProcessMonitor."""
    return ProcessMonitor(
        event_bus=collecting_bus,  # type: ignore[arg-type]
        poll_interval=0.1,
    )


def _make_mock_conn(
    remote_ip: str = "1.2.3.4",
    remote_port: int = 443,
    local_ip: str = "192.168.1.10",
    local_port: int = 54321,
    status: str = "ESTABLISHED",
    pid: int = 1234,
) -> MagicMock:
    """Build a mock psutil connection object."""
    conn = MagicMock()
    conn.raddr = MagicMock()
    conn.raddr.ip = remote_ip
    conn.raddr.port = remote_port
    conn.laddr = MagicMock()
    conn.laddr.ip = local_ip
    conn.laddr.port = local_port
    conn.status = status
    conn.pid = pid
    return conn


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestProcessMonitorLifecycle:
    """Tests for ProcessMonitor start/stop lifecycle."""

    def test_starts_and_stops(self, monitor: ProcessMonitor) -> None:
        """Monitor should start and stop without errors."""
        monitor.start()
        assert monitor.is_running
        monitor.stop()
        assert not monitor.is_running

    def test_double_start_raises(self, monitor: ProcessMonitor) -> None:
        """Starting an already-running monitor should raise RuntimeError."""
        monitor.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                monitor.start()
        finally:
            monitor.stop()

    def test_double_stop_is_safe(self, monitor: ProcessMonitor) -> None:
        """Calling stop() twice should not raise."""
        monitor.start()
        monitor.stop()
        monitor.stop()  # second call — no-op

    def test_stop_before_start_is_safe(self, monitor: ProcessMonitor) -> None:
        """Calling stop() before start() should not raise."""
        monitor.stop()

    def test_context_manager(self, collecting_bus: _CollectingBus) -> None:
        """ProcessMonitor used as a context manager should start and stop."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            poll_interval=0.05,
        )
        with mon:
            assert mon.is_running
        assert not mon.is_running

    def test_is_running_false_before_start(self, monitor: ProcessMonitor) -> None:
        """is_running should be False before the monitor is started."""
        assert not monitor.is_running

    def test_is_running_false_after_stop(self, monitor: ProcessMonitor) -> None:
        """is_running should be False after the monitor is stopped."""
        monitor.start()
        monitor.stop()
        assert not monitor.is_running


# ---------------------------------------------------------------------------
# Filter matching tests
# ---------------------------------------------------------------------------


class TestFilterMatching:
    """Tests for process name filter matching."""

    def test_no_filter_matches_everything(self, monitor: ProcessMonitor) -> None:
        """With no filter, every process name should match."""
        assert monitor._matches_filter("python") is True
        assert monitor._matches_filter("aider") is True
        assert monitor._matches_filter("bash") is True
        assert monitor._matches_filter("") is True

    def test_exact_name_matches(self, collecting_bus: _CollectingBus) -> None:
        """A process name equal to the filter should match."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["aider"],
        )
        assert mon._matches_filter("aider") is True

    def test_substring_name_matches(self, collecting_bus: _CollectingBus) -> None:
        """Filter matching should be case-insensitive substring."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["python"],
        )
        assert mon._matches_filter("python3") is True
        assert mon._matches_filter("Python") is True

    def test_non_matching_name_excluded(self, collecting_bus: _CollectingBus) -> None:
        """A process name not in the filter should not match."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["aider"],
        )
        assert mon._matches_filter("vim") is False
        assert mon._matches_filter("bash") is False

    def test_multiple_filter_entries(self, collecting_bus: _CollectingBus) -> None:
        """Any filter entry matching the name should return True."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["aider", "python"],
        )
        assert mon._matches_filter("aider") is True
        assert mon._matches_filter("python3") is True
        assert mon._matches_filter("node") is False

    def test_filter_is_case_insensitive(self, collecting_bus: _CollectingBus) -> None:
        """Filter entries should match regardless of case."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["AIDER"],
        )
        assert mon._matches_filter("aider") is True
        assert mon._matches_filter("Aider") is True


# ---------------------------------------------------------------------------
# Process event emission tests
# ---------------------------------------------------------------------------


class TestProcessEventEmission:
    """Tests for _emit_process_event and new-process detection logic."""

    def test_emit_process_event_publishes_shell_command(self, monitor: ProcessMonitor, collecting_bus: _CollectingBus) -> None:
        """_emit_process_event() should publish a SHELL_COMMAND event."""
        snapshot = _ProcessSnapshot(
            pid=9999,
            name="test_proc",
            cmdline="test_proc --flag",
            create_time=1234567890.0,
        )
        monitor._emit_process_event(snapshot)

        events = collecting_bus.events_of_type(EventType.SHELL_COMMAND)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == EventType.SHELL_COMMAND.value
        assert event.process_name == "test_proc"
        assert event.process_pid == 9999
        assert event.command_line == "test_proc --flag"
        assert "create_time" in event.metadata

    def test_emit_process_event_uses_name_as_fallback_cmdline(self, monitor: ProcessMonitor, collecting_bus: _CollectingBus) -> None:
        """When cmdline is empty, process name should be used as command_line."""
        snapshot = _ProcessSnapshot(
            pid=1111,
            name="myprog",
            cmdline="",
            create_time=0.0,
        )
        monitor._emit_process_event(snapshot)
        events = collecting_bus.events_of_type(EventType.SHELL_COMMAND)
        assert len(events) == 1
        assert events[0].command_line == "myprog"

    def test_new_process_not_in_known_set_triggers_event(
        self, monitor: ProcessMonitor, collecting_bus: _CollectingBus
    ) -> None:
        """A process absent from _known_process_keys should trigger an event."""
        # Manually add a fake known process to the seen set
        monitor._known_process_keys = {(99999, 0.0)}

        # Create a snapshot for a "new" process
        new_snapshot = _ProcessSnapshot(
            pid=88888,
            name="new_proc",
            cmdline="new_proc arg1",
            create_time=9999999.0,
        )

        # Simulate what _check_processes does when it finds a new process
        key = (new_snapshot.pid, new_snapshot.create_time)
        if key not in monitor._known_process_keys:
            monitor._emit_process_event(new_snapshot)

        events = collecting_bus.events_of_type(EventType.SHELL_COMMAND)
        assert len(events) == 1

    def test_existing_process_not_re_emitted(
        self, monitor: ProcessMonitor, collecting_bus: _CollectingBus
    ) -> None:
        """A process already in _known_process_keys should NOT trigger an event."""
        existing_pid, existing_create = 77777, 123456.0
        monitor._known_process_keys = {(existing_pid, existing_create)}

        snapshot = _ProcessSnapshot(
            pid=existing_pid,
            name="old_proc",
            cmdline="old_proc",
            create_time=existing_create,
        )
        key = (snapshot.pid, snapshot.create_time)
        if key not in monitor._known_process_keys:
            monitor._emit_process_event(snapshot)  # should NOT be called

        assert len(collecting_bus.events) == 0


# ---------------------------------------------------------------------------
# Network event emission tests
# ---------------------------------------------------------------------------


class TestNetworkEventEmission:
    """Tests for _emit_network_event and connection detection logic."""

    def test_emit_network_event_with_process(self, monitor: ProcessMonitor, collecting_bus: _CollectingBus) -> None:
        """_emit_network_event() should publish a NETWORK_CALL event with process info."""
        key = _ConnectionKey(
            pid=5555,
            local_addr="192.168.1.1",
            local_port=55555,
            remote_addr="api.openai.com",
            remote_port=443,
            status="ESTABLISHED",
        )
        proc = _ProcessSnapshot(pid=5555, name="python", cmdline="python agent.py", create_time=0.0)
        monitor._emit_network_event(key, proc)

        events = collecting_bus.events_of_type(EventType.NETWORK_CALL)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == EventType.NETWORK_CALL.value
        assert event.remote_host == "api.openai.com"
        assert event.remote_port == 443
        assert event.process_name == "python"
        assert event.process_pid == 5555
        assert "local_addr" in event.metadata
        assert "local_port" in event.metadata
        assert "status" in event.metadata

    def test_emit_network_event_without_process(self, monitor: ProcessMonitor, collecting_bus: _CollectingBus) -> None:
        """_emit_network_event() with no process snapshot should still publish."""
        key = _ConnectionKey(
            pid=0,
            local_addr="127.0.0.1",
            local_port=12345,
            remote_addr="8.8.8.8",
            remote_port=53,
            status="ESTABLISHED",
        )
        monitor._emit_network_event(key, None)

        events = collecting_bus.events_of_type(EventType.NETWORK_CALL)
        assert len(events) == 1
        assert events[0].process_name is None

    def test_new_connection_triggers_event(
        self, monitor: ProcessMonitor, collecting_bus: _CollectingBus
    ) -> None:
        """A connection absent from _known_connection_keys should emit an event."""
        key = _ConnectionKey(
            pid=1234,
            local_addr="10.0.0.1",
            local_port=40000,
            remote_addr="93.184.216.34",
            remote_port=80,
            status="ESTABLISHED",
        )
        monitor._known_connection_keys = set()

        if key not in monitor._known_connection_keys:
            monitor._emit_network_event(key, None)

        events = collecting_bus.events_of_type(EventType.NETWORK_CALL)
        assert len(events) == 1

    def test_known_connection_not_re_emitted(
        self, monitor: ProcessMonitor, collecting_bus: _CollectingBus
    ) -> None:
        """A connection already in _known_connection_keys should not emit again."""
        key = _ConnectionKey(
            pid=1234,
            local_addr="10.0.0.1",
            local_port=40000,
            remote_addr="93.184.216.34",
            remote_port=80,
            status="ESTABLISHED",
        )
        monitor._known_connection_keys = {key}

        if key not in monitor._known_connection_keys:
            monitor._emit_network_event(key, None)  # should NOT be called

        assert len(collecting_bus.events) == 0


# ---------------------------------------------------------------------------
# _connection_to_key tests
# ---------------------------------------------------------------------------


class TestConnectionToKey:
    """Tests for the _connection_to_key helper."""

    def test_valid_connection_returns_key(self) -> None:
        """A fully populated connection should return a valid _ConnectionKey."""
        conn = _make_mock_conn()
        key = _connection_to_key(conn)
        assert key is not None
        assert key.remote_addr == "1.2.3.4"
        assert key.remote_port == 443
        assert key.local_addr == "192.168.1.10"
        assert key.local_port == 54321
        assert key.status == "ESTABLISHED"
        assert key.pid == 1234

    def test_no_remote_addr_returns_none(self) -> None:
        """A connection without a remote address should return None."""
        conn = MagicMock()
        conn.raddr = None
        conn.laddr = MagicMock()
        conn.laddr.ip = "127.0.0.1"
        conn.laddr.port = 1234
        conn.status = "LISTEN"
        conn.pid = 0
        key = _connection_to_key(conn)
        assert key is None

    def test_remote_addr_empty_ip_returns_none(self) -> None:
        """A connection with an empty remote IP should return None."""
        conn = _make_mock_conn(remote_ip="")
        key = _connection_to_key(conn)
        assert key is None

    def test_invalid_remote_port_returns_none(self) -> None:
        """A connection with remote_port=0 should return None."""
        conn = _make_mock_conn(remote_port=0)
        key = _connection_to_key(conn)
        assert key is None

    def test_port_above_65535_returns_none(self) -> None:
        """A connection with remote_port > 65535 should return None."""
        conn = _make_mock_conn(remote_port=99999)
        key = _connection_to_key(conn)
        assert key is None

    def test_no_local_addr_returns_none(self) -> None:
        """A connection without laddr should return None."""
        conn = MagicMock()
        conn.raddr = MagicMock()
        conn.raddr.ip = "1.2.3.4"
        conn.raddr.port = 80
        conn.laddr = None
        conn.status = "ESTABLISHED"
        conn.pid = 1
        key = _connection_to_key(conn)
        assert key is None


# ---------------------------------------------------------------------------
# _is_outbound_connection tests
# ---------------------------------------------------------------------------


class TestIsOutboundConnection:
    """Tests for the _is_outbound_connection helper."""

    def test_established_with_remote_is_outbound(self) -> None:
        """ESTABLISHED connection with a remote address should be outbound."""
        conn = _make_mock_conn(status="ESTABLISHED")
        assert _is_outbound_connection(conn) is True

    def test_syn_sent_is_outbound(self) -> None:
        """SYN_SENT connection should be considered outbound."""
        conn = _make_mock_conn(status="SYN_SENT")
        assert _is_outbound_connection(conn) is True

    def test_listen_is_not_outbound(self) -> None:
        """LISTEN status should not be considered outbound."""
        conn = _make_mock_conn(status="LISTEN")
        assert _is_outbound_connection(conn) is False

    def test_no_remote_addr_is_not_outbound(self) -> None:
        """A connection with no remote address should not be outbound."""
        conn = MagicMock()
        conn.raddr = None
        conn.status = "ESTABLISHED"
        assert _is_outbound_connection(conn) is False

    def test_remote_port_zero_is_not_outbound(self) -> None:
        """A connection with remote port 0 should not be outbound."""
        conn = _make_mock_conn(remote_port=0)
        assert _is_outbound_connection(conn) is False

    def test_close_wait_is_not_outbound(self) -> None:
        """CLOSE_WAIT should not be treated as outbound (connection is dying)."""
        conn = _make_mock_conn(status="CLOSE_WAIT")
        assert _is_outbound_connection(conn) is False


# ---------------------------------------------------------------------------
# _safe_process_snapshot tests
# ---------------------------------------------------------------------------


class TestSafeProcessSnapshot:
    """Tests for the _safe_process_snapshot helper."""

    def test_valid_process_returns_snapshot(self) -> None:
        """A valid psutil Process should return a populated snapshot."""
        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 1234,
            "name": "python3",
            "cmdline": ["python3", "-c", "print('hi')"],
            "create_time": 1700000000.0,
        }
        snapshot = _safe_process_snapshot(mock_proc)
        assert snapshot is not None
        assert snapshot.pid == 1234
        assert snapshot.name == "python3"
        assert "python3" in snapshot.cmdline
        assert snapshot.create_time == 1700000000.0

    def test_no_such_process_returns_none(self) -> None:
        """If psutil raises NoSuchProcess, the helper should return None."""
        mock_proc = MagicMock()
        mock_proc.info = property(lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=0)))
        # Trigger via attribute access
        type(mock_proc).info = property(lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(0)))

        # The simplest way: patch the property
        class _FakeProc:
            @property
            def info(self):  # type: ignore
                raise psutil.NoSuchProcess(pid=99999)

        snap = _safe_process_snapshot(_FakeProc())  # type: ignore
        assert snap is None

    def test_access_denied_returns_none(self) -> None:
        """If psutil raises AccessDenied, the helper should return None."""
        class _FakeProc:
            @property
            def info(self):  # type: ignore
                raise psutil.AccessDenied(pid=1)

        snap = _safe_process_snapshot(_FakeProc())  # type: ignore
        assert snap is None

    def test_empty_cmdline_falls_back_to_name(self) -> None:
        """When cmdline list is empty, the snapshot cmdline should be the name."""
        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 5678,
            "name": "myprog",
            "cmdline": [],
            "create_time": 0.0,
        }
        snap = _safe_process_snapshot(mock_proc)
        assert snap is not None
        assert snap.cmdline == "myprog"


# ---------------------------------------------------------------------------
# _safe_process_snapshot_by_pid tests
# ---------------------------------------------------------------------------


class TestSafeProcessSnapshotByPid:
    """Tests for the _safe_process_snapshot_by_pid helper."""

    def test_real_own_process_returns_snapshot(self) -> None:
        """Looking up the current test process PID should succeed."""
        import os
        pid = os.getpid()
        snap = _safe_process_snapshot_by_pid(pid)
        assert snap is not None
        assert snap.pid == pid
        assert snap.name  # non-empty

    def test_nonexistent_pid_returns_none(self) -> None:
        """A PID that does not exist should return None."""
        # Use a very high PID that almost certainly doesn't exist
        snap = _safe_process_snapshot_by_pid(9999999)
        assert snap is None


# ---------------------------------------------------------------------------
# Integration: monitor detects new processes in real time
# ---------------------------------------------------------------------------


class TestProcessMonitorIntegration:
    """Integration tests that run the real poll loop."""

    def test_monitor_detects_new_process(
        self, collecting_bus: _CollectingBus
    ) -> None:
        """The monitor should emit a SHELL_COMMAND event for a new subprocess."""
        import subprocess
        import sys
        import os

        # Start monitor with filter for 'python' so it only watches Python procs
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            process_filter=["python"],
            poll_interval=0.1,
            track_network=False,
        )
        mon.start()

        # Spawn a short-lived Python subprocess
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Wait up to 3 seconds for the event to appear
            deadline = time.monotonic() + 3.0
            found = False
            while time.monotonic() < deadline:
                shell_events = collecting_bus.events_of_type(EventType.SHELL_COMMAND)
                for evt in shell_events:
                    if evt.process_pid == proc.pid:
                        found = True
                        break
                if found:
                    break
                time.sleep(0.1)
        finally:
            proc.wait(timeout=5)
            mon.stop()

        assert found, (
            f"Expected SHELL_COMMAND event for pid={proc.pid} but got: "
            f"{[(e.process_pid, e.command_line) for e in collecting_bus.events_of_type(EventType.SHELL_COMMAND)]}"
        )

    def test_monitor_with_no_filter_does_not_raise(
        self, collecting_bus: _CollectingBus
    ) -> None:
        """Running the monitor with no process filter should complete a poll without errors."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            poll_interval=0.05,
            track_network=False,
        )
        mon.start()
        time.sleep(0.2)  # Let at least one poll cycle run
        mon.stop()
        # Should not raise; events may or may not have been emitted

    def test_track_processes_false_emits_no_shell_events(
        self, collecting_bus: _CollectingBus
    ) -> None:
        """With track_processes=False, no SHELL_COMMAND events should be published."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            poll_interval=0.05,
            track_processes=False,
            track_network=False,
        )
        mon.start()
        time.sleep(0.2)
        mon.stop()

        assert len(collecting_bus.events_of_type(EventType.SHELL_COMMAND)) == 0

    def test_track_network_false_emits_no_network_events(
        self, collecting_bus: _CollectingBus
    ) -> None:
        """With track_network=False, no NETWORK_CALL events should be published."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            poll_interval=0.05,
            track_processes=False,
            track_network=False,
        )
        mon.start()
        time.sleep(0.2)
        mon.stop()

        assert len(collecting_bus.events_of_type(EventType.NETWORK_CALL)) == 0

    def test_poll_interval_enforced(self, collecting_bus: _CollectingBus) -> None:
        """The minimum poll interval should be capped at 0.1 seconds."""
        mon = ProcessMonitor(
            event_bus=collecting_bus,  # type: ignore[arg-type]
            poll_interval=0.0,  # below minimum
        )
        assert mon._poll_interval >= 0.1
        mon.start()
        mon.stop()
