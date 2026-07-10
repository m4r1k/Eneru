"""Tests for filesystems config: mount path parsing."""

import pytest
import yaml
from pathlib import Path

from eneru import (
    Config,
    ConfigLoader,
    UPSConfig,
    TriggersConfig,
    NotificationsConfig,
    ContainersConfig,
    ComposeFileConfig,
    RemoteServerConfig,
    RemoteCommandConfig,
)
from test_constants import (
    TEST_DISCORD_WEBHOOK_ID,
    TEST_DISCORD_WEBHOOK_TOKEN,
    TEST_DISCORD_APPRISE_URL,
    TEST_DISCORD_WEBHOOK_URL,
    TEST_SLACK_APPRISE_URL,
)


class TestMountConfiguration:
    """Test filesystem mount configuration parsing."""

    @pytest.mark.unit
    def test_string_mount_paths(self, temp_config_file):
        """Test simple string mount paths."""
        config_data = """
filesystems:
  unmount:
    enabled: true
    mounts:
      - "/mnt/data1"
      - "/mnt/data2"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.filesystems.unmount.mounts) == 2
        assert config.filesystems.unmount.mounts[0]["path"] == "/mnt/data1"
        assert config.filesystems.unmount.mounts[0]["options"] == ""
        assert config.filesystems.unmount.mounts[1]["path"] == "/mnt/data2"

    @pytest.mark.unit
    def test_dict_mount_paths_with_options(self, temp_config_file):
        """Test dictionary mount paths with options."""
        config_data = """
filesystems:
  unmount:
    enabled: true
    mounts:
      - path: "/mnt/nfs"
        options: "-l"
      - path: "/mnt/cifs"
        options: "-f"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.filesystems.unmount.mounts) == 2
        assert config.filesystems.unmount.mounts[0]["path"] == "/mnt/nfs"
        assert config.filesystems.unmount.mounts[0]["options"] == "-l"
        assert config.filesystems.unmount.mounts[1]["path"] == "/mnt/cifs"
        assert config.filesystems.unmount.mounts[1]["options"] == "-f"

    @pytest.mark.unit
    def test_mixed_mount_formats(self, temp_config_file):
        """Test mixed string and dictionary mount formats."""
        config_data = """
filesystems:
  unmount:
    enabled: true
    mounts:
      - "/mnt/local"
      - path: "/mnt/network"
        options: "-l"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.filesystems.unmount.mounts) == 2
        assert config.filesystems.unmount.mounts[0]["path"] == "/mnt/local"
        assert config.filesystems.unmount.mounts[0]["options"] == ""
        assert config.filesystems.unmount.mounts[1]["path"] == "/mnt/network"
        assert config.filesystems.unmount.mounts[1]["options"] == "-l"




class TestMountScalarCharSplitGuard:
    """F-001: a scalar string where a `mounts:` LIST is required used to be
    iterated character-by-character into per-character mount paths (`/mnt/x`
    became mounts "/", "m", "n", "t", "/", "x"). It is now a fatal shape error
    at load, for both the local filesystems block and the remote
    pre_shutdown_commands copy."""

    @pytest.mark.unit
    def test_local_mounts_scalar_not_char_split(self, temp_config_file):
        temp_config_file.write_text(
            "ups:\n  name: U@h\n"
            "filesystems:\n  unmount:\n    enabled: true\n"
            "    mounts: /mnt/data\n")
        with pytest.raises(SystemExit) as exc_info:
            ConfigLoader.load(str(temp_config_file))
        msg = str(exc_info.value)
        assert "mounts" in msg
        assert "must be a list" in msg

    @pytest.mark.unit
    def test_remote_mounts_scalar_not_char_split(self, temp_config_file):
        temp_config_file.write_text(
            "ups:\n  name: U@h\n"
            "remote_servers:\n  - name: nas\n    host: nas.lan\n    user: root\n"
            "    pre_shutdown_commands:\n"
            "      - action: unmount_filesystems\n"
            "        mounts: /mnt/data\n")
        with pytest.raises(SystemExit) as exc_info:
            ConfigLoader.load(str(temp_config_file))
        msg = str(exc_info.value)
        assert "mounts" in msg
        assert "must be a list" in msg

    @pytest.mark.unit
    def test_valid_list_mounts_still_parse(self, temp_config_file):
        """A proper list of mount paths is unaffected — one mount per entry."""
        temp_config_file.write_text(
            "ups:\n  name: U@h\n"
            "filesystems:\n  unmount:\n    enabled: true\n"
            "    mounts:\n      - /mnt/data\n      - /mnt/backup\n")
        config = ConfigLoader.load(str(temp_config_file))
        paths = [m["path"] for m in config.filesystems.unmount.mounts]
        assert paths == ["/mnt/data", "/mnt/backup"]
