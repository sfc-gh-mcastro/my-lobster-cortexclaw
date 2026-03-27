"""Task scheduler — cron / interval / one-shot task scheduling.

Ported from NanoClaw's src/task-scheduler.ts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Coroutine

from croniter import croniter

from . import db
from .agent_runner import run_agent
from .config import SCHEDULER_POLL_INTERVAL
from .group_queue import GroupQueue
from .router import format_outbound
from .types import RegisteredGroup, ScheduledTask, TaskRunLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Next-run computation
# ---------------------------------------------------------------------------


def compute_next_run(task: ScheduledTask) -> str | None:
    """Compute the next run time for a recurring task.

    Anchors to the task's scheduled time rather than now to prevent drift.
    """
    if task.schedule_type == "once":
        return None

    now = datetime.now(timezone.utc)

    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value, now)
        next_dt: datetime = cron.get_next(datetime)
        return next_dt.isoformat()

    if task.schedule_type == "interval":
        ms = int(task.schedule_value)
        if ms <= 0:
            logger.warning("Invalid interval for task %s: %s", task.id, task.schedule_value)
            return (now.timestamp() + 60).__str__()  # fallback 1 min

        # Anchor to the scheduled time to prevent drift
        if task.next_run:
            anchor = datetime.fromisoformat(task.next_run).timestamp()
            interval_s = ms / 1000.0
            next_ts = anchor + interval_s
            now_ts = now.timestamp()
            while next_ts <= now_ts:
                next_ts += interval_s
            return datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat()
        else:
            return datetime.fromtimestamp(
                now.timestamp() + ms / 1000.0, tz=timezone.utc
            ).isoformat()

    return None


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------


async def _run_task(
    task: ScheduledTask,
    registered_groups: dict[str, RegisteredGroup],
    sessions: dict[str, str],
    send_message: Callable[[str, str], Coroutine[None, None, None]],
    queue: GroupQueue,
    on_session_update: Callable[[str, str], Coroutine[None, None, None]] | None = None,
) -> None:
    """Execute a single scheduled task.

    *sessions* maps group_folder → session_id.  When ``task.context_mode``
    is ``"group"`` the agent continues the existing group session so it has
    access to prior conversation context.  When ``"isolated"`` (default)
    a fresh session is used.
    """
    start_ms = time.monotonic_ns() // 1_000_000

    group = next(
        (g for g in registered_groups.values() if g.folder == task.group_folder),
        None,
    )
    if not group:
        logger.error("Group not found for task %s: %s", task.id, task.group_folder)
        await db.log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=int((time.monotonic_ns() // 1_000_000) - start_ms),
                status="error",
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    # Determine session to resume for group context mode
    resume_session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    logger.info(
        "Running scheduled task %s for group %s (context_mode=%s, resume=%s)",
        task.id,
        task.group_folder,
        task.context_mode,
        resume_session_id or "new",
    )

    result_text: str | None = None
    error_text: str | None = None

    async def on_output(output):  # type: ignore[no-untyped-def]
        nonlocal result_text, error_text
        if output.result:
            result_text = output.result
            clean = format_outbound(output.result)
            if clean:
                await send_message(task.chat_jid, clean)
        if output.status == "error":
            error_text = output.error or "Unknown error"

    try:
        agent_result = await run_agent(
            group,
            task.prompt,
            task.chat_jid,
            on_output,
            resume_session_id=resume_session_id,
        )
        # Persist session if using group context mode
        if task.context_mode == "group" and agent_result.session_id and on_session_update:
            await on_session_update(task.group_folder, agent_result.session_id)
    except Exception as e:
        error_text = str(e)
        logger.error("Task %s failed: %s", task.id, e)

    duration_ms = int((time.monotonic_ns() // 1_000_000) - start_ms)

    await db.log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            status="error" if error_text else "success",
            result=result_text,
            error=error_text,
        )
    )

    next_run = compute_next_run(task)
    summary = (
        f"Error: {error_text}"
        if error_text
        else (result_text[:200] if result_text else "Completed")
    )
    await db.update_task_after_run(task.id, next_run, summary)
    logger.info("Task %s completed (duration=%dms)", task.id, duration_ms)


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

_scheduler_running = False


async def start_scheduler_loop(
    registered_groups: Callable[[], dict[str, RegisteredGroup]],
    sessions: Callable[[], dict[str, str]],
    send_message: Callable[[str, str], Coroutine[None, None, None]],
    queue: GroupQueue,
    on_session_update: Callable[[str, str], Coroutine[None, None, None]] | None = None,
) -> None:
    """Poll for due tasks and enqueue them via the GroupQueue."""
    global _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True
    logger.info("Scheduler loop started")

    while _scheduler_running:
        try:
            due_tasks = await db.get_due_tasks()
            if due_tasks:
                logger.info("Found %d due tasks", len(due_tasks))

            for task in due_tasks:
                # Re-check status
                current = await db.get_task_by_id(task.id)
                if not current or current.status != "active":
                    continue

                groups = registered_groups()
                sess = sessions()

                async def _task_fn(t=current, g=groups, s=sess):  # type: ignore[no-untyped-def]
                    await _run_task(t, g, s, send_message, queue, on_session_update)

                queue.enqueue_task(current.chat_jid, current.id, _task_fn)

        except Exception as e:
            logger.error("Error in scheduler loop: %s", e)

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


def stop_scheduler_loop() -> None:
    global _scheduler_running
    _scheduler_running = False
