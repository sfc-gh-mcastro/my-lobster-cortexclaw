"""CortexClaw configuration.

Reads from environment variables and .env file, mirroring NanoClaw's config.ts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Load .env from the project root (parent of this package)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Assistant identity
# ---------------------------------------------------------------------------

ASSISTANT_NAME: str = os.getenv("ASSISTANT_NAME", "CortexClaw")

# ---------------------------------------------------------------------------
# Timing (milliseconds unless noted)
# ---------------------------------------------------------------------------

POLL_INTERVAL: float = int(os.getenv("POLL_INTERVAL", "2000")) / 1000.0  # seconds
SCHEDULER_POLL_INTERVAL: float = int(os.getenv("SCHEDULER_POLL_INTERVAL", "60000")) / 1000.0
IDLE_TIMEOUT: int = int(os.getenv("IDLE_TIMEOUT", "1800000"))  # 30 min
MAX_CONCURRENT_AGENTS: int = max(1, int(os.getenv("MAX_CONCURRENT_AGENTS", "5")))
IPC_POLL_INTERVAL: float = int(os.getenv("IPC_POLL_INTERVAL", "1000")) / 1000.0

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

STORE_DIR: Path = Path(os.getenv("STORE_DIR", str(_PROJECT_ROOT / "store")))
GROUPS_DIR: Path = Path(os.getenv("GROUPS_DIR", str(_PROJECT_ROOT / "groups")))
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))

# ---------------------------------------------------------------------------
# Cortex Code SDK
# ---------------------------------------------------------------------------

CORTEX_CONNECTION: str = os.getenv("CORTEX_CONNECTION", "")
CORTEX_CLI_PATH: str = os.getenv("CORTEX_CLI_PATH", "cortex")

# ---------------------------------------------------------------------------
# Docker isolation (default: enabled)
# ---------------------------------------------------------------------------

DOCKER_ENABLED: bool = os.getenv("DOCKER_ENABLED", "true").lower() in ("true", "1", "yes")
DOCKER_IMAGE: str = os.getenv("DOCKER_IMAGE", "cortexclaw-agent:latest")
DOCKER_RUNTIME: str = os.getenv("DOCKER_RUNTIME", "docker")
DOCKER_CONNECTION: str = os.getenv("DOCKER_CONNECTION", "")

# ---------------------------------------------------------------------------
# Static group configuration
# ---------------------------------------------------------------------------

GROUPS_CONFIG: Path = Path(os.getenv("GROUPS_CONFIG", str(_PROJECT_ROOT / "groups.toml")))

# ---------------------------------------------------------------------------
# Channel credentials
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")

# ---------------------------------------------------------------------------
# CLI channel
# ---------------------------------------------------------------------------

ENABLE_CLI_CHANNEL: bool = os.getenv("ENABLE_CLI_CHANNEL", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


def _resolve_timezone() -> str:
    """Resolve IANA timezone from env → TZ → system default → UTC."""
    for candidate in (os.getenv("TZ"),):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, KeyError):
                pass
    # System default
    try:
        import time

        local_tz = time.tzname[0]
        if local_tz:
            ZoneInfo(local_tz)
            return local_tz
    except Exception:
        pass
    return "UTC"


TIMEZONE: str = _resolve_timezone()

# ---------------------------------------------------------------------------
# Trigger helpers
# ---------------------------------------------------------------------------

DEFAULT_TRIGGER: str = f"@{ASSISTANT_NAME}"


def build_trigger_pattern(trigger: str | None = None) -> re.Pattern[str]:
    """Build a regex that matches a trigger word at the start of a message."""
    t = (trigger or DEFAULT_TRIGGER).strip()
    escaped = re.escape(t)
    return re.compile(rf"^{escaped}\b", re.IGNORECASE)


TRIGGER_PATTERN: re.Pattern[str] = build_trigger_pattern()
