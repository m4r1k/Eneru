"""Tests for configuration loading and parsing."""

import pytest
import yaml
from pathlib import Path

from ups_monitor import (
    Config,
    ConfigLoader,
    UPSConfig,
    TriggersConfig,
    NotificationsConfig,
    ContainersConfig,
    ComposeFileConfig,
)


class TestConfigDefaults:
    """Test default configuration values."""

    @pytest.mark.unit
    def test_default_ups_config(self, default_config):
        """Test default UPS configuration."""
        assert default_config.ups.name == "UPS@localhost"
        assert default_config.ups.check_interval == 1
        assert default_config.ups.max_stale_data_tolerance == 3

    @pytest.mark.unit
    def test_default_triggers(self, default_config):
        """Test default trigger thresholds."""
        assert default_config.triggers.low_battery_threshold == 20
        assert default_config.triggers.critical_runtime_threshold == 600
        assert default_config.triggers.depletion.window == 300
        assert default_config.triggers.depletion.critical_rate == 15.0
        assert default_config.triggers.depletion.grace_period == 90
        assert default_config.triggers.extended_time.enabled is True
        assert default_config.triggers.extended_time.threshold == 900

    @pytest.mark.unit
    def test_default_behavior(self, default_config):
        """Test default behavior settings."""
        assert default_config.behavior.dry_run is False

    @pytest.mark.unit
    def test_default_notifications_disabled(self, default_config):
        """Test that notifications are disabled by default."""
        assert default_config.notifications.enabled is False
        assert default_config.notifications.urls == []

    @pytest.mark.unit
    def test_default_shutdown_components(self, default_config):
        """Test default shutdown component settings."""
        assert default_config.virtual_machines.enabled is False
        assert default_config.containers.enabled is False
        assert default_config.filesystems.sync_enabled is True
        assert default_config.local_shutdown.enabled is True


class TestConfigLoading:
    """Test configuration file loading."""

    @pytest.mark.unit
    def test_load_minimal_config(self, temp_config_file):
        """Test loading a minimal configuration."""
        config_data = """
ups:
  name: "TestUPS@192.168.1.1"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.ups.name == "TestUPS@192.168.1.1"
        # Defaults should be preserved
        assert config.ups.check_interval == 1
        assert config.triggers.low_battery_threshold == 20

    @pytest.mark.unit
    def test_load_full_config(self, temp_config_file):
        """Test loading a full configuration."""
        config_data = """
ups:
  name: "UPS@192.168.178.11"
  check_interval: 2
  max_stale_data_tolerance: 5

triggers:
  low_battery_threshold: 25
  critical_runtime_threshold: 900
  depletion:
    window: 600
    critical_rate: 10.0
    grace_period: 120
  extended_time:
    enabled: false
    threshold: 1200

behavior:
  dry_run: true

notifications:
  title: "Test UPS"
  urls:
    - "discord://webhook_id/webhook_token"

virtual_machines:
  enabled: true
  max_wait: 60

containers:
  enabled: true
  runtime: "podman"
  stop_timeout: 90
  include_user_containers: true

local_shutdown:
  enabled: true
  command: "poweroff"
  message: "Test message"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.ups.name == "UPS@192.168.178.11"
        assert config.ups.check_interval == 2
        assert config.ups.max_stale_data_tolerance == 5
        assert config.triggers.low_battery_threshold == 25
        assert config.triggers.critical_runtime_threshold == 900
        assert config.triggers.depletion.window == 600
        assert config.triggers.depletion.critical_rate == 10.0
        assert config.triggers.depletion.grace_period == 120
        assert config.triggers.extended_time.enabled is False
        assert config.triggers.extended_time.threshold == 1200
        assert config.behavior.dry_run is True
        assert config.notifications.enabled is True
        assert config.notifications.title == "Test UPS"
        assert len(config.notifications.urls) == 1
        assert config.virtual_machines.enabled is True
        assert config.virtual_machines.max_wait == 60
        assert config.containers.enabled is True
        assert config.containers.runtime == "podman"
        assert config.containers.stop_timeout == 90
        assert config.containers.include_user_containers is True
        assert config.local_shutdown.command == "poweroff"

    @pytest.mark.unit
    def test_load_nonexistent_file(self):
        """Test loading a non-existent file returns defaults."""
        config = ConfigLoader.load("/nonexistent/path/config.yaml")
        assert config.ups.name == "UPS@localhost"

    @pytest.mark.unit
    def test_load_empty_file(self, temp_config_file):
        """Test loading an empty file returns defaults."""
        temp_config_file.write_text("")
        config = ConfigLoader.load(str(temp_config_file))
        assert config.ups.name == "UPS@localhost"

    @pytest.mark.unit
    def test_load_invalid_yaml(self, temp_config_file):
        """Test loading invalid YAML returns defaults."""
        temp_config_file.write_text("invalid: yaml: content: [")
        config = ConfigLoader.load(str(temp_config_file))
        assert config.ups.name == "UPS@localhost"


