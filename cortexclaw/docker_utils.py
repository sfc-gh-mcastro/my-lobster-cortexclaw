"""Docker utility functions — health checks, image management, cleanup.

Used during orchestrator startup to verify the Docker environment is ready.
"""

from __future__ import annotations

import asyncio
import logging

from .config import DOCKER_RUNTIME

logger = logging.getLogger(__name__)


async def check_docker_available(runtime: str = DOCKER_RUNTIME) -> None:
    """Verify that the Docker (or Podman) daemon is reachable.

    Raises :class:`RuntimeError` if the daemon is not available.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"{runtime} daemon is not running or not accessible: {detail}")
    except FileNotFoundError:
        raise RuntimeError(
            f"'{runtime}' command not found. Install Docker or set DOCKER_RUNTIME."
        ) from None
    logger.info("Docker runtime verified: %s", runtime)


async def ensure_image_exists(
    image: str,
    runtime: str = DOCKER_RUNTIME,
    *,
    pull_if_missing: bool = True,
) -> None:
    """Ensure the Docker image exists locally, optionally pulling it.

    Raises :class:`RuntimeError` if the image cannot be found or pulled.
    """
    # Check if image exists locally
    proc = await asyncio.create_subprocess_exec(
        runtime,
        "image",
        "inspect",
        image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()

    if proc.returncode == 0:
        logger.info("Docker image found: %s", image)
        return

    if not pull_if_missing:
        raise RuntimeError(f"Docker image '{image}' not found locally and pull_if_missing=False")

    logger.info("Pulling Docker image: %s", image)
    proc = await asyncio.create_subprocess_exec(
        runtime,
        "pull",
        image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Failed to pull Docker image '{image}': {detail}")
    logger.info("Docker image pulled: %s", image)


async def cleanup_stale_containers(
    prefix: str = "cortexclaw-",
    runtime: str = DOCKER_RUNTIME,
) -> int:
    """Remove dead containers matching the given name prefix.

    Returns the number of containers removed.
    """
    # List containers with our prefix (including stopped)
    proc = await asyncio.create_subprocess_exec(
        runtime,
        "ps",
        "-a",
        "--filter",
        f"name={prefix}",
        "--filter",
        "status=exited",
        "--format",
        "{{.Names}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    names = [n.strip() for n in stdout.decode().splitlines() if n.strip()]

    if not names:
        return 0

    proc = await asyncio.create_subprocess_exec(
        runtime,
        "rm",
        *names,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    logger.info("Cleaned up %d stale containers", len(names))
    return len(names)
