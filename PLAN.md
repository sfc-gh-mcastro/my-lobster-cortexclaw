# CortexClaw Implementation Plan

## Context

**NanoClaw** is a TypeScript orchestrator that bridges messaging channels to Claude agents running in Docker containers. Key patterns: channel registry (self-registering factory), SQLite persistence, `GroupQueue` (per-group concurrency + retry), message router (XML formatting), IPC (filesystem inter-agent communication), and cron/interval task scheduler.

**Cortex Code Agent SDK** (v0.1.0) is a Python SDK wrapping the `cortex` CLI subprocess. Provides `query()` (one-shot) and `CortexCodeSDKClient` (multi-turn), plus permission callbacks, hooks, and in-process MCP tools.

The plan ports NanoClaw's full orchestrator to Python as **CortexClaw**, replacing Docker with direct Cortex Code SDK calls. Includes Slack as the messaging channel plus a CLI channel for terminal interaction and testing.

---

## Implementation Steps

### Step 1: Project scaffolding
```
cortexclaw/
├── __init__.py
├── __main__.py                # python -m cortexclaw
├── config.py                  # Env vars / .env config
├── types.py                   # Core dataclasses
├── db.py                      # SQLite persistence (aiosqlite)
├── channels/
│   ├── __init__.py            # Auto-imports all channel modules
│   ├── registry.py            # Channel registry (factory pattern)
│   ├── slack.py               # Slack channel (slack_bolt async)
│   └── cli.py                 # CLI channel (stdin/stdout interactive)
├── agent_runner.py            # Wraps Cortex Code SDK
├── group_queue.py             # Async concurrency + retry
├── router.py                  # Message formatting + routing
├── task_scheduler.py          # Cron/interval/one-shot scheduling
├── ipc.py                     # Filesystem IPC watcher
└── orchestrator.py            # Main event loop
```

### Step 2: Types (`cortexclaw/types.py`)
Port NanoClaw's interfaces to Python dataclasses:
- `RegisteredGroup`, `NewMessage`, `ScheduledTask`, `TaskRunLog`, `ContainerConfig`
- `Channel` ABC: `connect()`, `send_message()`, `is_connected()`, `owns_jid()`, `disconnect()`, optional `set_typing()`, `sync_groups()`
- Callback protocols: `OnInboundMessage`, `OnChatMetadata`, `OnRegisterGroup`

### Step 3: Configuration (`cortexclaw/config.py`)
Load from `.env` + environment:
- `ASSISTANT_NAME`, `POLL_INTERVAL` (2s), `SCHEDULER_POLL_INTERVAL` (60s), `IDLE_TIMEOUT` (30min), `MAX_CONCURRENT_AGENTS` (5), `TIMEZONE`
- `STORE_DIR`, `GROUPS_DIR`, `DATA_DIR`
- `CORTEX_CONNECTION`, `CORTEX_CLI_PATH`
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`
- `ENABLE_CLI_CHANNEL` (default true)
- Trigger pattern builder

### Step 4: SQLite persistence (`cortexclaw/db.py`)
Port using `aiosqlite`:
- Schema: `chats`, `messages`, `registered_groups`, `scheduled_tasks`, `task_run_logs`, `router_state`, `sessions`
- Migration pattern (ALTER TABLE try/except)
- All CRUD: `init_database()`, `store_message()`, `get_new_messages()`, `store_chat_metadata()`, group/task/session management, `get/set_router_state()`

### Step 5: Channel registry (`cortexclaw/channels/registry.py`)
Self-registering factory pattern:
- `register_channel(name, factory)` — factory receives `ChannelOpts`
- Returns `Channel | None` (None = skip gracefully if credentials missing)

### Step 6: Slack channel (`cortexclaw/channels/slack.py`)
`Channel` ABC via `slack_bolt` async:
- JID format: `slack:{channel_id}`
- `send_message()` via `chat_postMessage`
- `sync_groups()` via `conversations_list`
- Self-registers at import; returns `None` if `SLACK_BOT_TOKEN` not set

### Step 7: CLI channel (`cortexclaw/channels/cli.py`)
Interactive terminal channel for development and testing:
- JID format: `cli:default` (single "group" representing the terminal session)
- Auto-registers a default group on `connect()`
- Reads from stdin via `asyncio` reader (non-blocking)
- `send_message()` writes formatted output to stdout
- Self-registers; returns `None` if `ENABLE_CLI_CHANNEL` is false

### Step 8: Agent runner (`cortexclaw/agent_runner.py`)
Replace Docker with Cortex Code SDK:
- `run_agent(group, prompt, chat_jid, on_output)`:
  1. `CortexCodeAgentOptions(cwd=group_dir, connection=CORTEX_CONNECTION, can_use_tool=auto_approve)`
  2. `query()` to stream results
  3. Collect `AssistantMessage` text, call `on_output` callback
  4. Return success/error
- Group instructions from `GROUPS_DIR/{folder}/CLAUDE.md` prepended to prompt

### Step 9: Group queue (`cortexclaw/group_queue.py`)
Async port of `GroupQueue`:
- Per-group state (active, pending_messages, pending_tasks, retry_count)
- Global concurrency limit (`MAX_CONCURRENT_AGENTS`, default 5)
- Exponential backoff retry (max 5, base 5s)
- Tasks prioritized over messages during drain

### Step 10: Message router (`cortexclaw/router.py`)
- `format_messages()` — XML formatting
- `strip_internal_tags()` — remove `<internal>...</internal>`
- `find_channel()`, `route_outbound()`

### Step 11: Task scheduler (`cortexclaw/task_scheduler.py`)
- `compute_next_run()` — cron (`croniter`), interval (anchored to prevent drift), one-shot
- `start_scheduler_loop()` — poll every `SCHEDULER_POLL_INTERVAL`
- `run_task()` — dispatch to agent runner, log, compute next run

### Step 12: IPC system (`cortexclaw/ipc.py`)
Filesystem IPC:
- `DATA_DIR/ipc/{group_folder}/messages/*.json`, `.../tasks/*.json`
- Poll, process (send messages, schedule tasks, register groups)
- Authorization: non-main groups restricted to own chat

### Step 13: Orchestrator (`cortexclaw/orchestrator.py`)
Main loop:
1. Load `.env`, init DB, load state
2. Create `GroupQueue` with `process_messages_fn`
3. Connect channels via registry (Slack + CLI)
4. Start IPC watcher + task scheduler
5. Message polling loop
6. SIGINT/SIGTERM graceful shutdown

---

## Verification

1. **CLI smoke test**: Run `python -m cortexclaw`, type a message, verify agent responds
2. **Unit tests**: `GroupQueue` concurrency/retry, `router.format_messages()` XML, `compute_next_run()`, `db` CRUD
3. **Slack integration**: Set `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`, send a trigger message, verify agent responds
4. **Concurrency**: Enqueue 6+ simultaneous messages, confirm only `MAX_CONCURRENT_AGENTS` run at once
5. **IPC round-trip**: Agent writes a task file, orchestrator schedules it
6. **Scheduler**: Create a one-shot task via IPC, verify it fires and result is logged

## Critical Files

- `cortexclaw/orchestrator.py` — Main event loop (NanoClaw's index.ts equivalent)
- `cortexclaw/agent_runner.py` — Bridge to Cortex Code SDK (replaces container-runner.ts)
- `cortexclaw/group_queue.py` — Concurrency control, retry, task prioritization
- `cortexclaw/db.py` — All persistence
- `cortexclaw/channels/cli.py` — CLI channel for terminal interaction and testing
- `cortexclaw/channels/slack.py` — Slack channel implementation
