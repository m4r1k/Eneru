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
        config_data = f"""
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
    - "{TEST_DISCORD_APPRISE_URL}"

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
        config_data = f"""
notifications:
  discord:
    webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert len(config.notifications.urls) == 1
        assert config.notifications.urls[0].startswith("discord://")
        assert TEST_DISCORD_WEBHOOK_ID in config.notifications.urls[0]
        assert TEST_DISCORD_WEBHOOK_TOKEN in config.notifications.urls[0]

    @pytest.mark.unit
    def test_top_level_legacy_discord(self, temp_config_file):
        """Test top-level legacy Discord configuration."""
        config_data = f"""
discord:
  webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert "discord://" in config.notifications.urls[0]

    @pytest.mark.unit
    def test_discord_webhook_to_apprise_format(self):
        """Test the webhook URL conversion function."""
        result = ConfigLoader._convert_discord_webhook_to_apprise(TEST_DISCORD_WEBHOOK_URL)
        assert result == f"discord://{TEST_DISCORD_WEBHOOK_ID}/{TEST_DISCORD_WEBHOOK_TOKEN}/"

    @pytest.mark.unit
    def test_non_discord_url_unchanged(self):
        """Test that non-Discord URLs are not modified."""
        result = ConfigLoader._convert_discord_webhook_to_apprise(TEST_SLACK_APPRISE_URL)
        assert result == TEST_SLACK_APPRISE_URL


class TestAvatarUrlAppending:
    """Test avatar URL appending to notification URLs."""

    @pytest.mark.unit
    def test_append_avatar_to_discord(self):
        """Test appending avatar to Discord URL."""
        avatar = "https://example.com/avatar.png"
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, avatar)

        assert "avatar_url=" in result
        assert "example.com" in result

    @pytest.mark.unit
    def test_append_avatar_to_slack(self):
        """Test appending avatar to Slack URL."""
        avatar = "https://example.com/icon.png"
        result = ConfigLoader._append_avatar_to_url(TEST_SLACK_APPRISE_URL, avatar)

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
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, None)
        assert result == TEST_DISCORD_APPRISE_URL

    @pytest.mark.unit
    def test_no_avatar_when_empty(self):
        """Test that nothing is appended when avatar is empty."""
        result = ConfigLoader._append_avatar_to_url(TEST_DISCORD_APPRISE_URL, "")
        assert result == TEST_DISCORD_APPRISE_URL


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

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_actions(self, temp_config_file):
        """Test pre_shutdown_commands with predefined actions."""
        config_data = """
remote_servers:
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.1.60"
    user: "root"
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 120
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "sync"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 1
        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 3

        cmd1 = server.pre_shutdown_commands[0]
        assert cmd1.action == "stop_proxmox_vms"
        assert cmd1.timeout == 120
        assert cmd1.command is None

        cmd2 = server.pre_shutdown_commands[1]
        assert cmd2.action == "stop_proxmox_cts"
        assert cmd2.timeout == 60

        cmd3 = server.pre_shutdown_commands[2]
        assert cmd3.action == "sync"
        assert cmd3.timeout is None  # Uses server default

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_custom_command(self, temp_config_file):
        """Test pre_shutdown_commands with custom commands."""
        config_data = """
remote_servers:
  - name: "Docker Server"
    enabled: true
    host: "192.168.1.70"
    user: "root"
    pre_shutdown_commands:
      - command: "systemctl stop my-service"
        timeout: 30
      - command: "docker stop $(docker ps -q)"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 2

        cmd1 = server.pre_shutdown_commands[0]
        assert cmd1.command == "systemctl stop my-service"
        assert cmd1.timeout == 30
        assert cmd1.action is None

        cmd2 = server.pre_shutdown_commands[1]
        assert cmd2.command == "docker stop $(docker ps -q)"
        assert cmd2.timeout is None

    @pytest.mark.unit
    def test_pre_shutdown_commands_with_compose_path(self, temp_config_file):
        """Test pre_shutdown_commands with stop_compose action and path."""
        config_data = """
