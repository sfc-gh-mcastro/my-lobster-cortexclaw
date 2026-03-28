# CortexClaw Architecture

## High-level overview

CortexClaw is an orchestrator that bridges messaging channels to Snowflake Cortex Code agents. Messages arrive from channels (Slack, CLI), get persisted in SQLite, pass through a concurrency-controlled queue, and are dispatched to isolated Docker containers running the Cortex Code CLI.

```mermaid
graph TB
    subgraph Channels
        CLI[CLI Channel<br/>stdin/stdout]
        Slack[Slack Channel<br/>Socket Mode]
    end

    subgraph Orchestrator
        Router[Message Router]
        Queue[GroupQueue<br/>Concurrency Control]
        DB[(SQLite)]
        Scheduler[Task Scheduler]
        IPC[IPC Watcher]
        GC[Groups Config<br/>groups.toml]
    end

    subgraph Agent Execution
        AR[Agent Runner]
        DW[Docker Wrapper]
        Docker[Docker Container]
        Cortex[Cortex Code CLI]
    end

    CLI -->|on_message| Router
    Slack -->|on_message| Router
    Router -->|store_message| DB
    Router -->|enqueue| Queue
    Queue -->|process_group_messages| AR
    AR -->|Docker mode| DW
    AR -->|Host mode| Cortex
    DW -->|docker run| Docker
    Docker --> Cortex
    Cortex -->|response| AR
    AR -->|send_message| CLI
    AR -->|send_message| Slack
    Scheduler -->|enqueue_task| Queue
    Scheduler -->|poll due tasks| DB
    IPC -->|messages/tasks| Router
    GC -->|register_group| DB
```

## Message flow

This is the complete path a message takes from user input to agent response.

```mermaid
sequenceDiagram
    participant U as User
    participant CH as Channel<br/>(CLI/Slack)
    participant O as Orchestrator
    participant DB as SQLite
    participant GQ as GroupQueue
    participant AR as Agent Runner
    participant DK as Docker Container
    participant CC as Cortex Code CLI

    U->>CH: Type message
    CH->>O: on_message(chat_jid, msg)
    O->>DB: store_message(msg)
    O->>GQ: enqueue_message_check(jid)

    alt Group is active
        GQ-->>GQ: Set pending_messages=true<br/>(coalesce)
    else At concurrency limit
        GQ-->>GQ: Add to waiting_groups FIFO
    else Free slot available
        GQ->>O: _process_group_messages(jid)
    end

    O->>DB: get_messages_since(jid, cursor)
    DB-->>O: [NewMessage, ...]

    opt requires_trigger=true
        O->>O: Check trigger regex match
    end

    O->>O: format_messages() as XML
    O->>O: Resolve resume session_id

    O->>AR: run_agent(group, prompt, jid, on_output)

    alt Docker enabled
        AR->>AR: create_docker_wrapper(group)
        AR->>DK: exec wrapper.sh cortex <args>
        DK->>CC: cortex --resume <session_id> ...
    else Host mode
        AR->>CC: cortex --resume <session_id> ...
    end

    CC-->>AR: Stream AssistantMessage chunks
    CC-->>AR: ResultMessage (session_id, status)

    AR->>CH: on_output → send_message(jid, text)
    CH->>U: Display response

    AR->>DB: set_session(folder, session_id)
    O->>DB: save cursor (last_agent_timestamp)
    GQ->>GQ: _drain_group() → next task/message/waiting
```

## Docker isolation

Each agent run is isolated in a fresh Docker container. The SDK's `cli_path` option accepts any executable, so a per-group shell wrapper script transparently redirects `cortex` calls into Docker.

```mermaid
graph LR
    subgraph Host
        SDK[Cortex Code<br/>Agent SDK<br/>Python]
        Wrapper[wrapper.sh<br/>per group]
        TOML[~/.snowflake/<br/>connections.toml<br/>16 connections]
        Key[~/.ssh/<br/>key.p8]
        GroupDir[groups/sales/]
        IPCDir[data/ipc/sales/]
    end

    subgraph Docker Container
        CortexCLI[cortex CLI<br/>beta channel]
        MountedTOML[/home/coco/.snowflake/<br/>connections.toml<br/>1 connection only]
        MountedKey[/home/coco/.snowflake/<br/>key.p8]
        WorkDir[/workspace/group]
        IPCMount[/workspace/ipc]
    end

    SDK -->|cli_path=wrapper.sh| Wrapper
    Wrapper -->|docker run --rm -i| CortexCLI
    TOML -.->|extract single<br/>connection| MountedTOML
    Key -.->|mount RO| MountedKey
    GroupDir -.->|mount RW| WorkDir
    IPCDir -.->|mount RW| IPCMount
```

