"""CortexClaw orchestrator — main event loop.

Ported from NanoClaw's src/index.ts: loads state, connects channels, runs the
message polling loop, dispatches to agents via the GroupQueue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone

from . import db
from .agent_runner import run_agent
from .channels.registry import get_channel_factory, get_registered_channel_names
from .config import (
    ASSISTANT_NAME,
    DATA_DIR,
    GROUPS_DIR,
    POLL_INTERVAL,
    STORE_DIR,
    TIMEZONE,
    build_trigger_pattern,
)
from .group_queue import GroupQueue
from .ipc import IpcDeps, start_ipc_watcher, stop_ipc_watcher
from .router import find_channel, format_messages, format_outbound
from .task_scheduler import start_scheduler_loop, stop_scheduler_loop
from .types import AgentOutput, Channel, ChannelOpts, NewMessage, RegisteredGroup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (mirrors NanoClaw's top-level variables)
# ---------------------------------------------------------------------------

_last_timestamp: str = ""
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_channels: list[Channel] = []
_queue: GroupQueue = GroupQueue()
_running: bool = False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


async def _load_state() -> None:
    global _last_timestamp, _sessions, _registered_groups, _last_agent_timestamp

    _last_timestamp = (await db.get_router_state("last_timestamp")) or ""

    agent_ts_raw = await db.get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts_raw) if agent_ts_raw else {}
    except Exception:
        logger.warning("Corrupted last_agent_timestamp, resetting")
        _last_agent_timestamp = {}

    _sessions = await db.get_all_sessions()
    _registered_groups = await db.get_all_registered_groups()
    logger.info("State loaded: %d groups", len(_registered_groups))


async def _save_state() -> None:
    await db.set_router_state("last_timestamp", _last_timestamp)
    await db.set_router_state("last_agent_timestamp", json.dumps(_last_agent_timestamp))


# ---------------------------------------------------------------------------
# Group registration
# ---------------------------------------------------------------------------


async def _register_group(jid: str, group: RegisteredGroup) -> None:
    global _registered_groups
    _registered_groups[jid] = group
    await db.set_registered_group(jid, group)

    # Create group directory
    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "logs").mkdir(exist_ok=True)

    logger.info("Group registered: %s → %s", jid, group.name)


# ---------------------------------------------------------------------------
# Inbound message handling (called by channels)
# ---------------------------------------------------------------------------


def _on_inbound_message(chat_jid: str, msg: NewMessage) -> None:
    """Called by channels when a message arrives."""
    # Store message in DB
    asyncio.create_task(_store_and_enqueue(chat_jid, msg))


async def _store_and_enqueue(chat_jid: str, msg: NewMessage) -> None:
    # Only store messages for registered groups
    if chat_jid in _registered_groups:
        await db.store_message(msg)
        _queue.enqueue_message_check(chat_jid)
    else:
        logger.debug("Message for unregistered JID %s, ignoring", chat_jid)


def _on_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None,
    channel: str | None,
    is_group: bool | None,
) -> None:
    """Called by channels for chat metadata discovery."""
    asyncio.create_task(db.store_chat_metadata(chat_jid, timestamp, name, channel, is_group))


# ---------------------------------------------------------------------------
# Process messages for a group (called by GroupQueue)
# ---------------------------------------------------------------------------


async def _process_group_messages(chat_jid: str) -> bool:
    """Process all pending messages for a group. Returns True on success."""
    global _last_agent_timestamp, _sessions

    group = _registered_groups.get(chat_jid)
    if not group:
        return True

    channel = find_channel(_channels, chat_jid)
    if not channel:
        logger.warning("No channel for JID %s, skipping", chat_jid)
        return True

    since_timestamp = _last_agent_timestamp.get(chat_jid, "")
    missed_messages = await db.get_messages_since(chat_jid, since_timestamp, ASSISTANT_NAME)

    if not missed_messages:
        return True

    # Check trigger requirement for non-main groups
    if not group.is_main and group.requires_trigger:
        trigger_re = build_trigger_pattern(group.trigger)
        has_trigger = any(trigger_re.search(m.content.strip()) for m in missed_messages)
        if not has_trigger:
            return True

    prompt = format_messages(missed_messages, TIMEZONE)

    # Session continuity: explicitly resume the prior session by ID.
    # Using --resume <id> is more reliable than --continue (which resumes
    # the *last* session in the cwd and can pick up the wrong one).
    prior_session_id = _sessions.get(group.folder)
    if prior_session_id:
        logger.info(
            "Resuming session %s for group %s",
            prior_session_id,
            group.name,
        )

    # Advance cursor (save old for rollback on error)
    previous_cursor = _last_agent_timestamp.get(chat_jid, "")
    _last_agent_timestamp[chat_jid] = missed_messages[-1].timestamp
    await _save_state()

    logger.info(
        "Processing %d messages for group %s (resume=%s)",
        len(missed_messages),
        group.name,
        prior_session_id or "new",
    )

    await channel.set_typing(chat_jid, True)
    had_error = False
    output_sent = False

    async def _on_output(output: AgentOutput) -> None:
        nonlocal had_error, output_sent
        if output.result:
            clean = format_outbound(output.result)
            if clean:
                await channel.send_message(chat_jid, clean)
                output_sent = True

                # Store bot response in DB
                bot_msg = NewMessage(
                    id=f"bot-{datetime.now(timezone.utc).timestamp()}",
                    chat_jid=chat_jid,
                    sender=ASSISTANT_NAME,
                    sender_name=ASSISTANT_NAME,
                    content=clean,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    is_from_me=True,
                    is_bot_message=True,
                )
                await db.store_message(bot_msg)

        if output.status == "error":
            had_error = True

    agent_result = await run_agent(
        group,
        prompt,
        chat_jid,
        _on_output,
        resume_session_id=prior_session_id,
    )

    await channel.set_typing(chat_jid, False)

    # Persist session_id returned by the agent so the next invocation can
    # resume it via ``--resume <session_id>``.
    if agent_result.session_id:
        _sessions[group.folder] = agent_result.session_id
        await db.set_session(group.folder, agent_result.session_id)
        logger.info(
            "Saved session %s for group %s",
            agent_result.session_id,
            group.name,
        )

    if agent_result.status == "error" or had_error:
        if output_sent:
            logger.warning(
                "Agent error for %s after output sent, skipping rollback",
                group.name,
            )
            return True
        # Roll back cursor for retry
        _last_agent_timestamp[chat_jid] = previous_cursor
        await _save_state()
        logger.warning("Agent error for %s, rolled back cursor for retry", group.name)
        return False

    return True


# ---------------------------------------------------------------------------
# Send message helper (for IPC / scheduler)
# ---------------------------------------------------------------------------


async def _send_message(jid: str, text: str) -> None:
    """Send a message through the appropriate channel."""
    channel = find_channel(_channels, jid)
    if channel:
        await channel.send_message(jid, text)
    else:
        logger.warning("Cannot send message: no channel for %s", jid)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the CortexClaw orchestrator."""
    global _running

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    logger.info("Starting CortexClaw (assistant=%s)", ASSISTANT_NAME)

    # Ensure directories exist
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize database
    await db.init_database()
    await _load_state()

    # Set up GroupQueue
    _queue.set_process_messages_fn(_process_group_messages)

    # Import channels (triggers self-registration)
    import cortexclaw.channels  # noqa: F401

    # Connect channels
    channel_opts = ChannelOpts(
        on_message=_on_inbound_message,
        on_chat_metadata=_on_chat_metadata,
        registered_groups=lambda: _registered_groups,
        register_group=_register_group,
    )
    for ch_name in get_registered_channel_names():
        factory = get_channel_factory(ch_name)
        if not factory:
            continue
        channel = factory(channel_opts)
        if channel is None:
            logger.info("Channel %s skipped (credentials not set)", ch_name)
            continue
        try:
            await channel.connect()
            _channels.append(channel)
            logger.info("Channel %s connected", ch_name)
        except Exception as e:
            logger.error("Failed to connect channel %s: %s", ch_name, e)

    if not _channels:
        logger.error("No channels connected — nothing to do")
        await db.close_database()
        return

    # Start IPC watcher
    ipc_deps = IpcDeps(
        send_message=_send_message,
        registered_groups=lambda: _registered_groups,
        register_group=_register_group,
        on_tasks_changed=lambda: None,
    )
    _ipc_task = asyncio.create_task(start_ipc_watcher(ipc_deps))  # noqa: F841

    # Helper for task scheduler to update sessions
    async def _update_session(group_folder: str, session_id: str) -> None:
        global _sessions
        _sessions[group_folder] = session_id
        await db.set_session(group_folder, session_id)

    # Start task scheduler
    _scheduler_task = asyncio.create_task(  # noqa: F841
        start_scheduler_loop(
            registered_groups=lambda: _registered_groups,
            sessions=lambda: _sessions,
            send_message=_send_message,
            queue=_queue,
            on_session_update=_update_session,
        )
    )

    # Set up signal handlers for graceful shutdown
    _running = True
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        global _running
        if _running:
            logger.info("Received shutdown signal")
            _running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    logger.info(
        "CortexClaw running — %d channels, %d groups", len(_channels), len(_registered_groups)
    )

    # Main poll loop
    try:
        while _running:
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        pass

    # Shutdown
    logger.info("Shutting down...")
    stop_scheduler_loop()
    stop_ipc_watcher()

    for ch in _channels:
        try:
            await ch.disconnect()
        except Exception as e:
            logger.error("Error disconnecting %s: %s", ch.name, e)

    await _queue.shutdown(grace_period_seconds=30.0)
    await db.close_database()
    logger.info("CortexClaw stopped")
