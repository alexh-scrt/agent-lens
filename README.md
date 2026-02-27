# AgentLens

> A lightweight, local observability dashboard for AI coding agents.

AgentLens watches your filesystem and processes in real time, capturing file reads/writes, shell
commands, and outbound API calls made by autonomous agents like OpenClaw or Aider. Events are
streamed live to a clean web UI via Server-Sent Events (SSE), giving developers a clear audit
trail and timeline visualization of exactly what their AI agents are doing on their machines.

**No cloud. No telemetry. Everything stays local.**

---

## Features

- **Real-time filesystem event capture** — monitors configured watch paths for file reads, writes,
  creates, and deletes attributed to known agent processes.
- **Process and network monitoring** — uses `psutil` to track shell commands spawned by agents and
  outbound API/network connections in near-real-time.
- **Live SSE dashboard** — streams events instantly to a local web UI with a color-coded timeline;
  no polling required.
- **Persistent audit trail** — all events are stored in a local SQLite database with timestamps,
  process info, and full paths, queryable via a REST API.
- **Zero-config CLI** — start monitoring any directory with a single command with optional process
  name filtering.

---

## Requirements

- Python 3.10 or newer
- pip

---

## Installation

### From source (recommended for development)

```bash
git clone https://github.com/example/agent_lens.git
cd agent_lens
pip install -e ".[dev]"
```

### From PyPI (once published)

```bash
pip install agent_lens
```

---

## Quick Start

### Watch a project directory

```bash
agent-lens watch ./my-project
```

This will:
1. Start the AgentLens backend on `http://localhost:8765` by default.
2. Begin monitoring `./my-project` for filesystem events.
3. Open (or print the URL to) the live dashboard in your browser.

### Custom port and multiple paths

```bash
agent-lens watch ./my-project ./another-dir --port 9000
```

### Filter by agent process name

```bash
agent-lens watch ./my-project --process aider --process python
```

### View stored events via REST API

While the server is running:

```bash
# List all events (most recent 100)
curl http://localhost:8765/api/events

# Filter by event type
curl "http://localhost:8765/api/events?event_type=file_write"

# Filter by path prefix
curl "http://localhost:8765/api/events?path_prefix=./my-project/src"

# Limit results
curl "http://localhost:8765/api/events?limit=50"
```

### Live SSE stream

Connect to the SSE endpoint to receive events as they happen:

```bash
curl -N http://localhost:8765/api/events/stream
```

---

## Dashboard UI

Open `http://localhost:8765` in your browser to access the live dashboard.

The dashboard provides:

- **Event timeline** — a scrolling, color-coded list of all captured events with timestamps.
- **Filter controls** — filter by event type (file_create, file_modify, file_delete, shell_command,
  network_call), process name, or path substring.
- **Search** — full-text search across event paths and metadata.
- **Event detail panel** — click any event to see its full details including process PID, command
  line, and file metadata.

### Event type color coding

| Event Type       | Color  | Description                                      |
|------------------|--------|--------------------------------------------------|
| `file_create`    | Green  | A new file was created by an agent process       |
| `file_modify`    | Blue   | An existing file was modified                    |
| `file_delete`    | Red    | A file was deleted                               |
| `file_read`      | Gray   | A file was opened for reading                    |
| `shell_command`  | Yellow | A shell command was spawned                      |
| `network_call`   | Purple | An outbound network connection was detected      |

---

## Architecture

```
agent_lens/
├── __init__.py        # Package init, version
├── main.py            # FastAPI app, SSE endpoint, static files
├── cli.py             # Click CLI entry point
├── models.py          # Pydantic models: AgentEvent, EventType
├── store.py           # SQLite-backed event persistence
├── event_bus.py       # Async in-process event fan-out bus
├── watcher.py         # Watchdog filesystem monitor
├── process_monitor.py # Psutil process + network monitor
└── static/
    ├── index.html     # Dashboard SPA
    ├── app.js         # SSE client + UI logic
    └── style.css      # Dark theme styles
```

### Data flow

```
Filesystem events  ──┐
                     ├──► EventBus ──► SSE subscribers (browser)
Process events     ──┤              └──► SQLite store
                     │
Network events     ──┘
```

---

## Configuration

All configuration is passed via CLI flags. There is no config file required.

| Flag              | Default         | Description                                      |
|-------------------|-----------------|--------------------------------------------------|
| `--port`          | `8765`          | Port to serve the dashboard on                   |
| `--host`          | `127.0.0.1`     | Host to bind the server to                       |
| `--process`       | *(all)*         | Process name(s) to filter on (repeatable)        |
| `--db`            | `agent_lens.db` | Path to SQLite database file                     |
| `--poll-interval` | `1.0`           | Seconds between process monitor polls            |

---

## Development

### Running tests

```bash
pytest
```

### Running tests with coverage

```bash
pip install pytest-cov
pytest --cov=agent_lens --cov-report=term-missing
```

### Running the server in development mode

```bash
uvicorn agent_lens.main:app --reload --port 8765
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Contributing

Contributions are welcome! Please open an issue or pull request on GitHub.

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -am 'Add my feature'`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a pull request.
