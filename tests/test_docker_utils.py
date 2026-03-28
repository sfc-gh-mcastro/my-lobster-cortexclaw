"""Tests for cortexclaw.docker_utils — Docker health checks and image management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cortexclaw.docker_utils import (
    check_docker_available,
    cleanup_stale_containers,
    ensure_image_exists,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# check_docker_available
# ---------------------------------------------------------------------------


class TestCheckDockerAvailable:
    async def test_succeeds_when_docker_running(self):
        proc = _mock_proc(returncode=0)
        with patch("cortexclaw.docker_utils.asyncio.create_subprocess_exec", return_value=proc):
            await check_docker_available("docker")

    async def test_raises_when_daemon_not_running(self):
        proc = _mock_proc(returncode=1, stderr=b"Cannot connect to daemon")
        with (
            patch("cortexclaw.docker_utils.asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="daemon is not running"),
        ):
            await check_docker_available("docker")

    async def test_raises_when_command_not_found(self):
        with (
            patch(
                "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(RuntimeError, match="not found"),
        ):
            await check_docker_available("docker")


# ---------------------------------------------------------------------------
# ensure_image_exists
# ---------------------------------------------------------------------------


class TestEnsureImageExists:
    async def test_skips_pull_when_image_exists(self):
        proc = _mock_proc(returncode=0)
        with patch(
            "cortexclaw.docker_utils.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            await ensure_image_exists("my-image:latest", "docker")
        # Should only call image inspect, not pull
        assert mock_exec.call_count == 1
        call_args = mock_exec.call_args[0]
        assert "inspect" in call_args

    async def test_pulls_when_image_missing(self):
        inspect_proc = _mock_proc(returncode=1)  # image not found
        pull_proc = _mock_proc(returncode=0)  # pull succeeds
        with patch(
            "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
            side_effect=[inspect_proc, pull_proc],
        ) as mock_exec:
            await ensure_image_exists("my-image:latest", "docker")
        assert mock_exec.call_count == 2

    async def test_raises_when_pull_fails(self):
        inspect_proc = _mock_proc(returncode=1)
        pull_proc = _mock_proc(returncode=1, stderr=b"pull error")
        with (
            patch(
                "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
                side_effect=[inspect_proc, pull_proc],
            ),
            pytest.raises(RuntimeError, match="Failed to pull"),
        ):
            await ensure_image_exists("bad-image:latest", "docker")

    async def test_raises_when_pull_disabled(self):
        inspect_proc = _mock_proc(returncode=1)
        with (
            patch(
                "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
                return_value=inspect_proc,
            ),
            pytest.raises(RuntimeError, match="pull_if_missing=False"),
        ):
            await ensure_image_exists("missing:latest", "docker", pull_if_missing=False)


# ---------------------------------------------------------------------------
# cleanup_stale_containers
# ---------------------------------------------------------------------------


class TestCleanupStaleContainers:
    async def test_removes_dead_containers(self):
        list_proc = _mock_proc(
            returncode=0,
            stdout=b"cortexclaw-grp1-123\ncortexclaw-grp2-456\n",
        )
        rm_proc = _mock_proc(returncode=0)
        with patch(
            "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
            side_effect=[list_proc, rm_proc],
        ):
            count = await cleanup_stale_containers("cortexclaw-", "docker")
        assert count == 2

    async def test_returns_zero_when_none_found(self):
        list_proc = _mock_proc(returncode=0, stdout=b"")
        with patch(
            "cortexclaw.docker_utils.asyncio.create_subprocess_exec",
            return_value=list_proc,
        ):
            count = await cleanup_stale_containers("cortexclaw-", "docker")
        assert count == 0
