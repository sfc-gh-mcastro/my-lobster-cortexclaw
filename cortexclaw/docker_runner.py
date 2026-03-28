"""Docker isolation layer for CortexClaw agent runs.

Provides Docker container isolation inspired by NanoClaw's container-runner.ts.
Instead of running the Cortex Code CLI directly on the host, this module wraps
it inside a Docker container with selective credential mounting, volume isolation,
and per-group configuration.

The key trick: the Cortex Code Agent SDK's ``CortexCodeAgentOptions.cli_path``
accepts any executable.  We generate a shell wrapper script that translates
``cortex <args>`` into ``docker run ... cortex <args>``.  This means zero SDK
changes — the transport layer just runs a different binary.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DATA_DIR,
    DOCKER_CONNECTION,
    DOCKER_IMAGE,
    DOCKER_RUNTIME,
    GROUPS_DIR,
    TIMEZONE,
)
from .types import RegisteredGroup

logger = logging.getLogger(__name__)

# Where we store generated wrapper scripts
_WRAPPERS_DIR: Path = DATA_DIR / "wrappers"

# Default container home directory
_CONTAINER_HOME = "/home/coco"

# Snowflake config location on host
_HOST_SNOWFLAKE_DIR = Path.home() / ".snowflake"
_HOST_CONNECTIONS_TOML = _HOST_SNOWFLAKE_DIR / "connections.toml"


# ---------------------------------------------------------------------------
# Credential extraction
# ---------------------------------------------------------------------------


@dataclass
class ConnectionMount:
    """Paths needed to mount a single Snowflake connection into a container."""

    toml_path: Path  # Temp file with only the target connection
    key_path: Path | None  # Host path to JWT key file (if applicable)
    container_key_path: str | None  # Container path for the key file


def extract_connection_config(
    connection_name: str,
    connections_toml: Path | None = None,
) -> ConnectionMount:
    """Extract a single connection from ``connections.toml`` and write it to a temp file.

    Only the named connection section is written, so the container never sees
    other connections or their credentials (some may contain cleartext passwords).

    If the connection uses ``private_key_path``, the host path is resolved and
    the temp TOML is rewritten with the container-side path.
    """
    toml_path = connections_toml or _HOST_CONNECTIONS_TOML
    if not toml_path.exists():
        raise FileNotFoundError(f"Snowflake connections file not found: {toml_path}")

    # Python 3.11+ has tomllib; fall back to tomli for 3.10
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

    with open(toml_path, "rb") as f:
        all_connections = tomllib.load(f)

    if connection_name not in all_connections:
        available = ", ".join(all_connections.keys())
        raise KeyError(
            f"Connection '{connection_name}' not found in {toml_path}. Available: {available}"
        )

    conn = dict(all_connections[connection_name])

    # Resolve private key path
    host_key_path: Path | None = None
    container_key_path: str | None = None

    for key_field in ("private_key_path", "private_key_file"):
        if key_field in conn:
            raw = conn[key_field]
            # Expand ~ and resolve to absolute
            host_key_path = Path(raw).expanduser().resolve()
            if not host_key_path.exists():
                logger.warning("JWT key file not found: %s", host_key_path)
                host_key_path = None
            else:
                # Rewrite path to container location
                container_key_path = f"{_CONTAINER_HOME}/.snowflake/{host_key_path.name}"
                conn[key_field] = container_key_path
            break

    # Write minimal TOML with just this connection
    temp_dir = DATA_DIR / "docker-credentials"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_toml = temp_dir / f"{connection_name}.toml"

    lines = [f"[{connection_name}]"]
    for k, v in conn.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        else:
            lines.append(f"{k} = {v}")
    temp_toml.write_text("\n".join(lines) + "\n")

    return ConnectionMount(
        toml_path=temp_toml,
        key_path=host_key_path,
        container_key_path=container_key_path,
    )


# ---------------------------------------------------------------------------
# Docker command builder
# ---------------------------------------------------------------------------


def build_docker_args(
    group: RegisteredGroup,
    connection_mount: ConnectionMount,
    *,
    runtime: str = DOCKER_RUNTIME,
    image: str | None = None,
    connection_name: str = DOCKER_CONNECTION,
) -> list[str]:
    """Build ``docker run`` arguments for an isolated agent invocation.

    The returned list is everything before the cortex CLI args.  The caller
    appends ``cortex <sdk-generated-args>`` via the wrapper script.
    """
    effective_image = image or (
        group.container_config.image
        if group.container_config and group.container_config.image
        else DOCKER_IMAGE
    )

    safe_name = group.folder.replace("/", "-").replace(" ", "-")
    container_name = f"cortexclaw-{safe_name}-{int(time.time() * 1000)}"

    group_dir = GROUPS_DIR / group.folder
    ipc_dir = DATA_DIR / "ipc" / group.folder

    args: list[str] = [
        runtime,
        "run",
        "--rm",
        "-i",
        "--name",
        container_name,
    ]

    # Run as host user for correct file permissions on bind mounts
    if os.getuid() != 0:
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])

    # Environment
    args.extend(["-e", f"TZ={TIMEZONE}"])
    args.extend(["-e", f"HOME={_CONTAINER_HOME}"])
    if connection_name:
        args.extend(["-e", f"SNOWFLAKE_DEFAULT_CONNECTION_NAME={connection_name}"])

    # Extra env from ContainerConfig
    if group.container_config:
        for k, v in group.container_config.extra_env.items():
            args.extend(["-e", f"{k}={v}"])

    # --- Volume mounts ---

    # Group working directory (read-write)
    args.extend(["-v", f"{group_dir.resolve()}:/workspace/group:rw"])

    # IPC directory (read-write)
    ipc_dir.mkdir(parents=True, exist_ok=True)
    args.extend(["-v", f"{ipc_dir.resolve()}:/workspace/ipc:rw"])

    # Snowflake credentials (read-only, selective)
    args.extend(
        [
            "-v",
            f"{connection_mount.toml_path.resolve()}:{_CONTAINER_HOME}/.snowflake/connections.toml:ro",
        ]
    )

    # JWT key file (read-only)
    if connection_mount.key_path and connection_mount.container_key_path:
        args.extend(
            [
                "-v",
                f"{connection_mount.key_path.resolve()}:{connection_mount.container_key_path}:ro",
            ]
        )

    # Main groups get project root read-only
    if group.is_main:
        project_root = Path(__file__).resolve().parent.parent
        args.extend(["-v", f"{project_root}:/workspace/project:ro"])

    # Additional mounts from ContainerConfig
    if group.container_config:
        for mount in group.container_config.additional_mounts:
            args.extend(["-v", mount])

    # Working directory inside container
    args.extend(["-w", "/workspace/group"])

    # Image
    args.append(effective_image)

    return args


# ---------------------------------------------------------------------------
# Wrapper script generator
# ---------------------------------------------------------------------------


def create_docker_wrapper(
    group: RegisteredGroup,
    *,
    connection_name: str = DOCKER_CONNECTION,
    runtime: str = DOCKER_RUNTIME,
    image: str | None = None,
) -> Path:
    """Generate a shell wrapper script that runs ``cortex`` inside Docker.

    The Cortex Code SDK calls ``cli_path <args>``.  The wrapper translates
    this to ``docker run <volumes> <env> <image> cortex <args>``.

    Returns the path to the executable wrapper script.
    """
    connection_mount = extract_connection_config(connection_name)
    docker_args = build_docker_args(
        group,
        connection_mount,
        runtime=runtime,
        image=image,
        connection_name=connection_name,
    )

    _WRAPPERS_DIR.mkdir(parents=True, exist_ok=True)
    wrapper_path = _WRAPPERS_DIR / f"{group.folder}.sh"

    # The wrapper passes all its arguments (which are the cortex CLI flags)
    # after "cortex" inside the Docker container
    docker_cmd = " ".join(_shell_quote(a) for a in docker_args)
    script = f"""#!/bin/sh
# Auto-generated by CortexClaw — do not edit
exec {docker_cmd} cortex "$@"
"""

    wrapper_path.write_text(script)
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    logger.debug("Docker wrapper created: %s", wrapper_path)
    return wrapper_path


def _shell_quote(s: str) -> str:
    """Quote a string for safe shell use."""
    if not s:
        return "''"
    # If it contains no special chars, return as-is
    if all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"
