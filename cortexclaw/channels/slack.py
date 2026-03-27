"""Slack channel implementation.

Uses slack_bolt async for real-time event handling.  Self-registers via the
channel registry; returns None from the factory if credentials are missing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from ..config import SLACK_APP_TOKEN, SLACK_BOT_TOKEN
from ..types import Channel, ChannelOpts, NewMessage
from .registry import register_channel

logger = logging.getLogger(__name__)


class SlackChannel(Channel):
    """Slack channel backed by slack_bolt async."""

    def __init__(self, opts: ChannelOpts) -> None:
        self._opts = opts
        self._connected = False
        self._bot_user_id: str | None = None
        self._app: object | None = None  # slack_bolt.async_app.AsyncApp
        self._handler_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "slack"

    async def connect(self) -> None:
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        self._app = AsyncApp(token=SLACK_BOT_TOKEN)
        app: AsyncApp = self._app  # type: ignore[assignment]

        # Resolve our own bot user ID
        auth = await app.client.auth_test()
        self._bot_user_id = auth.get("user_id")

        @app.event("message")
        async def handle_message(event: dict, say: object) -> None:
            # Ignore bot's own messages
            if event.get("user") == self._bot_user_id:
                return
            # Ignore message subtypes (edits, deletes, etc.)
            if event.get("subtype"):
                return

            channel_id = event.get("channel", "")
            jid = f"slack:{channel_id}"
            ts = event.get("ts", "")
            timestamp = datetime.fromtimestamp(
                float(ts), tz=timezone.utc
            ).isoformat() if ts else datetime.now(timezone.utc).isoformat()

            user_id = event.get("user", "unknown")
            text = event.get("text", "")

            msg = NewMessage(
                id=event.get("client_msg_id", str(uuid4())),
                chat_jid=jid,
                sender=user_id,
                sender_name=user_id,  # Could be enriched with users.info
                content=text,
                timestamp=timestamp,
            )
            self._opts.on_chat_metadata(
                jid, timestamp, None, "slack", True
            )
            self._opts.on_message(jid, msg)

        handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
        self._handler_task = asyncio.create_task(handler.start_async())
        self._connected = True
        logger.info("Slack channel connected")

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app or not self._connected:
            return
        # Extract channel ID from JID
        channel_id = jid.removeprefix("slack:")
        app = self._app  # type: ignore
        await app.client.chat_postMessage(channel=channel_id, text=text)

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("slack:")

    async def disconnect(self) -> None:
        if self._handler_task:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except asyncio.CancelledError:
                pass
        self._connected = False
        logger.info("Slack channel disconnected")

    async def sync_groups(self, force: bool = False) -> None:
        if not self._app or not self._connected:
            return
        app = self._app  # type: ignore
        try:
            result = await app.client.conversations_list(
                types="public_channel,private_channel"
            )
            for channel in result.get("channels", []):
                jid = f"slack:{channel['id']}"
                self._opts.on_chat_metadata(
                    jid,
                    datetime.now(timezone.utc).isoformat(),
                    channel.get("name"),
                    "slack",
                    True,
                )
        except Exception as e:
            logger.error("Failed to sync Slack groups: %s", e)


def _slack_factory(opts: ChannelOpts) -> SlackChannel | None:
    """Create a Slack channel if credentials are present."""
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        logger.debug("Slack credentials not set, skipping Slack channel")
        return None
    return SlackChannel(opts)


register_channel("slack", _slack_factory)
