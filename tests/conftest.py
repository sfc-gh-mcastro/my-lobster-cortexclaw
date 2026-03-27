"""Shared fixtures for CortexClaw tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexclaw import db


@pytest.fixture()
async def in_memory_db():
    """Initialise an in-memory SQLite database and tear it down after the test."""
    await db.init_database(Path(":memory:"))
    yield
    await db.close_database()


@pytest.fixture()
def tmp_groups_dir(tmp_path: Path) -> Path:
    """Return a temporary directory usable as GROUPS_DIR."""
    d = tmp_path / "groups"
    d.mkdir()
    return d
