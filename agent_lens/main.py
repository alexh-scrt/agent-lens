"""FastAPI application factory, SSE endpoint, and static file mounting for AgentLens.

This module provides:

- ``create_app``: Factory function that builds and configures a FastAPI application
  with all routes, middleware, and static file serving.
- ``/api/events`` (GET): REST endpoint for querying stored events with filters.
- ``/api/events/{event_id}`` (GET): REST endpoint for retrieving a single event.
- ``/api/events/stream`` (GET): Server-Sent Events endpoint for live event streaming.
- ``/api/events`` (POST): Endpoint for manually injecting test events.
- ``/api/stats`` (GET): Summary statistics about the event store.
- Static file serving for the dashboard UI at the root path.

The app is designed to be created once at startup with references to the shared
``EventBus`` and ``EventStore`` instances.

Example usage::

    from agent_lens.store import EventStore
    from agent_lens.event_bus import EventBus
    from agent_lens.main import create_app

    store = EventStore()
    store.initialize()
    bus = EventBus(store=store)
    app = create_app(bus=bus, store=store)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_lens.event_bus import EventBus
from agent_lens.models import AgentEvent, EventFilter, EventType
from agent_lens.store import EventStore

logger = logging.getLogger(__name__)

# Path to the static assets directory
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    bus: Optional[EventBus] = None,
    store: Optional[EventStore] = None,
) -> FastAPI:
    """Create and configure the AgentLens FastAPI application.

    This factory is the primary entry point for constructing the web application.
    It sets up all API routes, CORS middleware, static file serving, and wires
    the provided ``EventBus`` and ``EventStore`` into each endpoint.

    Args:
        bus: An ``EventBus`` instance. If ``None``, a standalone bus without
            persistence is created automatically.
        store: An initialized ``EventStore`` instance.  If ``None``, an
            in-memory store is created and initialized automatically.

    Returns:
        A fully configured :class:`fastapi.FastAPI` application instance.
    """
    # Create defaults if not provided — useful for testing
    if store is None:
        store = EventStore(db_path=":memory:")
        store.initialize()

    if bus is None:
        bus = EventBus(store=store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Manage the EventBus lifecycle tied to the app lifespan."""
        await bus.start()
        logger.info("AgentLens FastAPI application started")
        yield
        await bus.stop()
        logger.info("AgentLens FastAPI application shutdown")

    app = FastAPI(
        title="AgentLens",
        description="Local observability dashboard for AI coding agents",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store references on the app state for access in routes
    app.state.bus = bus
    app.state.store = store

    # ---------------------------------------------------------------------------
    # CORS middleware
    # ---------------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------------------------------------------------------------------
    # API Routes
    # ---------------------------------------------------------------------------

    @app.get("/api/events/stream", response_class=StreamingResponse)
    async def stream_events(request: Request) -> StreamingResponse:
        """Server-Sent Events endpoint for live event streaming.

        Clients connect to this endpoint and receive a continuous stream of
        events as they are published to the ``EventBus``.  The stream stays
        open until the client disconnects.

        Each event is sent as a JSON-encoded SSE ``data:`` line.

        Returns:
            A :class:`~fastapi.responses.StreamingResponse` with
            ``text/event-stream`` content type.
        """
        event_bus: EventBus = request.app.state.bus

        async def event_generator() -> AsyncGenerator[str, None]:
            """Generate SSE-formatted event strings from the bus."""
            # Send a connection confirmation comment
            yield ": AgentLens SSE stream connected\n\n"

            async with event_bus.subscribe() as queue:
                while True:
                    # Check if the client has disconnected
                    if await request.is_disconnected():
                        logger.debug("SSE client disconnected")
                        break

                    try:
                        # Wait for an event with a timeout to allow disconnect checks
                        event: Optional[AgentEvent] = await asyncio.wait_for(
                            queue.get(), timeout=15.0
                        )
                    except asyncio.TimeoutError:
                        # Send a keepalive comment to prevent proxy timeouts
                        yield ": keepalive\n\n"
                        continue
                    except asyncio.CancelledError:
                        break

                    if event is None:
                        # Bus stopped — send close event and exit
                        yield "event: close\ndata: {}\n\n"
                        break

                    try:
                        payload = json.dumps(event.to_sse_dict())
                        yield f"data: {payload}\n\n"
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed to serialize SSE event: %s", exc)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/events")
    async def list_events(
        request: Request,
        event_type: Optional[str] = Query(default=None, description="Filter by event type"),
        process_name: Optional[str] = Query(default=None, description="Filter by process name"),
        path_prefix: Optional[str] = Query(default=None, description="Filter by path prefix"),
        path_contains: Optional[str] = Query(default=None, description="Filter by path substring"),
        since: Optional[str] = Query(default=None, description="ISO datetime lower bound"),
        until: Optional[str] = Query(default=None, description="ISO datetime upper bound"),
        limit: int = Query(default=100, ge=1, le=1000, description="Max events to return"),
        offset: int = Query(default=0, ge=0, description="Pagination offset"),
    ) -> JSONResponse:
        """List stored events with optional filtering and pagination.

        All filter parameters are optional.  Results are returned in ascending
        timestamp order.

        Returns:
            A JSON object with ``events`` (list) and ``total`` (count) fields.

        Raises:
            HTTPException: 400 if filter parameters are invalid.
        """
        event_store: EventStore = request.app.state.store

        try:
            # Build EventType enum value if provided
            et: Optional[EventType] = None
            if event_type is not None:
                try:
                    et = EventType(event_type)
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid event_type '{event_type}'. "
                               f"Valid values: {[e.value for e in EventType]}",
                    )

            event_filter = EventFilter(
                event_type=et,
                process_name=process_name,
                path_prefix=path_prefix,
                path_contains=path_contains,
                since=since,  # type: ignore[arg-type]
                until=until,  # type: ignore[arg-type]
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        events = event_store.query(event_filter)
        total = event_store.count(event_filter)

        return JSONResponse(
            content={
                "events": [e.to_sse_dict() for e in events],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/events/{event_id}")
    async def get_event(request: Request, event_id: str) -> JSONResponse:
        """Retrieve a single event by its UUID.

        Args:
            event_id: The UUID of the event to retrieve.

        Returns:
            The event as a JSON object.

        Raises:
            HTTPException: 404 if no event with the given ID exists.
        """
        event_store: EventStore = request.app.state.store
        event = event_store.get_by_id(event_id)

        if event is None:
            raise HTTPException(
                status_code=404,
                detail=f"Event '{event_id}' not found",
            )

        return JSONResponse(content=event.to_sse_dict())

    @app.post("/api/events", status_code=201)
    async def create_event(request: Request, event: AgentEvent) -> JSONResponse:
        """Manually inject an event into the bus and store.

        This endpoint is primarily useful for testing and external integrations
        that want to push events into AgentLens programmatically.

        Args:
            event: The ``AgentEvent`` payload to inject.

        Returns:
            The created event as a JSON object with HTTP 201 status.
        """
        event_bus: EventBus = request.app.state.bus
        await event_bus.publish_async(event)
        return JSONResponse(content=event.to_sse_dict(), status_code=201)

    @app.get("/api/stats")
    async def get_stats(request: Request) -> JSONResponse:
        """Return summary statistics about the AgentLens event store.

        Returns a JSON object with:
        - ``total_events``: Total number of stored events.
        - ``by_type``: Counts broken down by event type.
        - ``subscriber_count``: Number of active SSE subscribers.

        Returns:
            A JSON object with aggregate statistics.
        """
        event_store: EventStore = request.app.state.store
        event_bus: EventBus = request.app.state.bus

        total = event_store.count()
        by_type: dict[str, int] = {}
        for et in EventType:
            count = event_store.count(EventFilter(event_type=et, limit=1))
            by_type[et.value] = count

        return JSONResponse(
            content={
                "total_events": total,
                "by_type": by_type,
                "subscriber_count": event_bus.subscriber_count,
            }
        )

    @app.get("/api/health")
    async def health_check() -> JSONResponse:
        """Simple health check endpoint.

        Returns:
            A JSON object with ``status: ok``.
        """
        return JSONResponse(content={"status": "ok"})

    # ---------------------------------------------------------------------------
    # Static files (dashboard UI)
    # ---------------------------------------------------------------------------
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def serve_dashboard() -> HTMLResponse:
            """Serve the main dashboard HTML page.

            Returns:
                The ``index.html`` file as an HTML response.

            Raises:
                HTTPException: 404 if the index.html file is not found.
            """
            index_path = _STATIC_DIR / "index.html"
            if not index_path.exists():
                raise HTTPException(status_code=404, detail="Dashboard UI not found")
            return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    else:
        @app.get("/", response_class=HTMLResponse)
        async def serve_dashboard_fallback() -> HTMLResponse:
            """Fallback root handler when static directory is missing."""
            return HTMLResponse(
                content="<h1>AgentLens</h1><p>Static assets not found.</p>"
            )

    return app


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn / import usage)
# ---------------------------------------------------------------------------

# Create a default app instance. In production the CLI overrides this with
# properly configured bus and store instances.
_default_store = EventStore(db_path=":memory:")
_default_store.initialize()
_default_bus = EventBus(store=_default_store)

app = create_app(bus=_default_bus, store=_default_store)
