"""Tests for cortexclaw.agent_runner — mock SDK, session capture, error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from cortexclaw.types import RegisteredGroup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_group(**overrides) -> RegisteredGroup:
    defaults = dict(
        name="Test Group",
        folder="test-group",
        trigger="@test",
        added_at="2024-01-01T00:00:00",
    )
    defaults.update(overrides)
    return RegisteredGroup(**defaults)


# Fake SDK message types


class _FakeTextBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, text: str):
        self.content = [_FakeTextBlock(text)]


class _FakeResultMessage:
    def __init__(self, session_id: str = "sess-123", subtype: str = "success"):
        self.session_id = session_id
        self.subtype = subtype


class _FakeSystemMessage:
    def __init__(self, message: str = ""):
        self.message = message


async def _fake_query_success(**kwargs):
    """Simulate a successful SDK query yielding assistant text + result."""
    yield _FakeAssistantMessage("Hello from agent")
    yield _FakeResultMessage(session_id="sess-abc")


async def _fake_query_error(**kwargs):
    """Simulate an SDK query that yields an error result."""
    yield _FakeAssistantMessage("partial output")
    yield _FakeResultMessage(session_id="sess-err", subtype="error")


async def _fake_query_exception(**kwargs):
    """Simulate an SDK query that raises an exception."""
    raise RuntimeError("SDK connection failed")
    yield  # Make it an async generator  # noqa: E501


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunAgent:
    async def _run_with_mock_query(self, fake_query, tmp_path, **run_kwargs):
        """Helper to run agent_runner.run_agent with mocked SDK and filesystem."""
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()

        with (
            patch("cortexclaw.agent_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.agent_runner.CORTEX_CONNECTION", "test-conn"),
            patch("cortexclaw.agent_runner.CORTEX_CLI_PATH", "cortex"),
            patch("cortexclaw.agent_runner.query", fake_query),
            patch("cortexclaw.agent_runner.AssistantMessage", _FakeAssistantMessage),
            patch("cortexclaw.agent_runner.ResultMessage", _FakeResultMessage),
            patch("cortexclaw.agent_runner.SystemMessage", _FakeSystemMessage),
        ):
            from cortexclaw.agent_runner import run_agent

            group = _make_group(**run_kwargs.pop("group_overrides", {}))
            return await run_agent(group=group, prompt="test prompt", chat_jid="jid1", **run_kwargs)

    async def test_successful_run(self, tmp_path):
        result = await self._run_with_mock_query(_fake_query_success, tmp_path)
        assert result.status == "success"
        assert result.result == "Hello from agent"
        assert result.session_id == "sess-abc"
        assert result.error is None

    async def test_error_result(self, tmp_path):
        result = await self._run_with_mock_query(_fake_query_error, tmp_path)
        assert result.status == "error"
        assert result.session_id == "sess-err"
        assert "partial output" in (result.result or "")

    async def test_exception_returns_error(self, tmp_path):
        result = await self._run_with_mock_query(_fake_query_exception, tmp_path)
        assert result.status == "error"
        assert "SDK connection failed" in (result.error or "")
        assert result.result is None

    async def test_on_output_callback_called(self, tmp_path):
        callback = AsyncMock()
        await self._run_with_mock_query(_fake_query_success, tmp_path, on_output=callback)
        callback.assert_awaited()

    async def test_resume_session_id_passed_to_options(self, tmp_path):
        """Verify that resume_session_id is forwarded to SDK options."""
        captured_options = {}

        async def capturing_query(prompt, options, **kwargs):
            captured_options["resume"] = options.resume
            async for msg in _fake_query_success():
                yield msg

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()

        with (
            patch("cortexclaw.agent_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.agent_runner.CORTEX_CONNECTION", "test-conn"),
            patch("cortexclaw.agent_runner.CORTEX_CLI_PATH", "cortex"),
            patch("cortexclaw.agent_runner.query", capturing_query),
            patch("cortexclaw.agent_runner.AssistantMessage", _FakeAssistantMessage),
            patch("cortexclaw.agent_runner.ResultMessage", _FakeResultMessage),
            patch("cortexclaw.agent_runner.SystemMessage", _FakeSystemMessage),
        ):
            from cortexclaw.agent_runner import run_agent

            group = _make_group()
            await run_agent(
                group=group,
                prompt="test",
                chat_jid="jid1",
                resume_session_id="sess-prior",
            )

        assert captured_options.get("resume") == "sess-prior"

    async def test_claude_md_prepended(self, tmp_path):
        """If CLAUDE.md exists in the group dir, its content should be prepended."""
        captured_prompts = []

        async def capturing_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            async for msg in _fake_query_success():
                yield msg

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        group_dir = groups_dir / "test-group"
        group_dir.mkdir()
        (group_dir / "CLAUDE.md").write_text("You are a helpful bot.")

        with (
            patch("cortexclaw.agent_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.agent_runner.CORTEX_CONNECTION", "test-conn"),
            patch("cortexclaw.agent_runner.CORTEX_CLI_PATH", "cortex"),
            patch("cortexclaw.agent_runner.query", capturing_query),
            patch("cortexclaw.agent_runner.AssistantMessage", _FakeAssistantMessage),
            patch("cortexclaw.agent_runner.ResultMessage", _FakeResultMessage),
            patch("cortexclaw.agent_runner.SystemMessage", _FakeSystemMessage),
        ):
            from cortexclaw.agent_runner import run_agent

            group = _make_group()
            await run_agent(group=group, prompt="hello", chat_jid="jid1")

        assert len(captured_prompts) == 1
        assert "You are a helpful bot." in captured_prompts[0]
        assert "hello" in captured_prompts[0]