class TestLegacyDiscordConfig:
    """Test legacy Discord configuration conversion."""

    @pytest.mark.unit
    def test_legacy_discord_webhook_conversion(self, temp_config_file):
        """Test that legacy Discord webhook is converted to Apprise format."""
        config_data = """
notifications:
  discord:
    webhook_url: "https://discord.com/api/webhooks/123456789/abcdefghijk"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert len(config.notifications.urls) == 1
        assert config.notifications.urls[0].startswith("discord://")
        assert "123456789" in config.notifications.urls[0]
        assert "abcdefghijk" in config.notifications.urls[0]

    @pytest.mark.unit
    def test_top_level_legacy_discord(self, temp_config_file):
        """Test top-level legacy Discord configuration."""
        config_data = """
discord:
  webhook_url: "https://discord.com/api/webhooks/999/token"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert "discord://" in config.notifications.urls[0]

    @pytest.mark.unit
    def test_discord_webhook_to_apprise_format(self):
        """Test the webhook URL conversion function."""
        webhook = "https://discord.com/api/webhooks/123/abc"
        result = ConfigLoader._convert_discord_webhook_to_apprise(webhook)
        assert result == "discord://123/abc/"

    @pytest.mark.unit
    def test_non_discord_url_unchanged(self):
        """Test that non-Discord URLs are not modified."""
        url = "slack://token/channel"
        result = ConfigLoader._convert_discord_webhook_to_apprise(url)
        assert result == url


class TestAvatarUrlAppending:
    """Test avatar URL appending to notification URLs."""

    @pytest.mark.unit
    def test_append_avatar_to_discord(self):
        """Test appending avatar to Discord URL."""
        url = "discord://123/token"
        avatar = "https://example.com/avatar.png"
        result = ConfigLoader._append_avatar_to_url(url, avatar)

        assert "avatar_url=" in result
        assert "example.com" in result

    @pytest.mark.unit
    def test_append_avatar_to_slack(self):
        """Test appending avatar to Slack URL."""
        url = "slack://token/#channel"
        avatar = "https://example.com/icon.png"
        result = ConfigLoader._append_avatar_to_url(url, avatar)

        assert "avatar_url=" in result

    @pytest.mark.unit
    def test_no_avatar_for_unsupported_service(self):
        """Test that avatar is not appended to unsupported services."""
        url = "mailto://user:pass@smtp.example.com"
        avatar = "https://example.com/avatar.png"
        result = ConfigLoader._append_avatar_to_url(url, avatar)

        assert result == url
        assert "avatar_url" not in result

    @pytest.mark.unit
    def test_no_avatar_when_none(self):
        """Test that nothing is appended when avatar is None."""
        url = "discord://123/token"
        result = ConfigLoader._append_avatar_to_url(url, None)
        assert result == url

    @pytest.mark.unit
    def test_no_avatar_when_empty(self):
        """Test that nothing is appended when avatar is empty."""
        url = "discord://123/token"
        result = ConfigLoader._append_avatar_to_url(url, "")
        assert result == url


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


class TestComposeFilesConfig:
    """Test compose files configuration parsing."""

    @pytest.mark.unit
    def test_string_compose_paths(self, temp_config_file):
        """Test simple string compose file paths."""
        config_data = """
containers:
  enabled: true
  compose_files:
    - "/path/to/docker-compose.yml"
    - "/another/path/compose.yaml"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/path/to/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout is None
        assert config.containers.compose_files[1].path == "/another/path/compose.yaml"

    @pytest.mark.unit
    def test_dict_compose_paths_with_timeout(self, temp_config_file):
        """Test dictionary compose paths with custom timeout."""
        config_data = """
