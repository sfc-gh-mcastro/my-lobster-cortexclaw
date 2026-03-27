"""Tests for cortexclaw.types — dataclass construction and trigger pattern building."""

from __future__ import annotations

from cortexclaw.types import (
    AgentOutput,
    ContainerConfig,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
    build_trigger_pattern,
)

# ---------------------------------------------------------------------------
# build_trigger_pattern
# ---------------------------------------------------------------------------


class TestBuildTriggerPattern:
    def test_matches_at_start(self):
        pat = build_trigger_pattern("@bot")
        assert pat.match("@bot hello")

    def test_case_insensitive(self):
        pat = build_trigger_pattern("@Bot")
        assert pat.match("@bot hello")
        assert pat.match("@BOT hello")

    def test_requires_word_boundary(self):
        pat = build_trigger_pattern("@bot")
        assert pat.match("@bot hello")
        assert not pat.match("@bother me")

    def test_does_not_match_mid_string(self):
        pat = build_trigger_pattern("@bot")
        assert not pat.match("say @bot hello")

    def test_strips_whitespace(self):
        pat = build_trigger_pattern("  @bot  ")
        assert pat.match("@bot hi")

    def test_escapes_regex_chars(self):
        pat = build_trigger_pattern("@bot.v2")
        assert pat.match("@bot.v2 hello")
        # dot should be literal, not wildcard
        assert not pat.match("@botXv2 hello")


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestContainerConfig:
    def test_defaults(self):
        cfg = ContainerConfig()
        assert cfg.timeout == 300_000
        assert cfg.extra_env == {}

    def test_custom_values(self):
        cfg = ContainerConfig(timeout=60_000, extra_env={"KEY": "VAL"})
        assert cfg.timeout == 60_000
        assert cfg.extra_env == {"KEY": "VAL"}


class TestRegisteredGroup:
    def test_defaults(self):
        g = RegisteredGroup(name="test", folder="test-folder", trigger="@test")
        assert g.requires_trigger is True
        assert g.is_main is False
        assert g.container_config is None
        assert g.added_at == ""

    def test_custom(self):
        g = RegisteredGroup(
            name="main",
            folder="main-folder",
            trigger="@main",
            requires_trigger=False,
            is_main=True,
        )
        assert g.requires_trigger is False
        assert g.is_main is True


class TestNewMessage:
    def test_defaults(self):
        m = NewMessage(
            id="1",
            chat_jid="jid",
            sender="u1",
            sender_name="User",
            content="hi",
            timestamp="2024-01-01T00:00:00",
        )
        assert m.is_from_me is False
        assert m.is_bot_message is False


class TestAgentOutput:
    def test_defaults(self):
        out = AgentOutput()
        assert out.result is None
        assert out.status == "success"
        assert out.error is None
        assert out.session_id is None

    def test_error_output(self):
        out = AgentOutput(status="error", error="boom")
        assert out.status == "error"
        assert out.error == "boom"

    def test_with_session_id(self):
        out = AgentOutput(session_id="sess-123")
        assert out.session_id == "sess-123"


class TestScheduledTask:
    def test_defaults(self):
        t = ScheduledTask(
            id="t1",
            group_folder="grp",
            chat_jid="jid",
            prompt="do stuff",
            schedule_type="once",
            schedule_value="2024-01-01T00:00:00",
        )
        assert t.context_mode == "isolated"
        assert t.status == "active"
        assert t.script is None


class TestTaskRunLog:
    def test_construction(self):
        log = TaskRunLog(
            task_id="t1",
            run_at="2024-01-01T00:00:00",
            duration_ms=1234,
            status="success",
        )
        assert log.result is None
        assert log.error is None
