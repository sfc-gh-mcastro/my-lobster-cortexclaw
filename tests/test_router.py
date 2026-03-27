"""Tests for cortexclaw.router — XML formatting and outbound routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

from cortexclaw.router import (
    escape_xml,
    find_channel,
    format_messages,
    format_outbound,
    route_outbound,
    strip_internal_tags,
)
from cortexclaw.types import Channel, NewMessage

# ---------------------------------------------------------------------------
# escape_xml
# ---------------------------------------------------------------------------


class TestEscapeXml:
    def test_empty_string(self):
        assert escape_xml("") == ""

    def test_no_special_chars(self):
        assert escape_xml("hello world") == "hello world"

    def test_ampersand(self):
        assert escape_xml("A & B") == "A &amp; B"

    def test_angle_brackets(self):
        assert escape_xml("<tag>") == "&lt;tag&gt;"

    def test_quotes(self):
        assert escape_xml('say "hi"') == "say &quot;hi&quot;"

    def test_all_special_chars(self):
        assert escape_xml('&<>"') == "&amp;&lt;&gt;&quot;"

    def test_preserves_non_special(self):
        assert escape_xml("abc 123 !@#$%") == "abc 123 !@#$%"


# ---------------------------------------------------------------------------
# format_messages
# ---------------------------------------------------------------------------


class TestFormatMessages:
    def test_empty_list(self):
        result = format_messages([], "UTC")
        assert "<messages>" in result
        assert "</messages>" in result
        assert '<context timezone="UTC" />' in result

    def test_single_message(self):
        msg = NewMessage(
            id="1",
            chat_jid="chat1",
            sender="u1",
            sender_name="Alice",
            content="Hello!",
            timestamp="2024-01-15T12:00:00+00:00",
        )
        result = format_messages([msg], "UTC")
        assert 'sender="Alice"' in result
        assert "Hello!" in result

    def test_escapes_sender_name(self):
        msg = NewMessage(
            id="1",
            chat_jid="chat1",
            sender="u1",
            sender_name='<Bob & "Friends">',
            content="hi",
            timestamp="2024-01-15T12:00:00+00:00",
        )
        result = format_messages([msg], "UTC")
        assert "&lt;Bob &amp; &quot;Friends&quot;&gt;" in result

    def test_escapes_content(self):
        msg = NewMessage(
            id="1",
            chat_jid="chat1",
            sender="u1",
            sender_name="Alice",
            content="x < y & z > w",
            timestamp="2024-01-15T12:00:00+00:00",
        )
        result = format_messages([msg], "UTC")
        assert "x &lt; y &amp; z &gt; w" in result

    def test_invalid_timezone_falls_back_to_utc(self):
        msg = NewMessage(
            id="1",
            chat_jid="chat1",
            sender="u1",
            sender_name="Alice",
            content="hi",
            timestamp="2024-01-15T12:00:00+00:00",
        )
        # Should not raise
        result = format_messages([msg], "Invalid/Timezone")
        assert "<messages>" in result

    def test_multiple_messages(self):
        msgs = [
            NewMessage(
                id=str(i),
                chat_jid="chat1",
                sender="u1",
                sender_name=f"User{i}",
                content=f"msg{i}",
                timestamp=f"2024-01-15T12:0{i}:00+00:00",
            )
            for i in range(3)
        ]
        result = format_messages(msgs, "UTC")
        assert result.count("<message ") == 3


# ---------------------------------------------------------------------------
# strip_internal_tags
# ---------------------------------------------------------------------------


class TestStripInternalTags:
    def test_no_internal_tags(self):
        assert strip_internal_tags("hello world") == "hello world"

    def test_single_internal_tag(self):
        assert strip_internal_tags("before<internal>secret</internal>after") == "beforeafter"

    def test_multiline_internal_tag(self):
        text = "before<internal>\nline1\nline2\n</internal>after"
        assert strip_internal_tags(text) == "beforeafter"

    def test_multiple_internal_tags(self):
        text = "a<internal>x</internal>b<internal>y</internal>c"
        assert strip_internal_tags(text) == "abc"

    def test_strips_surrounding_whitespace(self):
        text = "  <internal>x</internal>  hello  "
        assert strip_internal_tags(text) == "hello"

    def test_empty_internal_tag(self):
        assert strip_internal_tags("<internal></internal>") == ""


# ---------------------------------------------------------------------------
# format_outbound
# ---------------------------------------------------------------------------


class TestFormatOutbound:
    def test_plain_text(self):
        assert format_outbound("hello") == "hello"

    def test_strips_internal_tags(self):
        assert format_outbound("visible<internal>hidden</internal>rest") == "visiblerest"


# ---------------------------------------------------------------------------
# find_channel / route_outbound
# ---------------------------------------------------------------------------


class _FakeChannel(Channel):
    def __init__(self, name_: str, jids: set[str], connected: bool = True):
        self._name = name_
        self._jids = jids
        self._connected = connected
        self._send_mock = AsyncMock()

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        pass

    async def send_message(self, jid: str, text: str) -> None:
        await self._send_mock(jid, text)

    async def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid in self._jids


class TestFindChannel:
    def test_finds_matching_channel(self):
        ch = _FakeChannel("slack", {"jid1", "jid2"})
        assert find_channel([ch], "jid1") is ch

    def test_returns_none_if_no_match(self):
        ch = _FakeChannel("slack", {"jid1"})
        assert find_channel([ch], "jid999") is None

    def test_skips_disconnected_channel(self):
        ch = _FakeChannel("slack", {"jid1"}, connected=False)
        assert find_channel([ch], "jid1") is None


class TestRouteOutbound:
    async def test_sends_via_channel(self):
        ch = _FakeChannel("slack", {"jid1"})
        await route_outbound([ch], "jid1", "hello")
        ch._send_mock.assert_awaited_once_with("jid1", "hello")

    async def test_raises_if_no_channel(self):
        import pytest

        with pytest.raises(RuntimeError, match="No channel for JID"):
            await route_outbound([], "jid1", "hello")
