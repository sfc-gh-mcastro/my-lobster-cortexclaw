"""SQLite persistence layer for CortexClaw.

Ported from NanoClaw's src/db.ts — uses aiosqlite for async access.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .config import ASSISTANT_NAME, STORE_DIR
from .types import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

_db: Optional[aiosqlite.Connection] = None

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TEXT,
    channel TEXT,
    is_group INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
    is_bot_message INTEGER DEFAULT 0,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    group_folder TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    prompt TEXT NOT NULL,
    script TEXT,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    context_mode TEXT DEFAULT 'isolated',
    next_run TEXT,
    last_run TEXT,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

CREATE TABLE IF NOT EXISTS router_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    group_folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registered_groups (
    jid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config TEXT,
    requires_trigger INTEGER DEFAULT 1,
    is_main INTEGER DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def init_database(db_path: Path | None = None) -> None:
    """Open (or create) the SQLite database and apply the schema."""
    global _db
    if db_path is None:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        db_path = STORE_DIR / "messages.db"
    _db = await aiosqlite.connect(str(db_path))
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _db.commit()


async def close_database() -> None:
    """Close the database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialised — call init_database() first")
    return _db


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


async def store_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None = None,
    channel: str | None = None,
    is_group: bool | None = None,
) -> None:
    db = _get_db()
    group_val = None if is_group is None else (1 if is_group else 0)
    display_name = name or chat_jid
    await db.execute(
        """INSERT INTO chats (jid, name, last_message_time, channel, is_group)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(jid) DO UPDATE SET
             name = COALESCE(excluded.name, chats.name),
             last_message_time = MAX(chats.last_message_time, excluded.last_message_time),
             channel = COALESCE(excluded.channel, chats.channel),
             is_group = COALESCE(excluded.is_group, chats.is_group)""",
        (chat_jid, display_name, timestamp, channel, group_val),
    )
    await db.commit()


