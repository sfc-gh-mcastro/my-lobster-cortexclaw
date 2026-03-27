"""Message router — XML formatting and outbound routing.

Ported from NanoClaw's src/router.ts.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .types import Channel, NewMessage


def escape_xml(s: str) -> str:
    """Escape XML special characters."""
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def format_messages(messages: list[NewMessage], timezone: str) -> str:
    """Format messages as XML for the agent prompt.

    Matches NanoClaw's ``<messages>`` XML format.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    lines: list[str] = []
    for m in messages:
        try:
            dt = datetime.fromisoformat(m.timestamp)
            display_time = dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            display_time = m.timestamp
        lines.append(
            f'<message sender="{escape_xml(m.sender_name)}" '
            f'time="{escape_xml(display_time)}">'
            f"{escape_xml(m.content)}</message>"
        )

    header = f'<context timezone="{escape_xml(timezone)}" />\n'
    return f"{header}<messages>\n" + "\n".join(lines) + "\n</messages>"


_INTERNAL_TAG_RE = re.compile(r"<internal>[\s\S]*?</internal>", re.DOTALL)


def strip_internal_tags(text: str) -> str:
    """Remove ``<internal>...</internal>`` blocks from agent output."""
    return _INTERNAL_TAG_RE.sub("", text).strip()


def format_outbound(raw_text: str) -> str:
    """Clean agent output for delivery to users."""
    return strip_internal_tags(raw_text)


def find_channel(channels: list[Channel], jid: str) -> Channel | None:
    """Find the channel that owns the given JID."""
    for ch in channels:
        if ch.owns_jid(jid) and ch.is_connected():
            return ch
    return None


async def route_outbound(channels: list[Channel], jid: str, text: str) -> None:
    """Send a message through the correct channel."""
    channel = find_channel(channels, jid)
    if not channel:
        raise RuntimeError(f"No channel for JID: {jid}")
    await channel.send_message(jid, text)
