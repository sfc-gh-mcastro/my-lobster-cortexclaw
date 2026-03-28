"""Core type definitions for CortexClaw.

Ported from NanoClaw's src/types.ts — all messaging, group, task, and channel
abstractions live here.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional

# ---------------------------------------------------------------------------
# Container / mount configuration
# ---------------------------------------------------------------------------


@dataclass
class ContainerConfig:
    """Per-group agent configuration (replaces NanoClaw's container config)."""

    timeout: int = 300_000  # ms, default 5 min
    extra_env: dict[str, str] = field(default_factory=dict)
    docker_enabled: Optional[bool] = None  # None = use global DOCKER_ENABLED default
    image: Optional[str] = None  # None = use global DOCKER_IMAGE default
    additional_mounts: list[str] = field(default_factory=list)  # "host:container:mode"


# ---------------------------------------------------------------------------
# Registered group
# ---------------------------------------------------------------------------


@dataclass
class RegisteredGroup:
    """A chat group registered with the orchestrator."""

    name: str
    folder: str
    trigger: str
    added_at: str = ""
    container_config: Optional[ContainerConfig] = None
    requires_trigger: bool = True  # True for groups, False for solo chats
    is_main: bool = False  # Elevated privileges: cross-group messaging, etc.


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class NewMessage:
    """An inbound message from any channel."""

    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False
    is_bot_message: bool = False


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


@dataclass
class ScheduledTask:
    """A cron / interval / one-shot scheduled task."""

    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: str  # "cron" | "interval" | "once"
    schedule_value: str
    context_mode: str = "isolated"  # "group" | "isolated"
    script: Optional[str] = None
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    last_result: Optional[str] = None
    status: str = "active"  # "active" | "paused" | "completed"
    created_at: str = ""


@dataclass
class TaskRunLog:
    """Log entry for a single task execution."""

    task_id: str
    run_at: str
    duration_ms: int
    status: str  # "success" | "error"
    result: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent output (replaces NanoClaw's ContainerOutput)
# ---------------------------------------------------------------------------


@dataclass
class AgentOutput:
    """Result streamed back from an agent run."""

    result: Optional[str] = None
    status: str = "success"  # "success" | "error"
    error: Optional[str] = None
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Channel abstraction
# ---------------------------------------------------------------------------

# Callback types
OnInboundMessage = Callable[[str, "NewMessage"], None]
OnChatMetadata = Callable[[str, str, Optional[str], Optional[str], Optional[bool]], None]


OnRegisterGroup = Callable[[str, "RegisteredGroup"], Coroutine[None, None, None]]


@dataclass
class ChannelOpts:
    """Options passed to channel factories."""

    on_message: OnInboundMessage
    on_chat_metadata: OnChatMetadata
    registered_groups: Callable[[], dict[str, RegisteredGroup]]
    register_group: OnRegisterGroup


class Channel(ABC):
    """Abstract base class for messaging channel implementations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel identifier (e.g. 'slack', 'cli')."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the messaging service."""
        ...

    @abstractmethod
    async def send_message(self, jid: str, text: str) -> None:
        """Send a message to the given JID."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the channel is currently connected."""
        ...

    @abstractmethod
    def owns_jid(self, jid: str) -> bool:
        """Whether this channel is responsible for the given JID."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the messaging service."""
        ...

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        """Optional: show/hide typing indicator."""
        pass

    async def sync_groups(self, force: bool = False) -> None:
        """Optional: sync group/chat names from the platform."""
        pass


# ---------------------------------------------------------------------------
# Channel factory type
# ---------------------------------------------------------------------------

ChannelFactory = Callable[[ChannelOpts], Optional[Channel]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_trigger_pattern(trigger: str) -> re.Pattern[str]:
    """Build a regex that matches a trigger word at the start of a message."""
    escaped = re.escape(trigger.strip())
    return re.compile(rf"^{escaped}\b", re.IGNORECASE)
