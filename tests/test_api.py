"""FastAPI endpoint tests for the REST and SSE interfaces.

Covers:
- GET /api/health — basic health check.
- GET /api/events — listing events with various filter parameters.
- GET /api/events/{event_id} — single event retrieval.
- POST /api/events — manual event injection.
- GET /api/stats — aggregate statistics.
- GET / — dashboard HTML serving.
- GET /api/events/stream — SSE streaming (connection and event delivery).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from agent_lens.event_bus import EventBus
from agent_lens.main import create_app
from agent_lens.models import AgentEvent, EventType
from agent_lens.store import EventStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> Generator[EventStore, None, None]:
    """Provide an initialized in-memory EventStore."""
    s = EventStore(db_path=":memory:")
    s.initialize()
    yield s
    s.close()


@pytest.fixture()
def bus(store: EventStore) -> EventBus:
    """Provide an EventBus wired to the store (not started — TestClient handles lifespan)."""
    return EventBus(store=store)


@pytest.fixture()
def app(bus: EventBus, store: EventStore):
    """Provide a configured FastAPI application."""
    return create_app(bus=bus, store=store)


@pytest.fixture()
def client(app) -> Generator[TestClient, None, None]:
    """Provide a synchronous TestClient with lifespan management."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def make_file_event(
    event_type: EventType = EventType.FILE_CREATE,
    path: str = "/tmp/test.py",
    process_name: str = "aider",
    process_pid: int = 1000,
    **kwargs,
) -> AgentEvent:
    """Factory for file-type AgentEvents."""
    return AgentEvent(
        event_type=event_type,
        path=path,
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
    """Factory for network_call AgentEvents."""
    return AgentEvent(
        event_type=EventType.NETWORK_CALL,
        remote_host=remote_host,
        remote_port=remote_port,
        process_name=process_name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for GET /api/health."""

    def test_health_returns_ok(self, client: TestClient) -> None:
        """Health endpoint should return 200 with status: ok."""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------


class TestDashboardEndpoint:
    """Tests for GET / (dashboard HTML)."""

    def test_root_returns_html(self, client: TestClient) -> None:
        """Root path should return an HTML response."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_root_contains_agent_lens(self, client: TestClient) -> None:
        """The HTML response should contain 'AgentLens'."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "AgentLens" in resp.text


# ---------------------------------------------------------------------------
# GET /api/events — listing events
# ---------------------------------------------------------------------------


class TestListEventsEndpoint:
    """Tests for GET /api/events."""

    def test_empty_store_returns_empty_list(self, client: TestClient) -> None:
        """Listing events on an empty store should return an empty list."""
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["total"] == 0

    def test_list_events_response_structure(self, client: TestClient, store: EventStore) -> None:
        """Response should have 'events', 'total', 'limit', and 'offset' keys."""
        store.insert(make_file_event())
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_list_returns_inserted_events(self, client: TestClient, store: EventStore) -> None:
        """Events inserted into the store should appear in the listing."""
        events = [
            make_file_event(path="/tmp/a.py"),
            make_file_event(path="/tmp/b.py"),
            make_file_event(path="/tmp/c.py"),
        ]
        store.insert_many(events)
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 3
        assert data["total"] == 3

    def test_filter_by_event_type(self, client: TestClient, store: EventStore) -> None:
        """event_type query param should filter results."""
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/a"))
        store.insert(make_file_event(event_type=EventType.FILE_MODIFY, path="/b"))
        store.insert(make_file_event(event_type=EventType.FILE_DELETE, path="/c"))

        resp = client.get("/api/events", params={"event_type": "file_create"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "file_create"

    def test_filter_by_invalid_event_type_returns_400(self, client: TestClient) -> None:
        """An invalid event_type should return HTTP 400."""
        resp = client.get("/api/events", params={"event_type": "not_real"})
        assert resp.status_code == 400

    def test_filter_by_process_name(self, client: TestClient, store: EventStore) -> None:
        """process_name query param should filter results."""
        store.insert(make_file_event(path="/a", process_name="aider"))
        store.insert(make_file_event(path="/b", process_name="python"))
        store.insert(make_file_event(path="/c", process_name="aider"))

        resp = client.get("/api/events", params={"process_name": "aider"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2
        assert all(e["process_name"] == "aider" for e in data["events"])

    def test_filter_by_path_prefix(self, client: TestClient, store: EventStore) -> None:
        """path_prefix query param should filter results."""
        store.insert(make_file_event(path="/src/main.py"))
        store.insert(make_file_event(path="/tests/test_main.py"))
        store.insert(make_file_event(path="/src/utils.py"))

        resp = client.get("/api/events", params={"path_prefix": "/src"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2
        for e in data["events"]:
            assert e["path"].startswith("/src")

    def test_filter_by_path_contains(self, client: TestClient, store: EventStore) -> None:
        """path_contains query param should filter results."""
        store.insert(make_file_event(path="/src/main.py"))
        store.insert(make_file_event(path="/src/utils.py"))
        store.insert(make_file_event(path="/tests/test_main.py"))

        resp = client.get("/api/events", params={"path_contains": "tests"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 1
        assert "tests" in data["events"][0]["path"]

    def test_limit_parameter(self, client: TestClient, store: EventStore) -> None:
        """limit query param should cap the number of results."""
        events = [make_file_event(path=f"/tmp/f{i}.py") for i in range(10)]
        store.insert_many(events)

        resp = client.get("/api/events", params={"limit": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 3

    def test_offset_parameter(self, client: TestClient, store: EventStore) -> None:
        """offset query param should skip events."""
        events = [make_file_event(path=f"/tmp/f{i}.py") for i in range(5)]
        store.insert_many(events)

        resp_all = client.get("/api/events")
        resp_offset = client.get("/api/events", params={"offset": 2})

        all_events = resp_all.json()["events"]
        offset_events = resp_offset.json()["events"]

        assert len(offset_events) == 3
        assert offset_events[0]["id"] == all_events[2]["id"]

    def test_limit_above_max_returns_400(self, client: TestClient) -> None:
        """A limit above 1000 should return HTTP 422 (validation error)."""
        resp = client.get("/api/events", params={"limit": 9999})
        assert resp.status_code == 422

    def test_offset_negative_returns_400(self, client: TestClient) -> None:
        """A negative offset should return HTTP 422 (validation error)."""
        resp = client.get("/api/events", params={"offset": -1})
        assert resp.status_code == 422

    def test_events_have_all_expected_fields(self, client: TestClient, store: EventStore) -> None:
        """Each event in the response should have all expected fields."""
        store.insert(make_file_event())
        resp = client.get("/api/events")
        data = resp.json()
        event = data["events"][0]
        expected_keys = {
            "id", "event_type", "timestamp", "path", "process_name",
            "process_pid", "command_line", "remote_host", "remote_port", "metadata",
        }
        assert expected_keys.issubset(event.keys())

    def test_timestamps_are_iso_strings(self, client: TestClient, store: EventStore) -> None:
        """Timestamps in the response should be ISO 8601 strings."""
        store.insert(make_file_event())
        resp = client.get("/api/events")
        event = resp.json()["events"][0]
        # Should be parseable as datetime
        dt = datetime.fromisoformat(event["timestamp"])
        assert dt.tzinfo is not None

    def test_since_filter(self, client: TestClient, store: EventStore) -> None:
        """'since' filter should exclude older events."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        old = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/old.py",
            timestamp=now - timedelta(hours=2),
        )
        new = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/new.py",
            timestamp=now,
        )
        store.insert_many([old, new])

        cutoff = (now - timedelta(hours=1)).isoformat()
        resp = client.get("/api/events", params={"since": cutoff})
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["path"] == "/new.py"

    def test_until_filter(self, client: TestClient, store: EventStore) -> None:
        """'until' filter should exclude newer events."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        old = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/old.py",
            timestamp=now - timedelta(hours=2),
        )
        new = AgentEvent(
            event_type=EventType.FILE_CREATE,
            path="/new.py",
            timestamp=now,
        )
        store.insert_many([old, new])

        cutoff = (now - timedelta(hours=1)).isoformat()
        resp = client.get("/api/events", params={"until": cutoff})
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["path"] == "/old.py"

    def test_combined_filters(self, client: TestClient, store: EventStore) -> None:
        """Multiple filters should all be applied together."""
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/src/a.py", process_name="aider"))
        store.insert(make_file_event(event_type=EventType.FILE_MODIFY, path="/src/b.py", process_name="aider"))
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/tests/a.py", process_name="python"))

        resp = client.get("/api/events", params={
            "event_type": "file_create",
            "process_name": "aider",
        })
        data = resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "file_create"
        assert data["events"][0]["process_name"] == "aider"


# ---------------------------------------------------------------------------
# GET /api/events/{event_id} — single event retrieval
# ---------------------------------------------------------------------------


class TestGetEventEndpoint:
    """Tests for GET /api/events/{event_id}."""

    def test_get_existing_event(self, client: TestClient, store: EventStore) -> None:
        """A stored event should be retrievable by ID."""
        event = make_file_event(path="/tmp/specific.py")
        store.insert(event)

        resp = client.get(f"/api/events/{event.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == event.id
        assert data["path"] == "/tmp/specific.py"

    def test_get_nonexistent_event_returns_404(self, client: TestClient) -> None:
        """Requesting an unknown event ID should return HTTP 404."""
        resp = client.get("/api/events/does-not-exist-xyz")
        assert resp.status_code == 404

    def test_get_event_has_all_fields(self, client: TestClient, store: EventStore) -> None:
        """The retrieved event should have all expected fields."""
        event = make_network_event()
        store.insert(event)

        resp = client.get(f"/api/events/{event.id}")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "id", "event_type", "timestamp", "path", "process_name",
            "process_pid", "command_line", "remote_host", "remote_port", "metadata",
        }
        assert expected_keys.issubset(data.keys())

    def test_get_event_values_match(self, client: TestClient, store: EventStore) -> None:
        """The retrieved event's values should match the inserted event."""
        event = AgentEvent(
            id="fixed-test-id",
            event_type=EventType.NETWORK_CALL,
            remote_host="example.com",
            remote_port=8080,
            process_name="curl",
            process_pid=12345,
            metadata={"tls": True},
        )
        store.insert(event)

        resp = client.get("/api/events/fixed-test-id")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "fixed-test-id"
        assert data["event_type"] == "network_call"
        assert data["remote_host"] == "example.com"
        assert data["remote_port"] == 8080
        assert data["process_name"] == "curl"
        assert data["metadata"] == {"tls": True}


