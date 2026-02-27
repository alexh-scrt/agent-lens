"""Click-based CLI entry point for AgentLens.

This module provides the ``main`` Click group and the ``watch`` subcommand
that wires together all AgentLens components (EventStore, EventBus,
FileSystemWatcher, ProcessMonitor, and the FastAPI application) into a
single cohesive startup sequence.

Usage examples::

    # Watch a single directory on the default port
    agent-lens watch ./my-project

    # Watch multiple directories on a custom port
    agent-lens watch ./project ./docs --port 9000

    # Filter by agent process names
    agent-lens watch ./my-project --process aider --process python

    # Use a persistent database file
    agent-lens watch ./my-project --db /var/log/agent_lens.db

    # Adjust process poll interval
    agent-lens watch ./my-project --poll-interval 2.0

    # Disable process or network monitoring
    agent-lens watch ./my-project --no-process-monitor
    agent-lens watch ./my-project --no-network-monitor
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple

import click
import uvicorn

from agent_lens.event_bus import EventBus
from agent_lens.main import create_app
from agent_lens.process_monitor import ProcessMonitor
from agent_lens.store import EventStore
from agent_lens.watcher import FileSystemWatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup helper
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    """Configure root logging level and format.

    Args:
        verbose: If ``True``, set level to DEBUG; otherwise INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    # Quieten noisy third-party loggers unless in verbose mode
    if not verbose:
        logging.getLogger("watchdog").setLevel(logging.WARNING)
        logging.getLogger("uvicorn").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.error").setLevel(logging.INFO)
        logging.getLogger("fastapi").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Click CLI definition
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="agent_lens", prog_name="agent-lens")
def main() -> None:
    """AgentLens — local observability dashboard for AI coding agents.

    Watches your filesystem and processes in real time, streaming captured
    events to a local web dashboard via Server-Sent Events.

    All data is stored locally in SQLite. No telemetry, no cloud.
    """


