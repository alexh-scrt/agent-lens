"""Async in-process event bus for AgentLens.

This module provides an ``EventBus`` that fans out ``AgentEvent`` instances
to multiple async subscribers (e.g., SSE connections) while also forwarding
events to a persistent ``EventStore``.

The bus is backed by a single ``asyncio.Queue`` per subscriber, enabling
back-pressure and graceful disconnection. Publishers post events from any
thread via the thread-safe :meth:`EventBus.publish` method.

Example usage (async context)::

    bus = EventBus(store=store)
    await bus.start()

    # In a background thread (e.g., watchdog):
    bus.publish(event)

    # In an async SSE handler:
    async with bus.subscribe() as queue:
        async for event in queue:
            yield event

    await bus.stop()
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Set

from agent_lens.models import AgentEvent

logger = logging.getLogger(__name__)

# Maximum number of events buffered per subscriber queue before dropping.
_SUBSCRIBER_QUEUE_SIZE = 512


class EventBus:
    """Fan-out async event bus that distributes ``AgentEvent`` objects.

    Publishers (filesystem watcher, process monitor) call :meth:`publish`
    from any thread.  Async consumers subscribe via :meth:`subscribe` and
    receive events from a per-subscriber ``asyncio.Queue``.

    The bus also persists every event to an optional :class:`~agent_lens.store.EventStore`.

    Args:
        store: An optional, already-initialized ``EventStore`` to which every
            published event will be persisted.  If ``None``, events are only
            delivered to in-memory subscribers.
        loop: The event loop to use for scheduling coroutines from threads.
            If ``None``, the running loop is captured at :meth:`start` time.
    """

    def __init__(
        self,
        store: Optional[object] = None,  # EventStore – kept as object to avoid circular import
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._store = store
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
        self._subscribers: Set[asyncio.Queue[Optional[AgentEvent]]] = set()
        self._subscribers_lock = threading.Lock()
        self._running = False
        # Internal async queue bridging thread-safe publish → async dispatch
        self._inbound: Optional[asyncio.Queue[Optional[AgentEvent]]] = None
        self._dispatcher_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the event bus dispatcher.

        Must be called from within a running asyncio event loop.  Subsequent
        calls are no-ops if the bus is already running.
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._inbound = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE * 4)
        self._running = True
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(), name="event_bus_dispatcher"
        )
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Gracefully stop the event bus.

        Signals the dispatcher to finish processing queued events and then
        notifies all subscribers with a sentinel ``None`` value so they can
        clean up.  Subsequent calls are no-ops.
        """
        if not self._running:
            return
        self._running = False
        # Sentinel to unblock the dispatcher
        if self._inbound is not None:
            await self._inbound.put(None)
        if self._dispatcher_task is not None:
            try:
                await asyncio.wait_for(self._dispatcher_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("EventBus dispatcher did not stop in time; cancelling")
                self._dispatcher_task.cancel()
        # Notify all subscribers to stop
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    logger.warning("Subscriber queue full when sending stop sentinel")
        logger.info("EventBus stopped")

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, event: AgentEvent) -> None:
        """Publish an event to the bus from any thread.

        This method is thread-safe and non-blocking.  If the internal inbound
        queue is full the event is dropped and a warning is logged.

        Args:
            event: The ``AgentEvent`` to publish.
        """
        if not self._running or self._loop is None or self._inbound is None:
            logger.debug(
                "EventBus not running; dropping event %s", event.event_type
            )
            return
        try:
            # Thread-safe scheduling onto the event loop
            asyncio.run_coroutine_threadsafe(
                self._enqueue(event), self._loop
            )
        except RuntimeError as exc:
            logger.warning("Failed to schedule event publish: %s", exc)

    async def publish_async(self, event: AgentEvent) -> None:
        """Publish an event from within the async event loop.

        Use this method when you are already in an async context (e.g. in
        a FastAPI endpoint) to avoid the overhead of
        ``run_coroutine_threadsafe``.

        Args:
            event: The ``AgentEvent`` to publish.
        """
        if not self._running or self._inbound is None:
            logger.debug(
                "EventBus not running; dropping async event %s", event.event_type
            )
            return
        await self._enqueue(event)

    async def _enqueue(self, event: AgentEvent) -> None:
        """Place an event onto the inbound queue (async, may block briefly)."""
        if self._inbound is None:
            return
        try:
            self._inbound.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "EventBus inbound queue full; dropping event %s", event.event_type
            )

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncGenerator[asyncio.Queue[Optional[AgentEvent]], None]:
        """Context manager that yields a per-subscriber async queue.

        Each subscriber gets its own ``asyncio.Queue``.  Events published to
        the bus are fan-out copied into every active subscriber's queue.
        The queue item is either an ``AgentEvent`` or ``None`` (sentinel
        indicating the bus has stopped).

        Yields:
            An ``asyncio.Queue`` of ``Optional[AgentEvent]``.

        Example::

            async with bus.subscribe() as q:
                while True:
                    event = await q.get()
                    if event is None:
                        break  # bus stopped
                    process(event)
        """
        queue: asyncio.Queue[Optional[AgentEvent]] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        with self._subscribers_lock:
            self._subscribers.add(queue)
        logger.debug(
            "New SSE subscriber added; total=%d", len(self._subscribers)
        )
        try:
            yield queue
        finally:
            with self._subscribers_lock:
                self._subscribers.discard(queue)
            logger.debug(
                "SSE subscriber removed; total=%d", len(self._subscribers)
            )

    @property
    def subscriber_count(self) -> int:
        """Return the number of currently active subscribers."""
        with self._subscribers_lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------
    # Internal dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Consume events from the inbound queue and fan them out.

        Runs until a ``None`` sentinel is received or the bus is stopped.
        """
        assert self._inbound is not None  # noqa: S101
        while True:
            try:
                event = await self._inbound.get()
            except asyncio.CancelledError:
                logger.debug("EventBus dispatcher cancelled")
                break

            if event is None:
                # Sentinel received — stop the loop
                logger.debug("EventBus dispatcher received stop sentinel")
                break

            # Persist to store
            if self._store is not None:
                try:
                    self._store.insert(event)  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to persist event to store: %s", exc)

            # Fan out to all subscribers
            with self._subscribers_lock:
                subscribers = list(self._subscribers)

            for q in subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Subscriber queue full; dropping event %s for one subscriber",
                        event.event_type,
                    )

        logger.debug("EventBus dispatch loop exited")
