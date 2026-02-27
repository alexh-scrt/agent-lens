"""AgentLens — a lightweight, local observability dashboard for AI coding agents.

This package provides real-time filesystem and process monitoring for autonomous
AI agents, streaming captured events to a local web dashboard via Server-Sent Events.
All data is stored locally in SQLite; no telemetry or cloud connectivity is required.

Example usage::

    # Via CLI
    agent-lens watch ./my-project --port 8765

    # Programmatic usage
    from agent_lens.main import create_app
    app = create_app(watch_paths=["/path/to/project"])
"""

__version__ = "0.1.0"
__author__ = "AgentLens Contributors"
__license__ = "MIT"

__all__ = ["__version__", "__author__", "__license__"]
