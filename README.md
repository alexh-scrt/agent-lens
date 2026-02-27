# AgentLens 🔍

> See exactly what your AI agents are doing — in real time, on your machine.

AgentLens is a lightweight, local observability dashboard for AI coding agents. It watches your filesystem and processes in real time, capturing file reads/writes, shell commands, and outbound API calls made by autonomous agents like Aider or OpenClaw. Events stream instantly to a clean web UI via Server-Sent Events, giving you a full audit trail and timeline of every action your agent takes — with zero cloud involvement.

**No cloud. No telemetry. Everything stays local.**

---

## Quick Start

**Install:**

```bash
pip install agent-lens
```

Or install from source:

```bash
git clone https://github.com/your-org/agent-lens
cd agent-lens
pip install -e .
```

**Start monitoring:**

```bash
agent-lens watch ./my-project
```

Then open your browser at [http://localhost:8000](http://localhost:8000) — the dashboard is live.

---

## Features

- **Real-time filesystem monitoring** — captures file creates, writes, modifies, and deletes across any watched directory, attributed to known agent processes.
- **Process & network tracking** — uses `psutil` to detect shell commands spawned by agents and outbound API/network connections as they happen.
- **Live SSE dashboard** — events stream instantly to a color-coded timeline in your browser; no polling, no refresh.
- **Persistent audit trail** — every event is stored in a local SQLite database with full timestamps, process info, and file paths, queryable via a REST API.
- **Zero-config CLI** — start monitoring any directory with a single command; optional flags for port, process filtering, and database path.

---

## Usage Examples

### CLI

```bash
# Watch a single project directory (default port 8000)
agent-lens watch ./my-project

# Watch multiple directories on a custom port
agent-lens watch ./project ./docs --port 9000

# Filter events to specific agent process names
agent-lens watch ./my-project --process aider --process python

# Persist events to a specific database file
agent-lens watch ./my-project --db /var/log/agent_lens.db
```

### REST API

Once running, the dashboard exposes a REST API alongside the UI:

```bash
# List recent events
curl http://localhost:8000/api/events

# Filter by event type
curl "http://localhost:8000/api/events?event_type=FILE_WRITE&limit=50"

# Get a specific event by ID
curl http://localhost:8000/api/events/<event-id>

# View aggregate stats
curl http://localhost:8000/api/stats
```

### Programmatic Usage

```python
from agent_lens.main import create_app
from agent_lens.store import EventStore
from agent_lens.event_bus import EventBus

store = EventStore(db_path="agent_lens.db")
store.initialize()

bus = EventBus(store=store)
app = create_app(bus=bus, store=store)
```

### SSE Stream

Connect directly to the live event stream from any client:

```javascript
const stream = new EventSource('http://localhost:8000/api/events/stream');
stream.onmessage = (e) => {
  const event = JSON.parse(e.data);
  console.log(event.event_type, event.path);
};
```

---

## Project Structure

```
agent-lens/
├── pyproject.toml                  # Project metadata and dependencies
├── README.md
│
├── agent_lens/
│   ├── __init__.py                 # Package init, version, entry point
│   ├── cli.py                      # Click CLI: `agent-lens watch`
│   ├── main.py                     # FastAPI app factory, routes, SSE endpoint
│   ├── models.py                   # Pydantic models: AgentEvent, EventType, EventFilter
│   ├── store.py                    # SQLite-backed persistent event store
│   ├── event_bus.py                # Async event bus (asyncio.Queue fan-out)
│   ├── watcher.py                  # Watchdog filesystem monitor
│   ├── process_monitor.py          # Psutil process & network monitor
│   └── static/
│       ├── index.html              # Single-page dashboard UI
│       ├── app.js                  # SSE client, timeline rendering, filters
│       └── style.css               # Dark theme, color-coded event badges
│
└── tests/
    ├── test_models.py              # Pydantic model validation tests
    ├── test_store.py               # SQLite store unit tests
    ├── test_watcher.py             # Filesystem watcher integration tests
    ├── test_process_monitor.py     # ProcessMonitor unit tests
    └── test_api.py                 # FastAPI REST and SSE endpoint tests
```

---

## Configuration

All configuration is passed via CLI flags. No config file is required.

| Flag | Default | Description |
|---|---|---|
| `paths` | _(required)_ | One or more directories to watch |
| `--port` | `8000` | Port for the web dashboard |
| `--host` | `127.0.0.1` | Host to bind the server to |
| `--process` | _(all)_ | Filter by agent process name (repeatable) |
| `--db` | `:memory:` | Path to SQLite database file for persistence |
| `--poll-interval` | `1.0` | Process monitor polling interval in seconds |

**Examples:**

```bash
# Ephemeral (in-memory) — events lost on exit
agent-lens watch ./project

# Persistent — events survive restarts
agent-lens watch ./project --db ~/.agent_lens/events.db

# Bind to all interfaces (e.g., for Docker)
agent-lens watch ./project --host 0.0.0.0 --port 8080
```

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Built with [Jitter](https://github.com/jitter-ai) — an AI agent that ships code daily.*
