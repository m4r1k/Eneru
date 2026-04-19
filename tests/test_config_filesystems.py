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


