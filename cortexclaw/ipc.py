"""Filesystem-based IPC watcher.

Ported from NanoClaw's src/ipc.ts — agents write JSON files into per-group
IPC directories; the orchestrator polls and processes them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Coroutine

from croniter import croniter

from . import db
from .config import DATA_DIR, IPC_POLL_INTERVAL, TIMEZONE
from .types import RegisteredGroup, ScheduledTask

logger = logging.getLogger(__name__)

_ipc_running = False


# ---------------------------------------------------------------------------
# Dependency injection interface
# ---------------------------------------------------------------------------


class IpcDeps:
    """Callbacks the IPC watcher needs from the orchestrator."""

    def __init__(
        self,
        send_message: Callable[[str, str], Coroutine[None, None, None]],
        registered_groups: Callable[[], dict[str, RegisteredGroup]],
        register_group: Callable[[str, RegisteredGroup], Coroutine[None, None, None]],
        on_tasks_changed: Callable[[], None],
    ) -> None:
        self.send_message = send_message
        self.registered_groups = registered_groups
        self.register_group = register_group
        self.on_tasks_changed = on_tasks_changed


# ---------------------------------------------------------------------------
# IPC processing
# ---------------------------------------------------------------------------


async def _process_message_ipc(
    data: dict,
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    """Process a message IPC file."""
    if data.get("type") != "message":
        return
    chat_jid = data.get("chatJid", "")
    text = data.get("text", "")
    if not chat_jid or not text:
        return

    groups = deps.registered_groups()
    target_group = groups.get(chat_jid)

    # Authorization: non-main groups can only message their own chat
    if is_main or (target_group and target_group.folder == source_group):
        await deps.send_message(chat_jid, text)
        logger.info("IPC message sent to %s from %s", chat_jid, source_group)
    else:
        logger.warning(
            "Unauthorized IPC message from %s to %s blocked",
            source_group, chat_jid,
        )


async def _process_task_ipc(
    data: dict,
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    """Process a task IPC file."""
    action = data.get("type", "")
    groups = deps.registered_groups()

    if action == "schedule_task":
        prompt = data.get("prompt")
        schedule_type = data.get("schedule_type")
        schedule_value = data.get("schedule_value")
        target_jid = data.get("targetJid")

        if not all([prompt, schedule_type, schedule_value, target_jid]):
            return

        target_group = groups.get(target_jid)
        if not target_group:
            logger.warning("Cannot schedule task: target %s not registered", target_jid)
            return

        # Authorization
        if not is_main and target_group.folder != source_group:
            logger.warning("Unauthorized schedule_task from %s", source_group)
            return

        # Compute next_run
        next_run: str | None = None
        if schedule_type == "cron":
            try:
                cron = croniter(schedule_value)
                next_run = cron.get_next(datetime).isoformat()
            except Exception:
                logger.warning("Invalid cron expression: %s", schedule_value)
                return
        elif schedule_type == "interval":
            ms = int(schedule_value)
            if ms <= 0:
                return
            next_run = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + ms / 1000.0,
                tz=timezone.utc,
            ).isoformat()
        elif schedule_type == "once":
            try:
                next_run = datetime.fromisoformat(schedule_value).isoformat()
            except Exception:
                return

        task_id = data.get("taskId") or f"task-{int(datetime.now(timezone.utc).timestamp()*1000)}"
        context_mode = data.get("context_mode", "isolated")
        if context_mode not in ("group", "isolated"):
            context_mode = "isolated"

        task = ScheduledTask(
            id=task_id,
            group_folder=target_group.folder,
            chat_jid=target_jid,
            prompt=prompt,
            script=data.get("script"),
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            context_mode=context_mode,
            next_run=next_run,
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await db.create_task(task)
        logger.info("Task %s created via IPC from %s", task_id, source_group)
        deps.on_tasks_changed()

    elif action == "pause_task":
        task_id = data.get("taskId")
        if not task_id:
            return
        task = await db.get_task_by_id(task_id)
        if task and (is_main or task.group_folder == source_group):
            await db.update_task(task_id, {"status": "paused"})
            logger.info("Task %s paused via IPC", task_id)
            deps.on_tasks_changed()

    elif action == "resume_task":
        task_id = data.get("taskId")
        if not task_id:
            return
        task = await db.get_task_by_id(task_id)
        if task and (is_main or task.group_folder == source_group):
            await db.update_task(task_id, {"status": "active"})
            logger.info("Task %s resumed via IPC", task_id)
            deps.on_tasks_changed()

    elif action == "delete_task":
        task_id = data.get("taskId")
        if not task_id:
            return
        task = await db.get_task_by_id(task_id)
        if task and (is_main or task.group_folder == source_group):
            await db.delete_task(task_id)
            logger.info("Task %s deleted via IPC", task_id)
            deps.on_tasks_changed()

    elif action == "register_group":
        if not is_main:
            logger.warning("Non-main group %s cannot register groups", source_group)
            return
        jid = data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")
        if not all([jid, name, folder, trigger]):
            return
        group = RegisteredGroup(
            name=name,
            folder=folder,
            trigger=trigger,
            added_at=datetime.now(timezone.utc).isoformat(),
            requires_trigger=data.get("requiresTrigger", True),
        )
        await deps.register_group(jid, group)
        logger.info("Group %s registered via IPC from %s", jid, source_group)


# ---------------------------------------------------------------------------
# Main watcher loop
# ---------------------------------------------------------------------------


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Poll per-group IPC directories for JSON files."""
    global _ipc_running
    if _ipc_running:
        return
    _ipc_running = True

    ipc_base = DATA_DIR / "ipc"
    ipc_base.mkdir(parents=True, exist_ok=True)
    errors_dir = ipc_base / "errors"
    errors_dir.mkdir(exist_ok=True)

    logger.info("IPC watcher started")

    while _ipc_running:
        try:
            groups = deps.registered_groups()
            # Build folder → is_main lookup
            folder_is_main: dict[str, bool] = {}
            for g in groups.values():
                if g.is_main:
                    folder_is_main[g.folder] = True

            # Scan all group IPC directories
            if ipc_base.exists():
                for group_dir in ipc_base.iterdir():
                    if not group_dir.is_dir() or group_dir.name == "errors":
                        continue

                    source_group = group_dir.name
                    is_main = folder_is_main.get(source_group, False)

                    # Process messages
                    messages_dir = group_dir / "messages"
                    if messages_dir.exists():
                        for f in sorted(messages_dir.glob("*.json")):
                            try:
                                data = json.loads(f.read_text())
                                await _process_message_ipc(data, source_group, is_main, deps)
                                f.unlink()
                            except Exception as e:
                                logger.error("IPC message error %s: %s", f.name, e)
                                shutil.move(str(f), str(errors_dir / f"{source_group}-{f.name}"))

                    # Process tasks
                    tasks_dir = group_dir / "tasks"
                    if tasks_dir.exists():
                        for f in sorted(tasks_dir.glob("*.json")):
                            try:
                                data = json.loads(f.read_text())
                                await _process_task_ipc(data, source_group, is_main, deps)
                                f.unlink()
                            except Exception as e:
                                logger.error("IPC task error %s: %s", f.name, e)
                                shutil.move(str(f), str(errors_dir / f"{source_group}-{f.name}"))

        except Exception as e:
            logger.error("Error in IPC watcher: %s", e)

        await asyncio.sleep(IPC_POLL_INTERVAL)


def stop_ipc_watcher() -> None:
    global _ipc_running
    _ipc_running = False
