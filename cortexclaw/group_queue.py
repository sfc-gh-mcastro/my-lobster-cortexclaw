"""Group queue — per-group concurrency control with global limit and retry.

Ported from NanoClaw's src/group-queue.ts to async Python.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Coroutine

from .config import MAX_CONCURRENT_AGENTS

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_RETRY_SECONDS = 5.0


@dataclass
class _QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Coroutine[None, None, None]]


@dataclass
class _GroupState:
    active: bool = False
    idle_waiting: bool = False
    is_task_container: bool = False
    running_task_id: str | None = None
    pending_messages: bool = False
    pending_tasks: list[_QueuedTask] = field(default_factory=list)
    group_folder: str | None = None
    retry_count: int = 0


ProcessMessagesFn = Callable[[str], Coroutine[None, None, bool]]


class GroupQueue:
    """Manages per-group agent runs with a global concurrency limit."""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: ProcessMessagesFn | None = None
        self._shutting_down = False
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    def _get_group(self, group_jid: str) -> _GroupState:
        if group_jid not in self._groups:
            self._groups[group_jid] = _GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(self, fn: ProcessMessagesFn) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        """Enqueue a message processing run for the given group."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active for %s, message queued", group_jid)
            return

        if self._active_count >= MAX_CONCURRENT_AGENTS:
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit (%d), message queued for %s",
                self._active_count,
                group_jid,
            )
            return

        asyncio.create_task(self._run_for_group(group_jid, "messages"))

    def enqueue_task(
        self,
        group_jid: str,
        task_id: str,
        fn: Callable[[], Coroutine[None, None, None]],
    ) -> None:
        """Enqueue a scheduled task for the given group."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Prevent double-queuing
        if state.running_task_id == task_id:
            return
        if any(t.id == task_id for t in state.pending_tasks):
            return

        queued = _QueuedTask(id=task_id, group_jid=group_jid, fn=fn)

        if state.active:
            state.pending_tasks.append(queued)
            logger.debug("Container active for %s, task %s queued", group_jid, task_id)
            return

        if self._active_count >= MAX_CONCURRENT_AGENTS:
            state.pending_tasks.append(queued)
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            return

        asyncio.create_task(self._run_task(group_jid, queued))

    # ------------------------------------------------------------------
    # Internal runners
    # ------------------------------------------------------------------

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False
        self._active_count += 1

        logger.debug(
            "Starting agent for %s (%s), active=%d",
            group_jid,
            reason,
            self._active_count,
        )

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception as e:
            logger.error("Error processing messages for %s: %s", group_jid, e)
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = True
        state.running_task_id = task.id
        self._active_count += 1

        logger.debug(
            "Running task %s for %s, active=%d",
            task.id,
            group_jid,
            self._active_count,
        )

        try:
            await task.fn()
        except Exception as e:
            logger.error("Error running task %s for %s: %s", task.id, group_jid, e)
        finally:
            state.active = False
            state.is_task_container = False
            state.running_task_id = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error(
                "Max retries exceeded for %s, dropping (will retry on next message)",
                group_jid,
            )
            state.retry_count = 0
            return

        delay = BASE_RETRY_SECONDS * (2 ** (state.retry_count - 1))
        logger.info(
            "Scheduling retry %d for %s in %.1fs",
            state.retry_count,
            group_jid,
            delay,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        asyncio.create_task(_retry())

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Tasks first
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            asyncio.create_task(self._run_task(group_jid, task))
            return

        # Then pending messages
        if state.pending_messages:
            asyncio.create_task(self._run_for_group(group_jid, "drain"))
            return

        # Nothing pending — drain waiting groups
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting_groups and self._active_count < MAX_CONCURRENT_AGENTS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                asyncio.create_task(self._run_task(next_jid, task))
            elif state.pending_messages:
                asyncio.create_task(self._run_for_group(next_jid, "drain"))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        self._shutting_down = True
        logger.info("GroupQueue shutting down (grace=%.1fs)", grace_period_seconds)
        # Wait for active tasks to finish
        deadline = asyncio.get_event_loop().time() + grace_period_seconds
        while self._active_count > 0:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "Grace period expired, %d agents still active",
                    self._active_count,
                )
                break
            await asyncio.sleep(min(1.0, remaining))
        logger.info("GroupQueue shutdown complete")
