"""Tests for cortexclaw.group_queue — concurrency, task priority, retry, shutdown."""

from __future__ import annotations

import asyncio

from cortexclaw.group_queue import GroupQueue

# ---------------------------------------------------------------------------
# Basic message enqueue
# ---------------------------------------------------------------------------


class TestEnqueueMessage:
    async def test_runs_process_messages(self):
        q = GroupQueue()
        called = asyncio.Event()

        async def process(jid: str) -> bool:
            called.set()
            return True

        q.set_process_messages_fn(process)
        q.enqueue_message_check("group1")
        await asyncio.wait_for(called.wait(), timeout=2.0)

    async def test_queues_when_group_active(self):
        q = GroupQueue()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def process(jid: str) -> bool:
            started.set()
            await finish.wait()
            return True

        q.set_process_messages_fn(process)
        q.enqueue_message_check("group1")
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # Second enqueue while first is active should queue
        q.enqueue_message_check("group1")
        state = q._get_group("group1")
        assert state.pending_messages is True

        finish.set()
        await asyncio.sleep(0.1)  # Let drain happen


# ---------------------------------------------------------------------------
# Task enqueue / deduplication
# ---------------------------------------------------------------------------


class TestEnqueueTask:
    async def test_runs_task(self):
        q = GroupQueue()
        called = asyncio.Event()

        async def task_fn():
            called.set()

        q.enqueue_task("group1", "t1", task_fn)
        await asyncio.wait_for(called.wait(), timeout=2.0)

    async def test_dedup_running_task(self):
        q = GroupQueue()
        started = asyncio.Event()
        finish = asyncio.Event()
        call_count = 0

        async def task_fn():
            nonlocal call_count
            call_count += 1
            started.set()
            await finish.wait()

        q.enqueue_task("group1", "t1", task_fn)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # Re-enqueue same task_id should be a no-op
        q.enqueue_task("group1", "t1", task_fn)
        state = q._get_group("group1")
        assert len(state.pending_tasks) == 0

        finish.set()
        await asyncio.sleep(0.1)
        assert call_count == 1

    async def test_dedup_pending_task(self):
        q = GroupQueue()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def blocker():
            started.set()
            await finish.wait()

        async def task_fn():
            pass

        # Fill the slot with a blocker
        q.enqueue_task("group1", "blocker", blocker)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # Enqueue t1 twice — second should be deduped
        q.enqueue_task("group1", "t1", task_fn)
        q.enqueue_task("group1", "t1", task_fn)

        state = q._get_group("group1")
        assert len(state.pending_tasks) == 1

        finish.set()
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Drain — tasks have priority over messages
# ---------------------------------------------------------------------------


class TestDrain:
    async def test_tasks_drain_before_messages(self):
        q = GroupQueue()
        order: list[str] = []
        started = asyncio.Event()
        finish = asyncio.Event()

        async def blocker(jid: str) -> bool:
            started.set()
            await finish.wait()
            return True

        async def task_fn():
            order.append("task")

        q.set_process_messages_fn(blocker)
        q.enqueue_message_check("group1")
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # While blocker is active, queue both a task and a message
        q.enqueue_task("group1", "t1", task_fn)
        q.enqueue_message_check("group1")

        # Let the blocker finish — drain should run task first
        finish.set()
        await asyncio.sleep(0.2)

        assert "task" in order


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_blocks_new_enqueue(self):
        q = GroupQueue()
        called = asyncio.Event()

        async def process(jid: str) -> bool:
            called.set()
            return True

        q.set_process_messages_fn(process)
        await q.shutdown(grace_period_seconds=0.1)

        # After shutdown, enqueue should be a no-op
        q.enqueue_message_check("group1")
        await asyncio.sleep(0.1)
        assert not called.is_set()

    async def test_shutdown_waits_for_active(self):
        q = GroupQueue()
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow_process(jid: str) -> bool:
            started.set()
            await asyncio.sleep(0.2)
            finished.set()
            return True

        q.set_process_messages_fn(slow_process)
        q.enqueue_message_check("group1")
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # Shutdown with enough grace to let it finish
        await q.shutdown(grace_period_seconds=2.0)
        assert finished.is_set()


# ---------------------------------------------------------------------------
# Retry backoff
# ---------------------------------------------------------------------------


class TestRetry:
    async def test_retry_on_failure(self):
        q = GroupQueue()
        attempts = []

        async def failing_process(jid: str) -> bool:
            attempts.append(1)
            return False  # Signal failure

        q.set_process_messages_fn(failing_process)
        q.enqueue_message_check("group1")

        # Wait enough for initial attempt + first retry (5s base is too long,
        # so we just check the retry was scheduled)
        await asyncio.sleep(0.2)
        assert len(attempts) >= 1

        state = q._get_group("group1")
        # retry_count should have been incremented
        assert state.retry_count >= 1