@main.command()
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=False, file_okay=False, dir_okay=True, resolve_path=True),
    metavar="PATH...",
)
@click.option(
    "--port",
    default=8765,
    show_default=True,
    type=click.IntRange(1, 65535),
    help="Port to serve the dashboard on.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind the server to.",
)
@click.option(
    "--process",
    "-p",
    multiple=True,
    metavar="NAME",
    help="Process name(s) to monitor (repeatable). Omit to monitor all.",
)
@click.option(
    "--db",
    default="agent_lens.db",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the SQLite database file. Use ':memory:' for ephemeral storage.",
)
@click.option(
    "--poll-interval",
    default=1.0,
    show_default=True,
    type=click.FloatRange(min=0.1),
    help="Seconds between process monitor poll cycles.",
)
@click.option(
    "--no-process-monitor",
    is_flag=True,
    default=False,
    help="Disable process monitoring (no shell_command events).",
)
@click.option(
    "--no-network-monitor",
    is_flag=True,
    default=False,
    help="Disable network connection monitoring (no network_call events).",
)
@click.option(
    "--no-open",
    is_flag=True,
    default=False,
    help="Do not print the dashboard URL on startup.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose/debug logging.",
)
def watch(
    paths: Tuple[str, ...],
    port: int,
    host: str,
    process: Tuple[str, ...],
    db: str,
    poll_interval: float,
    no_process_monitor: bool,
    no_network_monitor: bool,
    no_open: bool,
    verbose: bool,
) -> None:
    """Watch PATH(s) for AI agent activity and stream events to the dashboard.

    At least one PATH should be provided (a directory to monitor). If no PATH
    is given the current working directory is used as the default.

    \b
    Examples:
        agent-lens watch ./my-project
        agent-lens watch ./project ./docs --port 9000
        agent-lens watch ./project --process aider --process python
        agent-lens watch ./project --db /tmp/agent.db --poll-interval 0.5
    """
    _configure_logging(verbose)

    # Default to current working directory when no paths are provided
    watch_paths: list[str] = list(paths) if paths else [str(Path.cwd())]
    process_filter: list[str] = list(process)

    # Validate and warn about non-existent paths (watcher handles them too,
    # but giving early feedback is friendlier)
    valid_paths: list[str] = []
    for p in watch_paths:
        resolved = Path(p).resolve()
        if not resolved.exists():
            click.echo(
                click.style(
                    f"  Warning: path does not exist and will be skipped: {p}",
                    fg="yellow",
                ),
                err=True,
            )
        elif not resolved.is_dir():
            click.echo(
                click.style(
                    f"  Warning: path is not a directory and will be skipped: {p}",
                    fg="yellow",
                ),
                err=True,
            )
        else:
            valid_paths.append(str(resolved))

    # ---------------------------------------------------------------------------
    # Print startup banner
    # ---------------------------------------------------------------------------
    click.echo()
    click.echo(click.style("  AgentLens", fg="cyan", bold=True) + " — local AI agent observability")
    click.echo()

    click.echo(f"  {'Database:':<22}" + click.style(db, fg="white"))

    if valid_paths:
        click.echo(f"  {'Watching:':<22}" + click.style(", ".join(valid_paths), fg="white"))
    else:
        click.echo(
            click.style("  No valid watch paths found. File events will not be captured.", fg="yellow")
        )

    if process_filter:
        click.echo(f"  {'Process filter:':<22}" + click.style(", ".join(process_filter), fg="white"))
    else:
        click.echo(f"  {'Process filter:':<22}" + click.style("<all processes>", fg="white"))

    click.echo(f"  {'Poll interval:':<22}" + click.style(f"{poll_interval}s", fg="white"))
    click.echo(f"  {'Process monitor:':<22}" + click.style(
        "disabled" if no_process_monitor else "enabled", fg="white"
    ))
    click.echo(f"  {'Network monitor:':<22}" + click.style(
        "disabled" if no_network_monitor else "enabled", fg="white"
    ))

    dashboard_url = f"http://{host}:{port}"
    click.echo()
    click.echo("  Dashboard: " + click.style(dashboard_url, fg="cyan", underline=True))
    click.echo()
    click.echo(click.style("  Press Ctrl+C to stop.", fg="bright_black"))
    click.echo()

    # ---------------------------------------------------------------------------
    # Component assembly
    # ---------------------------------------------------------------------------

    # 1. Event store
    store = EventStore(db_path=db)
    try:
        store.initialize()
    except Exception as exc:
        click.echo(
            click.style(f"  Error: Failed to initialize database at '{db}': {exc}", fg="red"),
            err=True,
        )
        sys.exit(1)

    # 2. Event bus (wires store to bus)
    bus = EventBus(store=store)

    # 3. Filesystem watcher (only if we have valid paths)
    watcher: Optional[FileSystemWatcher] = None
    if valid_paths:
        watcher = FileSystemWatcher(
            paths=valid_paths,
            event_bus=bus,
            process_filter=process_filter if process_filter else None,
        )

    # 4. Process monitor
    monitor: Optional[ProcessMonitor] = None
    if not no_process_monitor:
        monitor = ProcessMonitor(
            event_bus=bus,
            process_filter=process_filter if process_filter else None,
            poll_interval=poll_interval,
            track_network=not no_network_monitor,
            track_processes=True,
        )

    # 5. FastAPI application
    app = create_app(bus=bus, store=store)

    # ---------------------------------------------------------------------------
    # Uvicorn server configuration
    # ---------------------------------------------------------------------------
    uv_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="debug" if verbose else "warning",
        access_log=verbose,
        lifespan="on",
    )
    server = uvicorn.Server(config=uv_config)

    # ---------------------------------------------------------------------------
    # Graceful shutdown handling
    # ---------------------------------------------------------------------------
    shutdown_event = threading.Event()

    def _handle_shutdown_signal(signum: int, frame: object) -> None:
        """Handle SIGINT/SIGTERM by signalling all components to stop."""
        click.echo(click.style("\n  Shutting down AgentLens...", fg="yellow"), err=True)
        shutdown_event.set()
        server.should_exit = True

    # Register signal handlers (only in the main thread)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    # ---------------------------------------------------------------------------
    # Startup sequence: start background components first, then uvicorn
    # ---------------------------------------------------------------------------
    try:
        # Start filesystem watcher (background thread)
        if watcher is not None:
            try:
                watcher.start()
                logger.info("Filesystem watcher started for %d path(s)", len(valid_paths))
            except Exception as exc:
                click.echo(
                    click.style(f"  Warning: Failed to start filesystem watcher: {exc}", fg="yellow"),
                    err=True,
                )
                watcher = None

        # Start process monitor (background thread)
        if monitor is not None:
            try:
                monitor.start()
                logger.info("Process monitor started")
            except Exception as exc:
                click.echo(
                    click.style(f"  Warning: Failed to start process monitor: {exc}", fg="yellow"),
                    err=True,
                )
                monitor = None

        # Run uvicorn (blocks until server exits)
        # uvicorn manages the asyncio event loop internally, which means the
        # EventBus lifespan hooks are driven by the FastAPI lifespan context.
        server.run()

    except KeyboardInterrupt:
        # Fallback in case signal handler is not installed
        click.echo(click.style("\n  Interrupted.", fg="yellow"), err=True)
    except Exception as exc:
        click.echo(
            click.style(f"  Fatal error: {exc}", fg="red", bold=True),
            err=True,
        )
        logger.exception("Unexpected error during server run")
        sys.exit(1)
    finally:
        # ---------------------------------------------------------------------------
        # Shutdown sequence: stop background components cleanly
        # ---------------------------------------------------------------------------
        if monitor is not None:
            try:
                monitor.stop()
                logger.info("Process monitor stopped")
            except Exception as exc:
                logger.warning("Error stopping process monitor: %s", exc)

        if watcher is not None:
            try:
                watcher.stop()
                logger.info("Filesystem watcher stopped")
            except Exception as exc:
                logger.warning("Error stopping filesystem watcher: %s", exc)

        try:
            store.close()
            logger.info("Event store closed")
        except Exception as exc:
            logger.warning("Error closing event store: %s", exc)

        click.echo(click.style("  AgentLens stopped.", fg="bright_black"), err=True)