### Credential extraction flow

```mermaid
flowchart TD
    A[Host connections.toml<br/>16 connections] --> B[extract_connection_config]
    B --> C{Connection uses<br/>SNOWFLAKE_JWT?}
    C -->|Yes| D[Resolve private_key_path<br/>Rewrite to container path]
    C -->|No| E[Write minimal TOML<br/>single connection only]
    D --> E
    E --> F[ConnectionMount<br/>toml_path + key_path]
    F --> G[build_docker_args]
    G --> H["-v toml:container_toml:ro<br/>-v key:container_key:ro<br/>-v group_dir:/workspace/group:rw<br/>-v ipc_dir:/workspace/ipc:rw"]
    H --> I[create_docker_wrapper]
    I --> J[wrapper.sh<br/>exec docker run ... cortex &quot;$@&quot;]
```

## Group registration

Groups can be registered statically (config file), dynamically (channels, IPC), or loaded from the database on restart.

```mermaid
flowchart TD
    subgraph Startup
        S1[orchestrator.main]
        S2[db.init_database]
        S3[_load_state<br/>load groups from DB]
        S4[load_groups_config<br/>parse groups.toml]
        S5[Channel.connect<br/>auto-register]
    end

    subgraph Runtime
        R1[IPC register_group<br/>from main agent]
        R2[Slack channel<br/>per-channel group]
    end

    subgraph Registration
        REG[_register_group<br/>jid, group]
        MEM[_registered_groups dict]
        DBST[(SQLite)]
        DIR[groups/folder/<br/>+ logs/]
    end

    S1 --> S2 --> S3
    S3 -->|restore| MEM
    S1 --> S4
    S4 -->|static:key JIDs<br/>skip if already in DB| REG
    S1 --> S5
    S5 -->|cli:default| REG
    R1 -->|dynamic JID| REG
    R2 -->|slack:channel_id| REG
    REG --> MEM
    REG --> DBST
    REG --> DIR
```

### JID naming conventions

| Source | JID format | Example |
|---|---|---|
| Static config | `static:<key>` | `static:sales` |
| CLI channel | `cli:default` | `cli:default` |
| Slack channel | `slack:<channel_id>` | `slack:C05ABC123` |
| IPC registration | custom | `custom:eng-team` |

## GroupQueue concurrency control

The GroupQueue serializes work per group while enforcing a global concurrency limit across all groups.

```mermaid
stateDiagram-v2
    [*] --> Idle

    Idle --> Running: enqueue_message_check<br/>(slot available)
    Idle --> Waiting: enqueue_message_check<br/>(at limit)
    Idle --> Running: enqueue_task<br/>(slot available)

    Running --> Draining: Agent completes

    Draining --> RunningTask: Pending tasks?
    RunningTask --> Draining: Task completes

    Draining --> RunningMessages: Pending messages?<br/>(no pending tasks)
    RunningMessages --> Draining: Messages processed

    Draining --> Idle: Nothing pending

    Waiting --> Running: Slot freed by<br/>_drain_waiting()

    Running --> RetryBackoff: Agent error<br/>(retry_count < 5)
    RetryBackoff --> Running: After exponential<br/>backoff delay

    Running --> Idle: Agent error<br/>(retry_count >= 5)

    note right of Running
        MAX_CONCURRENT_AGENTS = 5
        Tasks drain before messages
    end note
```

### Priority order during drain

1. **Pending tasks** (scheduled tasks are higher priority)
2. **Pending messages** (coalesced while group was active)
3. **Waiting groups** (FIFO queue of groups blocked by concurrency limit)

## IPC and task scheduling

Agents communicate back to the orchestrator through filesystem-based IPC. Scheduled tasks are persisted in SQLite and polled by the task scheduler.

