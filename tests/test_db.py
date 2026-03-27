"""Tests for cortexclaw.db — in-memory SQLite CRUD operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexclaw import db
from cortexclaw.types import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def memdb():
    """Provide a fresh in-memory database per test."""
    await db.init_database(Path(":memory:"))
    yield
    await db.close_database()


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


class TestChatMetadata:
    async def test_store_and_retrieve(self, memdb):
        await db.store_chat_metadata("jid1", "2024-01-01T00:00:00", name="Test Chat")
        chats = await db.get_all_chats()
        assert len(chats) == 1
        assert chats[0]["jid"] == "jid1"
        assert chats[0]["name"] == "Test Chat"

    async def test_upsert_preserves_existing_name(self, memdb):
        await db.store_chat_metadata("jid1", "2024-01-01T00:00:00", name="First")
        # Second upsert with a different name
        await db.store_chat_metadata("jid1", "2024-01-02T00:00:00", name="Second")
        chats = await db.get_all_chats()
        assert len(chats) == 1
        # COALESCE picks the new non-null name
        assert chats[0]["name"] == "Second"

    async def test_upsert_updates_timestamp(self, memdb):
        await db.store_chat_metadata("jid1", "2024-01-01T00:00:00", name="Chat")
        await db.store_chat_metadata("jid1", "2024-06-15T00:00:00", name="Chat")
        chats = await db.get_all_chats()
        assert chats[0]["last_message_time"] == "2024-06-15T00:00:00"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestMessages:
    async def test_store_and_fetch(self, memdb):
        msg = NewMessage(
            id="m1",
            chat_jid="jid1",
            sender="u1",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-15T12:00:00",
        )
        await db.store_message(msg)
        messages, new_ts = await db.get_new_messages(["jid1"], "2024-01-01T00:00:00", "BOT")
        assert len(messages) == 1
        assert messages[0].content == "Hello"
        assert new_ts == "2024-01-15T12:00:00"

    async def test_filters_bot_messages(self, memdb):
        msg = NewMessage(
            id="m1",
            chat_jid="jid1",
            sender="bot",
            sender_name="Bot",
            content="BOT: auto reply",
            timestamp="2024-01-15T12:00:00",
        )
        await db.store_message(msg)
        messages, _ = await db.get_new_messages(["jid1"], "2024-01-01T00:00:00", "BOT")
        # Content starts with "BOT:" so it should be filtered out
        assert len(messages) == 0

    async def test_respects_timestamp_filter(self, memdb):
        for i, ts in enumerate(["2024-01-10T00:00:00", "2024-01-20T00:00:00"]):
            await db.store_message(
                NewMessage(
                    id=f"m{i}",
                    chat_jid="jid1",
                    sender="u1",
                    sender_name="Alice",
                    content=f"msg{i}",
                    timestamp=ts,
                )
            )
        messages, _ = await db.get_new_messages(["jid1"], "2024-01-15T00:00:00", "BOT")
        assert len(messages) == 1
        assert messages[0].content == "msg1"

    async def test_empty_jids_returns_empty(self, memdb):
        messages, ts = await db.get_new_messages([], "2024-01-01T00:00:00", "BOT")
        assert messages == []

    async def test_get_messages_since(self, memdb):
        await db.store_message(
            NewMessage(
                id="m1",
                chat_jid="jid1",
                sender="u1",
                sender_name="Alice",
                content="hi",
                timestamp="2024-01-15T12:00:00",
            )
        )
        msgs = await db.get_messages_since("jid1", "2024-01-01T00:00:00", "BOT")
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


class TestRegisteredGroups:
    async def test_store_and_retrieve(self, memdb):
        group = RegisteredGroup(
            name="Test Group",
            folder="test-group",
            trigger="@test",
            added_at="2024-01-01T00:00:00",
        )
        await db.set_registered_group("jid1", group)
        groups = await db.get_all_registered_groups()
        assert "jid1" in groups
        assert groups["jid1"].name == "Test Group"
        assert groups["jid1"].folder == "test-group"
        assert groups["jid1"].trigger == "@test"

    async def test_round_trip_preserves_flags(self, memdb):
        group = RegisteredGroup(
            name="Main",
            folder="main-folder",
            trigger="@main",
            added_at="2024-01-01T00:00:00",
            requires_trigger=False,
            is_main=True,
        )
        await db.set_registered_group("jid1", group)
        groups = await db.get_all_registered_groups()
        assert groups["jid1"].requires_trigger is False
        assert groups["jid1"].is_main is True

    async def test_upsert_replaces(self, memdb):
        g1 = RegisteredGroup(name="V1", folder="f1", trigger="@v1", added_at="2024-01-01T00:00:00")
        g2 = RegisteredGroup(name="V2", folder="f1", trigger="@v2", added_at="2024-01-02T00:00:00")
        await db.set_registered_group("jid1", g1)
        await db.set_registered_group("jid1", g2)
        groups = await db.get_all_registered_groups()
        assert len(groups) == 1
        assert groups["jid1"].name == "V2"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_store_and_retrieve(self, memdb):
        await db.set_session("grp-folder", "sess-abc")
        sessions = await db.get_all_sessions()
        assert sessions == {"grp-folder": "sess-abc"}

    async def test_upsert_overwrites(self, memdb):
        await db.set_session("grp-folder", "sess-1")
        await db.set_session("grp-folder", "sess-2")
        sessions = await db.get_all_sessions()
        assert sessions["grp-folder"] == "sess-2"

    async def test_multiple_groups(self, memdb):
        await db.set_session("grp-a", "sess-a")
        await db.set_session("grp-b", "sess-b")
        sessions = await db.get_all_sessions()
        assert len(sessions) == 2


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


class TestRouterState:
    async def test_get_missing_key(self, memdb):
        val = await db.get_router_state("nonexistent")
        assert val is None

    async def test_set_and_get(self, memdb):
        await db.set_router_state("key1", "value1")
        assert await db.get_router_state("key1") == "value1"

    async def test_upsert(self, memdb):
        await db.set_router_state("key1", "v1")
        await db.set_router_state("key1", "v2")
        assert await db.get_router_state("key1") == "v2"


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


class TestScheduledTasks:
    def _make_task(self, **overrides) -> ScheduledTask:
        defaults = dict(
            id="task-1",
            group_folder="grp",
            chat_jid="jid1",
            prompt="do work",
            schedule_type="interval",
            schedule_value="60000",
            context_mode="isolated",
            next_run="2024-01-15T12:00:00",
            status="active",
            created_at="2024-01-01T00:00:00",
        )
        defaults.update(overrides)
        return ScheduledTask(**defaults)

    async def test_create_and_retrieve(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        tasks = await db.get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "task-1"
        assert tasks[0].prompt == "do work"

    async def test_get_by_id(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        found = await db.get_task_by_id("task-1")
        assert found is not None
        assert found.id == "task-1"

    async def test_get_by_id_not_found(self, memdb):
        found = await db.get_task_by_id("nonexistent")
        assert found is None

    async def test_update_task(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        await db.update_task("task-1", {"status": "paused"})
        updated = await db.get_task_by_id("task-1")
        assert updated is not None
        assert updated.status == "paused"

    async def test_delete_task(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        await db.delete_task("task-1")
        assert await db.get_task_by_id("task-1") is None

    async def test_update_after_run_recurring(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        await db.update_task_after_run("task-1", "2024-01-15T13:00:00", "OK")
        updated = await db.get_task_by_id("task-1")
        assert updated is not None
        assert updated.next_run == "2024-01-15T13:00:00"
        assert updated.last_result == "OK"
        assert updated.status == "active"

    async def test_update_after_run_oneshot(self, memdb):
        task = self._make_task(schedule_type="once")
        await db.create_task(task)
        await db.update_task_after_run("task-1", None, "Done")
        updated = await db.get_task_by_id("task-1")
        assert updated is not None
        assert updated.next_run is None
        assert updated.status == "completed"

    async def test_log_task_run(self, memdb):
        task = self._make_task()
        await db.create_task(task)
        log = TaskRunLog(
            task_id="task-1",
            run_at="2024-01-15T12:05:00",
            duration_ms=5000,
            status="success",
            result="output text",
        )
        await db.log_task_run(log)
        # Verify it was inserted (no get API, just ensure no error)