# ---------------------------------------------------------------------------
# Additional convenience subcommands
# ---------------------------------------------------------------------------


@main.command(name="events")
@click.option(
    "--db",
    default="agent_lens.db",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the SQLite database file to query.",
)
@click.option(
    "--event-type",
    "-t",
    default=None,
    type=click.Choice(
        ["file_create", "file_modify", "file_delete", "file_read", "shell_command", "network_call"],
        case_sensitive=False,
    ),
    help="Filter by event type.",
)
@click.option(
    "--limit",
    "-n",
    default=50,
    show_default=True,
    type=click.IntRange(1, 1000),
    help="Maximum number of events to display.",
)
@click.option(
    "--process",
    "-p",
    default=None,
    help="Filter by process name.",
)
@click.option(
    "--path-contains",
    default=None,
    help="Filter events whose path contains this substring.",
)
def list_events(
    db: str,
    event_type: Optional[str],
    limit: int,
    process: Optional[str],
    path_contains: Optional[str],
) -> None:
    """Query and display stored events from the AgentLens database.

    This command reads directly from the SQLite database without starting
    the server — useful for post-hoc inspection of agent activity.

    \b
    Examples:
        agent-lens events
        agent-lens events --event-type file_create --limit 20
        agent-lens events --process aider
        agent-lens events --db /tmp/agent.db --path-contains src/
    """
    db_path = Path(db)
    if not db_path.exists():
        click.echo(
            click.style(f"Database not found: {db}", fg="red"),
            err=True,
        )
        sys.exit(1)

    from agent_lens.models import EventFilter, EventType as ET

    store = EventStore(db_path=str(db_path))
    try:
        store.initialize()
    except Exception as exc:
        click.echo(click.style(f"Failed to open database: {exc}", fg="red"), err=True)
        sys.exit(1)

    try:
        et: Optional[ET] = ET(event_type) if event_type else None
        event_filter = EventFilter(
            event_type=et,
            process_name=process,
            path_contains=path_contains,
            limit=limit,
        )
        events = store.query(event_filter)
        total = store.count()

        if not events:
            click.echo(click.style("No events found.", fg="yellow"))
            return

        # ---------------------------------------------------------------------------
        # Colour map for event type badges
        # ---------------------------------------------------------------------------
        _COLORS = {
            "file_create":   "green",
            "file_modify":   "blue",
            "file_delete":   "red",
            "file_read":     "white",
            "shell_command": "yellow",
            "network_call":  "magenta",
        }

        click.echo()
        click.echo(
            click.style(f"  Showing {len(events)} of {total} total events", fg="bright_black")
        )
        click.echo()

        for evt in events:
            ts = evt.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            et_str = evt.event_type if isinstance(evt.event_type, str) else evt.event_type.value
            color = _COLORS.get(et_str, "white")
            badge = click.style(f"[{et_str:<14}]", fg=color)

            primary = ""
            if evt.path:
                primary = evt.path
            elif evt.command_line:
                primary = evt.command_line[:80]
            elif evt.remote_host:
                primary = f"{evt.remote_host}:{evt.remote_port or '?'}"

            proc = ""
            if evt.process_name:
                proc = click.style(
                    f" ({evt.process_name}:{evt.process_pid or '?'})",
                    fg="bright_black",
                )

            click.echo(f"  {click.style(ts, fg='bright_black')} {badge} {primary}{proc}")

        click.echo()
    finally:
        store.close()


@main.command(name="stats")
@click.option(
    "--db",
    default="agent_lens.db",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the SQLite database file to query.",
)
def show_stats(db: str) -> None:
    """Display aggregate statistics about a stored AgentLens database.

    \b
    Examples:
        agent-lens stats
        agent-lens stats --db /tmp/agent.db
    """
    db_path = Path(db)
    if not db_path.exists():
        click.echo(
            click.style(f"Database not found: {db}", fg="red"),
            err=True,
        )
        sys.exit(1)

    from agent_lens.models import EventFilter, EventType as ET

    store = EventStore(db_path=str(db_path))
    try:
        store.initialize()
    except Exception as exc:
        click.echo(click.style(f"Failed to open database: {exc}", fg="red"), err=True)
        sys.exit(1)

    try:
        total = store.count()

        click.echo()
        click.echo(click.style("  AgentLens Event Statistics", fg="cyan", bold=True))
        click.echo(click.style(f"  Database: {db}", fg="bright_black"))
        click.echo()
        click.echo(f"  {'Total events:':<24}" + click.style(str(total), fg="white", bold=True))
        click.echo()

        _COLORS = {
            "file_create":   "green",
            "file_modify":   "blue",
            "file_delete":   "red",
            "file_read":     "white",
            "shell_command": "yellow",
            "network_call":  "magenta",
        }

        for et in ET:
            count = store.count(EventFilter(event_type=et, limit=1))
            color = _COLORS.get(et.value, "white")
            bar = click.style("█" * min(count, 40), fg=color) if count > 0 else ""
            label = click.style(f"{et.value:<18}", fg=color)
            click.echo(f"  {label} {str(count):>6}  {bar}")

        click.echo()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Entry point guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
