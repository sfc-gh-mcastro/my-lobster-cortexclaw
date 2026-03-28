"""Tests for cortexclaw.docker_runner — credential extraction, command builder, wrapper."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from cortexclaw.docker_runner import (
    ConnectionMount,
    build_docker_args,
    create_docker_wrapper,
    extract_connection_config,
)
from cortexclaw.types import ContainerConfig, RegisteredGroup

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_TOML = """\
[other-conn]
account = "OTHER_ACCOUNT"
user = "other_user"
authenticator = "snowflake"
password = "s3cret"

[my-snowflake-conn]
account = "MYORG-MYACCOUNT"
user = "SVC_AGENT_USER"
role = "AGENT_ROLE"
warehouse = "AGENT_WH"
authenticator = "SNOWFLAKE_JWT"
private_key_path = "{key_path}"

[yet-another]
account = "FOO"
user = "bar"
authenticator = "externalbrowser"
"""


@pytest.fixture()
def connections_toml(tmp_path: Path) -> Path:
    """Write a sample connections.toml with a JWT key file."""
    key_file = tmp_path / "test_key.p8"
    key_file.write_text("FAKE_PRIVATE_KEY")

    toml_content = _SAMPLE_TOML.format(key_path=str(key_file))
    toml_file = tmp_path / "connections.toml"
    toml_file.write_text(toml_content)
    return toml_file


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def _make_group(**overrides) -> RegisteredGroup:
    defaults = dict(
        name="Test Group",
        folder="test-group",
        trigger="@test",
        added_at="2024-01-01T00:00:00",
    )
    defaults.update(overrides)
    return RegisteredGroup(**defaults)


# ---------------------------------------------------------------------------
# extract_connection_config
# ---------------------------------------------------------------------------


class TestExtractConnectionConfig:
    def test_extracts_correct_section(self, connections_toml, data_dir):
        with patch("cortexclaw.docker_runner.DATA_DIR", data_dir):
            mount = extract_connection_config(
                "my-snowflake-conn", connections_toml=connections_toml
            )
        content = mount.toml_path.read_text()
        assert "[my-snowflake-conn]" in content
        assert "MYORG-MYACCOUNT" in content
        assert "SVC_AGENT_USER" in content

    def test_excludes_other_connections(self, connections_toml, data_dir):
        with patch("cortexclaw.docker_runner.DATA_DIR", data_dir):
            mount = extract_connection_config(
                "my-snowflake-conn", connections_toml=connections_toml
            )
        content = mount.toml_path.read_text()
        # Must NOT contain other connections
        assert "[other-conn]" not in content
        assert "s3cret" not in content
        assert "[yet-another]" not in content

    def test_resolves_jwt_key_path(self, connections_toml, data_dir):
        with patch("cortexclaw.docker_runner.DATA_DIR", data_dir):
            mount = extract_connection_config(
                "my-snowflake-conn", connections_toml=connections_toml
            )
        assert mount.key_path is not None
        assert mount.key_path.exists()
        assert mount.key_path.name == "test_key.p8"

    def test_rewrites_key_path_to_container(self, connections_toml, data_dir):
        with patch("cortexclaw.docker_runner.DATA_DIR", data_dir):
            mount = extract_connection_config(
                "my-snowflake-conn", connections_toml=connections_toml
            )
        content = mount.toml_path.read_text()
        assert mount.container_key_path is not None
        assert "/home/coco/.snowflake/" in mount.container_key_path
        # TOML should reference the container path, not the host path
        assert mount.container_key_path in content

    def test_raises_for_unknown_connection(self, connections_toml, data_dir):
        with (
            patch("cortexclaw.docker_runner.DATA_DIR", data_dir),
            pytest.raises(KeyError, match="nonexistent"),
        ):
            extract_connection_config("nonexistent", connections_toml=connections_toml)

    def test_raises_for_missing_toml(self, tmp_path, data_dir):
        with (
            patch("cortexclaw.docker_runner.DATA_DIR", data_dir),
            pytest.raises(FileNotFoundError),
        ):
            extract_connection_config("anything", connections_toml=tmp_path / "does-not-exist.toml")

    def test_connection_without_key(self, tmp_path, data_dir):
        """Connection with password auth (no key file) should have key_path=None."""
        toml_file = tmp_path / "connections.toml"
        toml_file.write_text(
            '[pw-conn]\naccount = "ACC"\nuser = "u"\n'
            'authenticator = "snowflake"\npassword = "pass"\n'
        )
        with patch("cortexclaw.docker_runner.DATA_DIR", data_dir):
            mount = extract_connection_config("pw-conn", connections_toml=toml_file)
        assert mount.key_path is None
        assert mount.container_key_path is None


# ---------------------------------------------------------------------------
# build_docker_args
# ---------------------------------------------------------------------------


class TestBuildDockerArgs:
    def _mount(self, tmp_path: Path) -> ConnectionMount:
        toml = tmp_path / "conn.toml"
        toml.write_text("[test]\naccount = 'X'\n")
        key = tmp_path / "key.p8"
        key.write_text("KEY")
        return ConnectionMount(
            toml_path=toml,
            key_path=key,
            container_key_path="/home/coco/.snowflake/key.p8",
        )

    def test_includes_user_flag(self, tmp_path):
        group = _make_group()
        mount = self._mount(tmp_path)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
            patch("cortexclaw.docker_runner.os.getuid", return_value=501),
            patch("cortexclaw.docker_runner.os.getgid", return_value=20),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            args = build_docker_args(group, mount, runtime="docker")
        assert "--user" in args
        idx = args.index("--user")
        assert args[idx + 1] == "501:20"

    def test_mounts_group_dir_rw(self, tmp_path):
        group = _make_group()
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        group_mount = [v for v in volume_args if "/workspace/group:" in v]
        assert len(group_mount) == 1
        assert group_mount[0].endswith(":rw")

    def test_mounts_credentials_ro(self, tmp_path):
        group = _make_group()
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        cred_mounts = [v for v in volume_args if "connections.toml:" in v]
        assert len(cred_mounts) == 1
        assert cred_mounts[0].endswith(":ro")

    def test_mounts_jwt_key_ro(self, tmp_path):
        group = _make_group()
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        key_mounts = [v for v in volume_args if "key.p8:" in v]
        assert len(key_mounts) == 1
        assert key_mounts[0].endswith(":ro")

    def test_main_group_gets_project_root(self, tmp_path):
        group = _make_group(is_main=True)
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        project_mounts = [v for v in volume_args if "/workspace/project:" in v]
        assert len(project_mounts) == 1
        assert project_mounts[0].endswith(":ro")

    def test_non_main_group_no_project_root(self, tmp_path):
        group = _make_group(is_main=False)
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        project_mounts = [v for v in volume_args if "/workspace/project:" in v]
        assert len(project_mounts) == 0

    def test_extra_env_from_config(self, tmp_path):
        cfg = ContainerConfig(extra_env={"MY_VAR": "hello"})
        group = _make_group(container_config=cfg)
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        env_args = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert "MY_VAR=hello" in env_args

    def test_additional_mounts(self, tmp_path):
        cfg = ContainerConfig(additional_mounts=["/host/data:/container/data:ro"])
        group = _make_group(container_config=cfg)
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        assert "/host/data:/container/data:ro" in volume_args

    def test_no_host_snowflake_dir_mounted(self, tmp_path):
        """The full ~/.snowflake directory must NEVER be mounted."""
        group = _make_group()
        mount = self._mount(tmp_path)
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", tmp_path / "data"),
        ):
            args = build_docker_args(group, mount, runtime="docker")
        joined = " ".join(args)
        assert "/.snowflake:" not in joined or "connections.toml:" in joined


# ---------------------------------------------------------------------------
# create_docker_wrapper
# ---------------------------------------------------------------------------


class TestCreateDockerWrapper:
    def test_generates_executable_script(self, connections_toml, data_dir, tmp_path):
        group = _make_group()
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", data_dir),
            patch("cortexclaw.docker_runner._HOST_CONNECTIONS_TOML", connections_toml),
            patch("cortexclaw.docker_runner.DOCKER_CONNECTION", "my-snowflake-conn"),
        ):
            wrapper = create_docker_wrapper(group, connection_name="my-snowflake-conn")
        assert wrapper.exists()
        assert wrapper.stat().st_mode & stat.S_IEXEC

    def test_script_contains_docker_run(self, connections_toml, data_dir, tmp_path):
        group = _make_group()
        groups_dir = tmp_path / "groups"
        (groups_dir / "test-group").mkdir(parents=True)
        with (
            patch("cortexclaw.docker_runner.GROUPS_DIR", groups_dir),
            patch("cortexclaw.docker_runner.DATA_DIR", data_dir),
            patch("cortexclaw.docker_runner._HOST_CONNECTIONS_TOML", connections_toml),
            patch("cortexclaw.docker_runner.DOCKER_CONNECTION", "my-snowflake-conn"),
        ):
            wrapper = create_docker_wrapper(group, connection_name="my-snowflake-conn")
        content = wrapper.read_text()
        assert "docker run" in content
        assert 'cortex "$@"' in content
        assert "#!/bin/sh" in content