containers:
  enabled: true
  stop_timeout: 60
  compose_files:
    - path: "/path/to/critical-db/docker-compose.yml"
      stop_timeout: 120
    - path: "/path/to/app/docker-compose.yml"
      stop_timeout: 30
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/path/to/critical-db/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout == 120
        assert config.containers.compose_files[1].path == "/path/to/app/docker-compose.yml"
        assert config.containers.compose_files[1].stop_timeout == 30

    @pytest.mark.unit
    def test_mixed_compose_formats(self, temp_config_file):
        """Test mixed string and dictionary compose file formats."""
        config_data = """
containers:
  enabled: true
  compose_files:
    - "/simple/path/docker-compose.yml"
    - path: "/path/with/timeout/docker-compose.yml"
      stop_timeout: 180
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.containers.compose_files) == 2
        assert config.containers.compose_files[0].path == "/simple/path/docker-compose.yml"
        assert config.containers.compose_files[0].stop_timeout is None
        assert config.containers.compose_files[1].path == "/path/with/timeout/docker-compose.yml"
        assert config.containers.compose_files[1].stop_timeout == 180

    @pytest.mark.unit
    def test_shutdown_all_remaining_containers_default(self, temp_config_file):
        """Test that shutdown_all_remaining_containers defaults to True."""
        config_data = """
containers:
  enabled: true
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.shutdown_all_remaining_containers is True

    @pytest.mark.unit
    def test_shutdown_all_remaining_containers_false(self, temp_config_file):
        """Test setting shutdown_all_remaining_containers to False."""
        config_data = """
containers:
  enabled: true
  shutdown_all_remaining_containers: false
  compose_files:
    - "/path/to/docker-compose.yml"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.shutdown_all_remaining_containers is False

    @pytest.mark.unit
    def test_empty_compose_files(self, temp_config_file):
        """Test empty compose_files list."""
        config_data = """
containers:
  enabled: true
  compose_files: []
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.compose_files == []

    @pytest.mark.unit
    def test_no_compose_files_key(self, temp_config_file):
        """Test missing compose_files key defaults to empty list."""
        config_data = """
containers:
  enabled: true
  runtime: docker
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.compose_files == []


class TestRemoteServersConfig:
    """Test remote servers configuration parsing."""

    @pytest.mark.unit
    def test_multiple_remote_servers(self, temp_config_file):
        """Test multiple remote server configurations."""
        config_data = """
remote_servers:
  - name: "NAS 1"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "sudo shutdown -h now"
  - name: "NAS 2"
    enabled: false
    host: "192.168.1.51"
    user: "root"
    connect_timeout: 15
    command_timeout: 45
    shutdown_command: "poweroff"
    ssh_options:
      - "-o StrictHostKeyChecking=no"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 2

        server1 = config.remote_servers[0]
        assert server1.name == "NAS 1"
        assert server1.enabled is True
        assert server1.host == "192.168.1.50"
        assert server1.user == "admin"
        assert server1.shutdown_command == "sudo shutdown -h now"
        assert server1.connect_timeout == 10  # default
        assert server1.command_timeout == 30  # default

        server2 = config.remote_servers[1]
        assert server2.name == "NAS 2"
        assert server2.enabled is False
        assert server2.host == "192.168.1.51"
        assert server2.user == "root"
        assert server2.connect_timeout == 15
        assert server2.command_timeout == 45
        assert server2.shutdown_command == "poweroff"
        assert "-o StrictHostKeyChecking=no" in server2.ssh_options


class TestConfigValidation:
    """Test configuration validation."""

    @pytest.mark.unit
    def test_validate_config_with_apprise(self, full_config):
        """Test validation returns info about Apprise."""
        messages = ConfigLoader.validate_config(full_config)
        # Should have message about Discord via Apprise
        assert any("Discord" in msg or "Apprise" in msg for msg in messages)

    @pytest.mark.unit
    def test_validate_config_empty_notifications(self, minimal_config):
        """Test validation with no notifications configured."""
        messages = ConfigLoader.validate_config(minimal_config)
        # Should not have warnings about missing Apprise
        assert not any("WARNING" in msg for msg in messages)
