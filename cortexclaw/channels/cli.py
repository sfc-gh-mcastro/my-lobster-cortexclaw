"""CLI channel — interactive terminal interface for development and testing.

Reads from stdin, writes to stdout.  Auto-registers a default group so the
orchestrator is usable with zero external services.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from uuid import uuid4

from ..config import ASSISTANT_NAME, ENABLE_CLI_CHANNEL
from ..types import Channel, ChannelOpts, NewMessage, RegisteredGroup
from .registry import register_channel

logger = logging.getLogger(__name__)

CLI_JID = "cli:default"
CLI_GROUP_FOLDER = "cli-default"


class CLIChannel(Channel):
    """Interactive stdin/stdout channel."""

    def __init__(self, opts: ChannelOpts) -> None:
        self._opts = opts
        self._connected = False
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "cli"

    async def connect(self) -> None:
        # Auto-register a default CLI group so messages are processed
        groups = self._opts.registered_groups()
        if CLI_JID not in groups:
            group = RegisteredGroup(
                name="CLI",
                folder=CLI_GROUP_FOLDER,
                trigger=f"@{ASSISTANT_NAME}",
                added_at=datetime.now(timezone.utc).isoformat(),
                requires_trigger=False,  # No trigger needed for CLI
            )
            # Register via the orchestrator callback — updates both DB and
            # in-memory state so messages are routed immediately.
            await self._opts.register_group(CLI_JID, group)

        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())
        print(f"\n  {ASSISTANT_NAME} CLI ready.  Type a message and press Enter.\n")
        logger.info("CLI channel connected")

    async def _read_loop(self) -> None:
        """Read lines from stdin in a non-blocking way."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self._connected:
            try:
                line_bytes = await reader.readline()
                if not line_bytes:
                    # EOF
                    break
                text = line_bytes.decode().strip()
                if not text:
                    continue

                now = datetime.now(timezone.utc).isoformat()
                msg = NewMessage(
                    id=str(uuid4()),
                    chat_jid=CLI_JID,
                    sender="user",
                    sender_name="User",
                    content=text,
                    timestamp=now,
                )
                self._opts.on_chat_metadata(CLI_JID, now, "CLI", "cli", False)
                self._opts.on_message(CLI_JID, msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("CLI read error: %s", e)

    async def send_message(self, jid: str, text: str) -> None:
        if jid != CLI_JID:
            return
        # Print with a visual prefix
        for line in text.splitlines():
            print(f"  {ASSISTANT_NAME}: {line}")
        print()  # blank line after response

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("cli:")

    async def disconnect(self) -> None:
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        logger.info("CLI channel disconnected")

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        if jid == CLI_JID and is_typing:
            print(f"  {ASSISTANT_NAME}: thinking...", end="\r")


def _cli_factory(opts: ChannelOpts) -> CLIChannel | None:
    """Create a CLI channel if enabled."""
    if not ENABLE_CLI_CHANNEL:
        return None
    return CLIChannel(opts)


register_channel("cli", _cli_factory)