# ---------------------------------------------------------------------------
# POST /api/events — manual event injection
# ---------------------------------------------------------------------------


class TestCreateEventEndpoint:
    """Tests for POST /api/events."""

    def test_create_file_event_returns_201(self, client: TestClient) -> None:
        """Creating a valid event should return HTTP 201."""
        payload = {
            "event_type": "file_create",
            "path": "/tmp/new_file.py",
            "process_name": "test",
            "process_pid": 1234,
        }
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 201

    def test_create_event_response_has_id(self, client: TestClient) -> None:
        """The response should include the event's ID."""
        payload = {
            "event_type": "file_modify",
            "path": "/tmp/modified.py",
        }
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert len(data["id"]) > 0

    def test_create_network_event(self, client: TestClient, store: EventStore) -> None:
        """A valid network_call event should be stored and retrievable."""
        payload = {
            "event_type": "network_call",
            "remote_host": "api.anthropic.com",
            "remote_port": 443,
            "process_name": "python",
        }
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 201
        event_id = resp.json()["id"]

        # Verify it was persisted (may need brief wait for bus dispatch)
        import time
        time.sleep(0.15)
        stored = store.get_by_id(event_id)
        assert stored is not None
        assert stored.remote_host == "api.anthropic.com"

    def test_create_event_with_invalid_type_returns_422(self, client: TestClient) -> None:
        """An invalid event_type should return HTTP 422."""
        payload = {
            "event_type": "not_real_type",
            "path": "/tmp/x.py",
        }
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 422

    def test_create_file_event_missing_path_returns_422(self, client: TestClient) -> None:
        """A file event without a path should return HTTP 422."""
        payload = {"event_type": "file_create"}  # no path
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 422

    def test_create_network_event_missing_host_returns_422(self, client: TestClient) -> None:
        """A network_call event without remote_host should return HTTP 422."""
        payload = {"event_type": "network_call", "remote_port": 443}
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 422

    def test_create_shell_command_event(self, client: TestClient) -> None:
        """A shell_command event should be accepted without a path."""
        payload = {
            "event_type": "shell_command",
            "command_line": "git status",
            "process_name": "bash",
        }
        resp = client.post("/api/events", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["event_type"] == "shell_command"
        assert data["command_line"] == "git status"


# ---------------------------------------------------------------------------
# GET /api/stats — statistics
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    """Tests for GET /api/stats."""

    def test_stats_returns_200(self, client: TestClient) -> None:
        """Stats endpoint should return HTTP 200."""
        resp = client.get("/api/stats")
        assert resp.status_code == 200

    def test_stats_response_structure(self, client: TestClient) -> None:
        """Stats response should have 'total_events', 'by_type', and 'subscriber_count'."""
        resp = client.get("/api/stats")
        data = resp.json()
        assert "total_events" in data
        assert "by_type" in data
        assert "subscriber_count" in data

    def test_stats_by_type_has_all_event_types(self, client: TestClient) -> None:
        """'by_type' should include a count for every EventType."""
        resp = client.get("/api/stats")
        data = resp.json()
        by_type = data["by_type"]
        for et in EventType:
            assert et.value in by_type, f"Missing event type in stats: {et.value}"

    def test_stats_total_reflects_store(self, client: TestClient, store: EventStore) -> None:
        """Total events count should reflect the number of events in the store."""
        events = [
            make_file_event(path=f"/tmp/f{i}.py") for i in range(4)
        ]
        store.insert_many(events)

        resp = client.get("/api/stats")
        data = resp.json()
        assert data["total_events"] == 4

    def test_stats_by_type_counts(self, client: TestClient, store: EventStore) -> None:
        """Per-type counts should match actual stored data."""
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/a"))
        store.insert(make_file_event(event_type=EventType.FILE_CREATE, path="/b"))
        store.insert(make_file_event(event_type=EventType.FILE_MODIFY, path="/c"))
        store.insert(make_network_event())

        resp = client.get("/api/stats")
        data = resp.json()
        assert data["by_type"]["file_create"] == 2
        assert data["by_type"]["file_modify"] == 1
        assert data["by_type"]["network_call"] == 1
        assert data["by_type"]["file_delete"] == 0

    def test_stats_subscriber_count_is_integer(self, client: TestClient) -> None:
        """subscriber_count should be a non-negative integer."""
        resp = client.get("/api/stats")
        data = resp.json()
        assert isinstance(data["subscriber_count"], int)
        assert data["subscriber_count"] >= 0


# ---------------------------------------------------------------------------
# GET /api/events/stream — SSE endpoint
# ---------------------------------------------------------------------------


class TestSSEEndpoint:
    """Tests for the GET /api/events/stream SSE endpoint."""

    def test_sse_returns_event_stream_content_type(self, client: TestClient) -> None:
        """The SSE endpoint should return text/event-stream content type."""
        with client.stream("GET", "/api/events/stream") as resp:
            assert resp.status_code == 200
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct

    def test_sse_sends_initial_comment(self, client: TestClient) -> None:
        """The SSE stream should send an initial connection comment."""
        lines = []
        with client.stream("GET", "/api/events/stream") as resp:
            for i, line in enumerate(resp.iter_lines()):
                lines.append(line)
                if i >= 2:  # Read a few lines then break
                    break

        # First line should be a SSE comment (starts with ':')
        assert any(line.startswith(":") for line in lines)

    def test_sse_headers_include_cache_control(self, client: TestClient) -> None:
        """SSE response should include Cache-Control: no-cache."""
        with client.stream("GET", "/api/events/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "no-cache"

    def test_sse_streams_published_event(self, app, store: EventStore) -> None:
        """An event published to the bus should appear in the SSE stream."""
        event_data_lines = []

        with TestClient(app, raise_server_exceptions=True) as client:
            # Start streaming in a thread
            import threading

            received_event = []
            stop_flag = threading.Event()

            def stream_reader():
                try:
                    with client.stream("GET", "/api/events/stream") as resp:
                        for line in resp.iter_lines():
                            if line.startswith("data:"):
                                payload = line[len("data:"):].strip()
                                try:
                                    evt = json.loads(payload)
                                    received_event.append(evt)
                                except json.JSONDecodeError:
                                    pass
                            if received_event or stop_flag.is_set():
                                break
                except Exception:
                    pass

            t = threading.Thread(target=stream_reader, daemon=True)
            t.start()

            # Give the stream time to connect
            time.sleep(0.2)

            # Inject an event via the POST endpoint
            payload = {
                "event_type": "file_create",
                "path": "/tmp/sse_test.py",
                "process_name": "test",
            }
            client.post("/api/events", json=payload)

            # Wait for the event to arrive
            t.join(timeout=3.0)
            stop_flag.set()

        assert len(received_event) >= 1, "No event received via SSE"
        assert received_event[0]["path"] == "/tmp/sse_test.py"


# ---------------------------------------------------------------------------
# Application factory tests
# ---------------------------------------------------------------------------


class TestAppFactory:
    """Tests for the create_app factory function."""

    def test_create_app_with_no_args(self) -> None:
        """create_app() with no arguments should create a working app."""
        from agent_lens.main import create_app
        test_app = create_app()
        with TestClient(test_app, raise_server_exceptions=True) as c:
            resp = c.get("/api/health")
            assert resp.status_code == 200

    def test_create_app_with_custom_store(self) -> None:
        """create_app() with a provided store should use it."""
        from agent_lens.main import create_app
        custom_store = EventStore(db_path=":memory:")
        custom_store.initialize()
        test_app = create_app(store=custom_store)

        # Insert an event directly into the store
        custom_store.insert(make_file_event(path="/custom/path.py"))

        with TestClient(test_app, raise_server_exceptions=True) as c:
            resp = c.get("/api/events")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 1
            assert data["events"][0]["path"] == "/custom/path.py"

        custom_store.close()

    def test_app_has_cors_headers(self) -> None:
        """The app should include CORS headers in responses."""
        from agent_lens.main import create_app
        test_app = create_app()
        with TestClient(test_app, raise_server_exceptions=True) as c:
            resp = c.options(
                "/api/health",
                headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
            )
            # CORS preflight or regular request — check header is present
            # The middleware adds it to actual responses too
            resp2 = c.get("/api/health", headers={"Origin": "http://localhost:3000"})
            assert "access-control-allow-origin" in resp2.headers