```mermaid
flowchart TD
    subgraph Agent in Docker
        A1[Agent writes JSON file]
    end

    subgraph "IPC Directory (data/ipc/{folder}/)"
        MSG[messages/*.json]
        TSK[tasks/*.json]
    end

    subgraph IPC Watcher
        W1[Poll every 1s]
        W2[_process_message_ipc]
        W3[_process_task_ipc]
    end

    subgraph Actions
        SEND[_send_message<br/>via channel]
        CREATE[db.create_task]
        PAUSE[db.update_task<br/>status=paused]
        RESUME[db.update_task<br/>status=active]
        DELETE[db.delete_task]
        RGRP[_register_group<br/>main groups only]
    end

    A1 --> MSG
    A1 --> TSK
    W1 --> MSG
    W1 --> TSK
    MSG --> W2
    TSK --> W3
    W2 --> SEND
    W3 -->|schedule_task| CREATE
    W3 -->|pause_task| PAUSE
    W3 -->|resume_task| RESUME
    W3 -->|delete_task| DELETE
    W3 -->|register_group| RGRP
```

### Task scheduler flow

```mermaid
sequenceDiagram
    participant S as Task Scheduler<br/>(polling every 60s)
    participant DB as SQLite
    participant GQ as GroupQueue
    participant AR as Agent Runner
    participant CH as Channel

    loop Every SCHEDULER_POLL_INTERVAL
        S->>DB: get_due_tasks()<br/>WHERE status=active AND next_run <= now
        DB-->>S: [ScheduledTask, ...]

        loop For each due task
            S->>GQ: enqueue_task(jid, task_id, _run_task)
            GQ->>AR: _run_task(task)

            alt context_mode = "group"
                AR->>AR: Resume group session
            else context_mode = "isolated"
                AR->>AR: Fresh session (no resume)
            end

            AR->>AR: run_agent(group, task.prompt)
            AR->>CH: Send result via channel
            AR->>DB: Log run in task_run_logs

            alt schedule_type = "cron"
                AR->>DB: Compute next_run via croniter
            else schedule_type = "interval"
                AR->>DB: Anchor-based next_run<br/>(prevents drift)
            else schedule_type = "once"
                AR->>DB: Set status=completed
            end
        end
    end
```

## Data persistence

All state is persisted in a single SQLite database via `aiosqlite`.

```mermaid
erDiagram
    messages {
        text id PK
        text chat_jid
        text sender
        text sender_name
        text content
        text timestamp
        int is_from_me
        int is_bot_message
    }

    registered_groups {
        text jid PK
        text name
        text folder
        text trigger
        text added_at
        text container_config_json
        int requires_trigger
        int is_main
    }

    sessions {
        text group_folder PK
        text session_id
    }

    router_state {
        text key PK
        text value
    }

    scheduled_tasks {
        text id PK
        text group_folder
        text chat_jid
        text prompt
        text schedule_type
        text schedule_value
        text context_mode
        text script
        text next_run
        text last_run
        text last_result
        text status
        text created_at
    }

    task_run_logs {
        text task_id FK
        text run_at
        int duration_ms
        text status
        text result
        text error
    }

    chat_metadata {
        text jid PK
        text timestamp
        text name
        text channel
        int is_group
    }

    registered_groups ||--o{ messages : "chat_jid"
    registered_groups ||--o| sessions : "folder"
    registered_groups ||--o{ scheduled_tasks : "group_folder"
    scheduled_tasks ||--o{ task_run_logs : "task_id"
    registered_groups ||--o| chat_metadata : "jid"
```

## Directory structure at runtime

```
project-root/
├── cortexclaw/                    # Source code
├── groups/                        # Per-group working directories
│   ├── cli-default/               #   CLI channel default group
│   │   ├── CLAUDE.md              #   Optional per-group instructions
│   │   └── logs/
│   ├── sales/                     #   Static group from groups.toml
│   │   ├── CLAUDE.md
│   │   └── logs/
│   └── eng/
│       └── logs/
├── data/
│   ├── ipc/                       # IPC filesystem interface
│   │   ├── cli-default/
│   │   │   ├── messages/          #   Agent writes JSON here
│   │   │   └── tasks/
│   │   └── sales/
│   │       ├── messages/
│   │       └── tasks/
│   ├── docker-credentials/        # Temp TOML files (single connection)
│   │   └── my-connection.toml
│   └── wrappers/                  # Generated Docker wrapper scripts
│       ├── cli-default.sh
│       └── sales.sh
├── store/
│   └── cortexclaw.db              # SQLite database
├── groups.toml                    # Static group configuration
├── Dockerfile                     # Agent container image
└── .env                           # Environment configuration
```
