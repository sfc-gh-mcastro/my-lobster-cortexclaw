"""Tests for cortexclaw.groups_config — static group loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexclaw.groups_config import load_groups_config
from cortexclaw.types import ContainerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "groups.toml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestLoadGroupsConfig:
    """Core loading behaviour."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_groups_config(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        p = _write_toml(tmp_path, "")
        result = load_groups_config(p)
        assert result == {}

    def test_single_group_minimal(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[sales]
name = "Sales Team"
folder = "sales"
trigger = "@sales"
""",
        )
        groups = load_groups_config(p)
        assert len(groups) == 1
        assert "static:sales" in groups

        g = groups["static:sales"]
        assert g.name == "Sales Team"
        assert g.folder == "sales"
        assert g.trigger == "@sales"
        assert g.requires_trigger is True
        assert g.is_main is False
        assert g.container_config is None
        assert g.added_at  # non-empty timestamp

    def test_multiple_groups(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[sales]
name = "Sales"
folder = "sales"
trigger = "@sales"

[eng]
name = "Engineering"
folder = "eng"
trigger = "@eng"

[support]
name = "Support"
folder = "support"
trigger = "@support"
""",
        )
        groups = load_groups_config(p)
        assert len(groups) == 3
        assert set(groups.keys()) == {"static:sales", "static:eng", "static:support"}

    def test_optional_flags(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[main]
name = "Main Group"
folder = "main"
trigger = "@main"
requires_trigger = false
is_main = true
""",
        )
        g = load_groups_config(p)["static:main"]
        assert g.requires_trigger is False
        assert g.is_main is True


# ---------------------------------------------------------------------------
# ContainerConfig mapping
# ---------------------------------------------------------------------------


class TestContainerConfig:
    """Optional Docker/container overrides."""

    def test_image_override(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[eng]
name = "Engineering"
folder = "eng"
trigger = "@eng"
image = "cortexclaw-agent-eng:latest"
""",
        )
        g = load_groups_config(p)["static:eng"]
        assert g.container_config is not None
        assert g.container_config.image == "cortexclaw-agent-eng:latest"
        assert g.container_config.timeout == 300_000  # default

    def test_timeout_override(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[support]
name = "Support"
folder = "support"
trigger = "@support"
timeout = 600000
""",
        )
        g = load_groups_config(p)["static:support"]
        assert g.container_config is not None
        assert g.container_config.timeout == 600_000

    def test_docker_enabled_false(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[local]
name = "Local Dev"
folder = "local"
trigger = "@local"
docker_enabled = false
""",
        )
        g = load_groups_config(p)["static:local"]
        assert g.container_config is not None
        assert g.container_config.docker_enabled is False

    def test_extra_env(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[eng]
name = "Engineering"
folder = "eng"
trigger = "@eng"

[eng.extra_env]
MY_VAR = "hello"
DEBUG = "1"
""",
        )
        g = load_groups_config(p)["static:eng"]
        assert g.container_config is not None
        assert g.container_config.extra_env == {"MY_VAR": "hello", "DEBUG": "1"}

    def test_additional_mounts(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[eng]
name = "Engineering"
folder = "eng"
trigger = "@eng"
additional_mounts = ["/host/data:/data:ro"]
""",
        )
        g = load_groups_config(p)["static:eng"]
        assert g.container_config is not None
        assert g.container_config.additional_mounts == ["/host/data:/data:ro"]

    def test_no_container_config_when_no_docker_fields(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[plain]
name = "Plain"
folder = "plain"
trigger = "@plain"
""",
        )
        g = load_groups_config(p)["static:plain"]
        assert g.container_config is None

    def test_all_container_fields(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[full]
name = "Full Config"
folder = "full"
trigger = "@full"
image = "custom:v2"
timeout = 120000
docker_enabled = true
additional_mounts = ["/a:/b:rw", "/c:/d:ro"]

[full.extra_env]
FOO = "bar"
""",
        )
        g = load_groups_config(p)["static:full"]
        cc = g.container_config
        assert cc is not None
        assert cc.image == "custom:v2"
        assert cc.timeout == 120_000
        assert cc.docker_enabled is True
        assert cc.extra_env == {"FOO": "bar"}
        assert cc.additional_mounts == ["/a:/b:rw", "/c:/d:ro"]


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    """Missing required fields should raise ValueError."""

    def test_missing_name(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[bad]
folder = "bad"
trigger = "@bad"
""",
        )
        with pytest.raises(ValueError, match="missing required fields.*name"):
            load_groups_config(p)

    def test_missing_folder(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[bad]
name = "Bad"
trigger = "@bad"
""",
        )
        with pytest.raises(ValueError, match="missing required fields.*folder"):
            load_groups_config(p)

    def test_missing_trigger(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[bad]
name = "Bad"
folder = "bad"
""",
        )
        with pytest.raises(ValueError, match="missing required fields.*trigger"):
            load_groups_config(p)

    def test_missing_multiple_fields(self, tmp_path: Path) -> None:
        p = _write_toml(
            tmp_path,
            """
[bad]
trigger = "@bad"
""",
        )
        with pytest.raises(ValueError, match="missing required fields"):
            load_groups_config(p)

    def test_non_table_entry_skipped(self, tmp_path: Path) -> None:
        """Top-level scalar values are skipped, not treated as groups."""
        p = _write_toml(
            tmp_path,
            """
version = 1

[sales]
name = "Sales"
folder = "sales"
trigger = "@sales"
""",
        )
        groups = load_groups_config(p)
        assert len(groups) == 1
        assert "static:sales" in groups
