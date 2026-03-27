# CortexClaw

> **Status:** Early development — first working milestone tagged as [`v0.0.1`](https://github.com/sfc-gh-mcastro/my-lobster-cortexclaw/releases/tag/v0.0.1)

A [NanoClaw](https://github.com/qwibitai/nanoclaw)-inspired orchestrator built on top of the [Cortex Code Agent SDK](https://docs.snowflake.com/en/user-guide/cortex-code). CortexClaw bridges messaging channels to Snowflake Cortex Code agents, providing a multi-channel AI assistant platform with persistent memory, scheduled tasks, and inter-agent communication.

## How it works

```
Messaging Channels ──► SQLite ──► Polling Loop ──► Cortex Code SDK (subprocess) ──► Response
     (Slack, CLI)                                    (agent_runner.py)
```

1. **Channels** (Slack, CLI terminal) receive messages and store them in SQLite
2. The **orchestrator** polls for new messages in registered groups
3. Messages are formatted as XML and dispatched to a **Cortex Code agent** via the SDK
4. The agent's response streams back and is routed to the originating channel
5. **IPC** (filesystem-based) allows agents to send messages, create tasks, and register groups

### Key differences from NanoClaw

| NanoClaw | CortexClaw |
|---|---|
| TypeScript | Python (asyncio) |
| Claude Agent SDK | Cortex Code Agent SDK |
| Docker containers for isolation | Direct subprocess via SDK |
| WhatsApp, Telegram, Slack, Discord, Gmail | Slack + CLI (extensible via channel registry) |
| `better-sqlite3` (sync) | `aiosqlite` (async) |
| `cron-parser` | `croniter` |

## Project structure

```
cortexclaw/
├── __init__.py
├── __main__.py           # Entry point: python -m cortexclaw
├── config.py             # Configuration from .env / environment variables
├── types.py              # Core dataclasses (RegisteredGroup, NewMessage, Channel ABC, etc.)
├── db.py                 # SQLite persistence layer (aiosqlite)
├── channels/
│   ├── __init__.py       # Auto-imports channel modules for self-registration
│   ├── registry.py       # Channel factory registry pattern
│   ├── slack.py          # Slack channel (slack_bolt async)
│   └── cli.py            # Interactive CLI channel (stdin/stdout)
├── agent_runner.py       # Bridges the orchestrator to Cortex Code SDK
├── group_queue.py        # Per-group concurrency control with global limit + retry
├── router.py             # XML message formatting + outbound routing
├── task_scheduler.py     # Cron/interval/one-shot task scheduling
├── ipc.py                # Filesystem-based inter-process communication
└── orchestrator.py       # Main event loop tying all components together
```

## Prerequisites

- Python 3.10+
- [Cortex Code CLI](https://docs.snowflake.com/en/user-guide/cortex-code) (`cortex`) installed and authenticated
- Cortex Code Agent SDK (`cortex_code_agent_sdk` Python package)

## Setup

1. **Clone the repo:**
   ```bash
   git clone https://github.com/sfc-gh-mcastro/my-lobster-cortexclaw.git
   cd my-lobster-cortexclaw
   ```

2. **Create a virtual environment and install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install aiosqlite croniter python-dotenv
   pip install cortex_code_agent_sdk  # or install from local wheel
   ```

3. **Configure environment variables** (create a `.env` file):
   ```bash
   # Required
   CORTEX_CONNECTION=your_snowflake_connection_name
   
   # Optional
   ASSISTANT_NAME=CortexClaw          # Bot display name (default: CortexClaw)
   CORTEX_CLI_PATH=cortex             # Path to cortex CLI (default: cortex)
   MAX_CONCURRENT_AGENTS=5            # Max parallel agent runs (default: 5)
   ENABLE_CLI_CHANNEL=true            # Enable terminal interaction (default: true)
   
   # Slack (optional — only needed if you want Slack integration)
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

4. **Install Slack SDK** (optional, only if using Slack):
   ```bash
   pip install slack_bolt
   ```

## Running

```bash
# Using the CLI channel (no external services needed):
PYTHONPATH=. python -m cortexclaw
```

You'll see:
```
CortexClaw CLI ready.  Type a message and press Enter.
```

Type a message and press Enter — the orchestrator dispatches it to a Cortex Code agent and prints the response.

### With Slack

Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env`, then run the same command. The orchestrator connects to both CLI and Slack simultaneously.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ASSISTANT_NAME` | `CortexClaw` | Bot name used in messages and triggers |
| `CORTEX_CONNECTION` | *(empty)* | Snowflake connection name for Cortex Code SDK |
| `CORTEX_CLI_PATH` | `cortex` | Path to the Cortex Code CLI binary |
| `POLL_INTERVAL` | `2000` | Message poll interval in ms |
| `MAX_CONCURRENT_AGENTS` | `5` | Maximum parallel agent subprocess runs |
| `IDLE_TIMEOUT` | `1800000` | Agent idle timeout in ms (30 min) |
| `ENABLE_CLI_CHANNEL` | `true` | Enable interactive terminal channel |
| `SLACK_BOT_TOKEN` | *(empty)* | Slack bot token (skips Slack if not set) |
| `SLACK_APP_TOKEN` | *(empty)* | Slack app-level token for Socket Mode |
| `TZ` | system default | IANA timezone for scheduling and message formatting |

## Architecture

### Channel registry

Channels self-register via a factory pattern. To add a new channel (e.g., Discord), create a file in `cortexclaw/channels/` that:
1. Implements the `Channel` ABC
2. Calls `register_channel("discord", factory_fn)` at import time
3. Returns `None` from the factory if credentials are missing

### Group queue

Per-group concurrency control with a global limit. Features:
- Messages and tasks are queued per-group
- Tasks are prioritized over messages during drain
- Exponential backoff retry (max 5 retries, base 5s)
- Waiting queue for groups blocked by the concurrency limit

### IPC system

Agents can communicate back to the orchestrator by writing JSON files to `data/ipc/{group_folder}/`:
- `messages/*.json` — send messages to channels
- `tasks/*.json` — create/pause/resume/delete scheduled tasks, register groups

### Per-group instructions

Place a `CLAUDE.md` file in `groups/{folder}/` to give the agent custom instructions for that group. The agent runner prepends it to every prompt.

## Development

### Setup

```bash
# Install with dev dependencies
pip install -e ".[dev]"
```

### Running tests

```bash
# Full test suite with coverage
pytest --cov=cortexclaw --cov-report=term-missing

# Single test file
pytest tests/test_router.py -v
```

### Linting & formatting

```bash
# Check formatting
ruff format --check .

# Auto-format
ruff format .

# Lint
ruff check .

# Lint with auto-fix
ruff check --fix .
```

### CI/CD

Pull requests run lint + tests automatically via GitHub Actions (`.github/workflows/ci.yml`). Tests run against Python 3.10, 3.11, and 3.12.

Merges to `main` auto-bump the patch version and create a git tag (`.github/workflows/bump-version.yml`).

## Releases

### v0.0.1 — First working version

The initial milestone with the core orchestrator fully functional:

- Multi-channel messaging (Slack + interactive CLI)
- Cortex Code agent dispatch via the SDK (subprocess)
- SQLite persistence for messages, groups, sessions, and scheduled tasks
- Per-group concurrency control with global limit and retry
- Session continuity — agents resume prior conversations within a group via `--resume <session_id>`
- Cron / interval / one-shot task scheduler with `context_mode` support (`group` or `isolated`)
- Filesystem-based IPC for inter-agent communication
- Self-registering channel factory pattern (easy to add new channels)

### Reverting to a tagged version

To check out a specific release:

```bash
# List available tags
git tag -l

# Check out v0.0.1
git checkout v0.0.1

# Or create a branch from the tag to work on it
git checkout -b my-branch v0.0.1
```

## Acknowledgments

Architecture inspired by [NanoClaw](https://github.com/qwibitai/nanoclaw) by [Qwibit AI](https://github.com/qwibitai). Built on the [Cortex Code Agent SDK](https://docs.snowflake.com/en/user-guide/cortex-code) by Snowflake.