async def get_all_chats() -> list[dict[str, Any]]:
    db = _get_db()
    cursor = await db.execute(
        "SELECT jid, name, last_message_time, channel, is_group "
        "FROM chats ORDER BY last_message_time DESC"
    )
    return [dict(row) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def store_message(msg: NewMessage) -> None:
    db = _get_db()
    await db.execute(
        """INSERT OR REPLACE INTO messages
           (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
            1 if msg.is_bot_message else 0,
        ),
    )
    await db.commit()


async def get_new_messages(
    jids: list[str],
    last_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> tuple[list[NewMessage], str]:
    """Fetch new non-bot messages since *last_timestamp* for the given JIDs."""
    if not jids:
        return [], last_timestamp
    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    sql = f"""
        SELECT * FROM (
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
            FROM messages
            WHERE timestamp > ? AND chat_jid IN ({placeholders})
              AND is_bot_message = 0 AND content NOT LIKE ?
              AND content != '' AND content IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
        ) ORDER BY timestamp
    """
    params: list[Any] = [last_timestamp, *jids, f"{bot_prefix}:%", limit]
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    messages = [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]
    new_ts = messages[-1].timestamp if messages else last_timestamp
    return messages, new_ts


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> list[NewMessage]:
    """Get messages for a single chat since a timestamp."""
    msgs, _ = await get_new_messages([chat_jid], since_timestamp, bot_prefix, limit)
    return msgs


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


async def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups")
    groups: dict[str, RegisteredGroup] = {}
    for row in await cursor.fetchall():
        config = None
        if row["container_config"]:
            try:
                config_data = json.loads(row["container_config"])
                from .types import ContainerConfig

                config = ContainerConfig(**config_data)
            except Exception:
                pass
        groups[row["jid"]] = RegisteredGroup(
            name=row["name"],
            folder=row["folder"],
            trigger=row["trigger_pattern"],
            added_at=row["added_at"],
            container_config=config,
            requires_trigger=bool(row["requires_trigger"]),
            is_main=bool(row["is_main"]),
        )
    return groups


async def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    db = _get_db()
    config_json = (
        json.dumps(
            {"timeout": group.container_config.timeout}
            if group.container_config
            else {}
        )
        if group.container_config
        else None
    )
    await db.execute(
        """INSERT OR REPLACE INTO registered_groups
           (jid, name, folder, trigger_pattern, added_at, container_config,
            requires_trigger, is_main)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            config_json,
            1 if group.requires_trigger else 0,
            1 if group.is_main else 0,
        ),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def get_all_sessions() -> dict[str, str]:
    db = _get_db()
    cursor = await db.execute("SELECT group_folder, session_id FROM sessions")
    return {row["group_folder"]: row["session_id"] for row in await cursor.fetchall()}


async def set_session(group_folder: str, session_id: str) -> None:
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


async def get_router_state(key: str) -> str | None:
    db = _get_db()
    cursor = await db.execute(
        "SELECT value FROM router_state WHERE key = ?", (key,)
    )
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_router_state(key: str, value: str) -> None:
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


async def get_all_tasks() -> list[ScheduledTask]:
    db = _get_db()
    cursor = await db.execute("SELECT * FROM scheduled_tasks")
    return [
        ScheduledTask(
            id=row["id"],
            group_folder=row["group_folder"],
            chat_jid=row["chat_jid"],
            prompt=row["prompt"],
            script=row["script"],
            schedule_type=row["schedule_type"],
            schedule_value=row["schedule_value"],
            context_mode=row["context_mode"] or "isolated",
            next_run=row["next_run"],
            last_run=row["last_run"],
            last_result=row["last_result"],
            status=row["status"],
            created_at=row["created_at"],
        )
        for row in await cursor.fetchall()
    ]


async def get_due_tasks() -> list[ScheduledTask]:
    """Return active tasks whose next_run is in the past."""
    db = _get_db()
    cursor = await db.execute(
        """SELECT * FROM scheduled_tasks
           WHERE status = 'active' AND next_run IS NOT NULL
             AND next_run <= datetime('now')"""
    )
    return [
        ScheduledTask(
            id=row["id"],
            group_folder=row["group_folder"],
            chat_jid=row["chat_jid"],
            prompt=row["prompt"],
            script=row["script"],
            schedule_type=row["schedule_type"],
            schedule_value=row["schedule_value"],
            context_mode=row["context_mode"] or "isolated",
            next_run=row["next_run"],
            last_run=row["last_run"],
            last_result=row["last_result"],
            status=row["status"],
            created_at=row["created_at"],
        )
        for row in await cursor.fetchall()
    ]


async def get_task_by_id(task_id: str) -> ScheduledTask | None:
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        script=row["script"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def create_task(task: ScheduledTask) -> None:
    db = _get_db()
    await db.execute(
        """INSERT INTO scheduled_tasks
           (id, group_folder, chat_jid, prompt, script, schedule_type,
            schedule_value, context_mode, next_run, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.script,
            task.schedule_type,
            task.schedule_value,
            task.context_mode,
            task.next_run,
            task.status,
            task.created_at,
        ),
    )
    await db.commit()


async def update_task(task_id: str, updates: dict[str, Any]) -> None:
    db = _get_db()
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [task_id]
    await db.execute(
        f"UPDATE scheduled_tasks SET {sets} WHERE id = ?", vals
    )
    await db.commit()


async def update_task_after_run(
    task_id: str, next_run: str | None, result_summary: str
) -> None:
    db = _get_db()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    if next_run is None:
        # One-shot task — mark completed
        await db.execute(
            """UPDATE scheduled_tasks
               SET last_run = ?, last_result = ?, next_run = NULL, status = 'completed'
               WHERE id = ?""",
            (now, result_summary, task_id),
        )
    else:
        await db.execute(
            """UPDATE scheduled_tasks
               SET last_run = ?, last_result = ?, next_run = ?
               WHERE id = ?""",
            (now, result_summary, next_run, task_id),
        )
    await db.commit()


async def delete_task(task_id: str) -> None:
    db = _get_db()
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await db.commit()


async def log_task_run(log: TaskRunLog) -> None:
    db = _get_db()
    await db.execute(
        """INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (log.task_id, log.run_at, log.duration_ms, log.status, log.result, log.error),
    )
    await db.commit()
