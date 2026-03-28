"""Load static group definitions from a TOML config file.

Groups defined here are registered at orchestrator startup, allowing multiple
groups (sales, eng, support, etc.) to run without needing Slack or IPC.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .types import ContainerConfig, RegisteredGroup

logger = logging.getLogger(__name__)

# Required keys in each group section
_REQUIRED_KEYS = ("name", "folder", "trigger")


def _parse_toml(path: Path) -> dict:
    """Parse a TOML file, handling Python 3.10 vs 3.11+."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ImportError(
                "Python 3.10 requires the 'tomli' package to parse TOML. "
                "Install with: pip install tomli"
            )
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_groups_config(path: Path | None = None) -> dict[str, RegisteredGroup]:
    """Load group definitions from a TOML file.

    Args:
        path: Path to the TOML config file. If *None*, uses the default
              from ``config.GROUPS_CONFIG``.

    Returns:
        Mapping of JID (``static:<section_key>``) to ``RegisteredGroup``.
        Returns an empty dict if the file does not exist.

    Raises:
        ValueError: If a group section is missing required fields.
    """
    if path is None:
        from .config import GROUPS_CONFIG

        path = GROUPS_CONFIG

    if not path.exists():
        logger.debug("No groups config at %s, skipping static groups", path)
        return {}

    raw = _parse_toml(path)
    groups: dict[str, RegisteredGroup] = {}
    now = datetime.now(timezone.utc).isoformat()

    for key, section in raw.items():
        if not isinstance(section, dict):
            logger.warning("Skipping non-table entry '%s' in groups config", key)
            continue

        # Validate required fields
        missing = [k for k in _REQUIRED_KEYS if k not in section]
        if missing:
            raise ValueError(
                f"Group '{key}' is missing required fields: {', '.join(missing)}"
            )

        # Build optional ContainerConfig from recognized keys
        container_config: ContainerConfig | None = None
        has_container_fields = any(
            k in section
            for k in ("image", "timeout", "docker_enabled", "extra_env", "additional_mounts")
        )
        if has_container_fields:
            container_config = ContainerConfig(
                timeout=section.get("timeout", 300_000),
                extra_env=section.get("extra_env", {}),
                docker_enabled=section.get("docker_enabled"),
                image=section.get("image"),
                additional_mounts=section.get("additional_mounts", []),
            )

        group = RegisteredGroup(
            name=section["name"],
            folder=section["folder"],
            trigger=section["trigger"],
            added_at=now,
            container_config=container_config,
            requires_trigger=section.get("requires_trigger", True),
            is_main=section.get("is_main", False),
        )

        jid = f"static:{key}"
        groups[jid] = group

    logger.info("Loaded %d static group(s) from %s", len(groups), path)
    return groups