remote_servers:
  - name: "Docker Server"
    enabled: true
    host: "192.168.1.70"
    user: "root"
    pre_shutdown_commands:
      - action: "stop_compose"
        path: "/opt/myapp/docker-compose.yml"
        timeout: 120
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert len(server.pre_shutdown_commands) == 1

        cmd = server.pre_shutdown_commands[0]
        assert cmd.action == "stop_compose"
        assert cmd.path == "/opt/myapp/docker-compose.yml"
        assert cmd.timeout == 120

    @pytest.mark.unit
    def test_pre_shutdown_commands_mixed(self, temp_config_file):
        """Test pre_shutdown_commands with mixed actions and commands."""
        config_data = """
remote_servers:
  - name: "Mixed Server"
    enabled: true
    host: "192.168.1.80"
    user: "root"
    command_timeout: 45
    pre_shutdown_commands:
      - action: "stop_containers"
        timeout: 90
      - command: "systemctl stop nginx"
        timeout: 15
      - action: "sync"
    shutdown_command: "poweroff"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.command_timeout == 45
        assert len(server.pre_shutdown_commands) == 3

        # Action with custom timeout
        assert server.pre_shutdown_commands[0].action == "stop_containers"
        assert server.pre_shutdown_commands[0].timeout == 90

        # Custom command
        assert server.pre_shutdown_commands[1].command == "systemctl stop nginx"
        assert server.pre_shutdown_commands[1].timeout == 15

        # Action without timeout (uses server default)
        assert server.pre_shutdown_commands[2].action == "sync"
        assert server.pre_shutdown_commands[2].timeout is None

    @pytest.mark.unit
    def test_pre_shutdown_commands_empty(self, temp_config_file):
        """Test server with empty pre_shutdown_commands."""
        config_data = """
remote_servers:
  - name: "Simple Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    pre_shutdown_commands: []
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_pre_shutdown_commands_not_specified(self, temp_config_file):
        """Test server without pre_shutdown_commands field (backward compatible)."""
        config_data = """
remote_servers:
  - name: "Legacy Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_parallel_option_default_true(self, temp_config_file):
        """Test that parallel defaults to True when not specified."""
        config_data = """
remote_servers:
  - name: "Server Without Parallel"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.parallel is True

    @pytest.mark.unit
    def test_parallel_option_explicit_false(self, temp_config_file):
        """Test setting parallel to False."""
        config_data = """
remote_servers:
  - name: "Sequential Server"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    parallel: false
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.parallel is False

    @pytest.mark.unit
    def test_parallel_option_mixed(self, temp_config_file):
        """Test mixed parallel and sequential servers."""
        config_data = """
remote_servers:
  - name: "Parallel Server 1"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "shutdown -h now"
  - name: "Sequential Server"
    enabled: true
    host: "192.168.1.51"
    user: "admin"
    parallel: false
    shutdown_command: "shutdown -h now"
  - name: "Parallel Server 2"
    enabled: true
    host: "192.168.1.52"
    user: "admin"
    parallel: true
    shutdown_command: "shutdown -h now"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert len(config.remote_servers) == 3
        assert config.remote_servers[0].parallel is True
        assert config.remote_servers[1].parallel is False
        assert config.remote_servers[2].parallel is True


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


class TestConfigParsingEdgeCases:
    """Test edge cases in configuration parsing."""

    @pytest.mark.unit
    def test_partial_ups_config_preserves_defaults(self, temp_config_file):
        """Test that partial UPS config preserves default values."""
        config_data = """
ups:
  name: "CustomUPS@192.168.1.1"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.ups.name == "CustomUPS@192.168.1.1"
        assert config.ups.check_interval == 1  # default preserved
        assert config.ups.max_stale_data_tolerance == 3  # default preserved

    @pytest.mark.unit
    def test_partial_triggers_config_preserves_defaults(self, temp_config_file):
        """Test that partial triggers config preserves default values."""
        config_data = """
triggers:
  low_battery_threshold: 15
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.triggers.low_battery_threshold == 15
        assert config.triggers.critical_runtime_threshold == 600  # default
        assert config.triggers.depletion.window == 300  # default
        assert config.triggers.extended_time.enabled is True  # default

    @pytest.mark.unit
    def test_partial_depletion_config(self, temp_config_file):
        """Test partial depletion configuration."""
        config_data = """
triggers:
  depletion:
    critical_rate: 20.0
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.triggers.depletion.critical_rate == 20.0
        assert config.triggers.depletion.window == 300  # default
        assert config.triggers.depletion.grace_period == 90  # default

    @pytest.mark.unit
    def test_null_logging_file(self, temp_config_file):
        """Test null/None value for logging file."""
        config_data = """
logging:
  file: null
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.logging.file is None

    @pytest.mark.unit
    def test_empty_string_logging_file(self, temp_config_file):
        """Test empty string for logging file (should preserve empty)."""
        config_data = """
logging:
  file: ""
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.logging.file == ""

    @pytest.mark.unit
    def test_notifications_urls_without_discord(self, temp_config_file):
        """Test modern notifications config without legacy Discord."""
        config_data = """
notifications:
  title: "UPS Alert"
  urls:
    - "slack://token/channel"
    - "telegram://bot_token/chat_id"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert len(config.notifications.urls) == 2
        assert "slack://" in config.notifications.urls[0]
        assert "telegram://" in config.notifications.urls[1]
        assert config.notifications.title == "UPS Alert"

    @pytest.mark.unit
    def test_notifications_with_both_urls_and_legacy_discord(self, temp_config_file):
        """Test that both URLs and legacy Discord can coexist."""
        config_data = f"""
notifications:
  urls:
    - "{TEST_SLACK_APPRISE_URL}"
  discord:
    webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is True
        assert len(config.notifications.urls) == 2
        # Discord should be first (inserted at position 0)
        assert "discord://" in config.notifications.urls[0]
        assert "slack://" in config.notifications.urls[1]

    @pytest.mark.unit
    def test_notifications_empty_urls_disables(self, temp_config_file):
        """Test that empty URLs list disables notifications."""
        config_data = """
notifications:
  title: "Test"
  urls: []
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.enabled is False
        assert config.notifications.urls == []

    @pytest.mark.unit
    def test_containers_legacy_docker_section(self, temp_config_file):
        """Test legacy 'docker' section is parsed correctly."""
        config_data = """
docker:
  enabled: true
  stop_timeout: 45
  compose_files:
    - "/path/to/compose.yml"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.containers.enabled is True
        assert config.containers.runtime == "docker"  # Legacy assumes docker
        assert config.containers.stop_timeout == 45
        assert len(config.containers.compose_files) == 1

    @pytest.mark.unit
    def test_containers_new_format_overrides_legacy(self, temp_config_file):
        """Test that new 'containers' section is preferred over 'docker'."""
        config_data = """
containers:
  enabled: true
  runtime: "podman"
  stop_timeout: 90

docker:
  enabled: false
  stop_timeout: 30
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        # 'containers' section should take precedence
        assert config.containers.enabled is True
        assert config.containers.runtime == "podman"
        assert config.containers.stop_timeout == 90

    @pytest.mark.unit
    def test_remote_server_minimal_config(self, temp_config_file):
        """Test remote server with minimal required fields."""
        config_data = """
remote_servers:
  - host: "192.168.1.50"
    user: "root"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        server = config.remote_servers[0]
        assert server.host == "192.168.1.50"
        assert server.user == "root"
        assert server.name == ""  # default
        assert server.enabled is False  # default
        assert server.connect_timeout == 10  # default
        assert server.command_timeout == 30  # default
        assert server.shutdown_command == "sudo shutdown -h now"  # default
        assert server.ssh_options == []  # default
        assert server.pre_shutdown_commands == []  # default
        assert server.parallel is True  # default

    @pytest.mark.unit
    def test_filesystems_sync_disabled(self, temp_config_file):
        """Test disabling filesystem sync."""
        config_data = """
filesystems:
  sync_enabled: false
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.filesystems.sync_enabled is False

    @pytest.mark.unit
    def test_unmount_without_mounts_list(self, temp_config_file):
        """Test unmount enabled but no mounts specified."""
        config_data = """
filesystems:
  unmount:
    enabled: true
    timeout: 30
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.filesystems.unmount.enabled is True
        assert config.filesystems.unmount.timeout == 30
        assert config.filesystems.unmount.mounts == []

    @pytest.mark.unit
    def test_local_shutdown_custom_command(self, temp_config_file):
        """Test custom local shutdown command."""
        config_data = """
local_shutdown:
  enabled: true
  command: "poweroff -f"
  message: "Emergency UPS shutdown"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.local_shutdown.enabled is True
        assert config.local_shutdown.command == "poweroff -f"
        assert config.local_shutdown.message == "Emergency UPS shutdown"

    @pytest.mark.unit
    def test_local_shutdown_disabled(self, temp_config_file):
        """Test disabling local shutdown."""
        config_data = """
local_shutdown:
  enabled: false
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.local_shutdown.enabled is False

    @pytest.mark.unit
    def test_virtual_machines_config(self, temp_config_file):
        """Test virtual machines configuration."""
        config_data = """
virtual_machines:
  enabled: true
  max_wait: 120
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.virtual_machines.enabled is True
        assert config.virtual_machines.max_wait == 120

    @pytest.mark.unit
    def test_notifications_timeout_from_legacy_discord(self, temp_config_file):
        """Test that timeout is read from legacy Discord config."""
        config_data = f"""
notifications:
  discord:
    webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
    timeout: 20
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.notifications.timeout == 20

    @pytest.mark.unit
    def test_extended_time_disabled(self, temp_config_file):
        """Test disabling extended time trigger."""
        config_data = """
triggers:
  extended_time:
    enabled: false
    threshold: 1800
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        assert config.triggers.extended_time.enabled is False
        assert config.triggers.extended_time.threshold == 1800

    @pytest.mark.unit
    def test_duplicate_discord_urls_deduplicated(self, temp_config_file):
        """Test that duplicate Discord URLs in different locations are not duplicated."""
        config_data = f"""
notifications:
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
  discord:
    webhook_url: "{TEST_DISCORD_WEBHOOK_URL}"
"""
        temp_config_file.write_text(config_data)
        config = ConfigLoader.load(str(temp_config_file))

        # Should only have one URL (deduplication logic)
        assert len(config.notifications.urls) == 1
        assert "discord://" in config.notifications.urls[0]
