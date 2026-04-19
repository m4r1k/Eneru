"""Tests for cross-field config validation and parsing edge cases."""

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


class TestConfigValidation:
    """Test configuration validation."""

    @pytest.mark.unit
    def test_validate_config_with_modern_discord(self, full_config):
        """Test validation with modern discord:// URL format."""
        messages = ConfigLoader.validate_config(full_config)
        # Modern discord:// URLs should not trigger legacy warning
        assert not any("Legacy" in msg for msg in messages)

    @pytest.mark.unit
    def test_validate_config_with_legacy_discord(self, full_config):
        """Test validation returns info about legacy Discord webhook_url."""
        # Simulate raw config data with legacy discord.webhook_url
        raw_data = {
            'notifications': {
                'discord': {
                    'webhook_url': 'https://discord.com/api/webhooks/123/abc'
                }
            }
        }
        messages = ConfigLoader.validate_config(full_config, raw_data)
        # Should have message about legacy Discord webhook_url
        assert any("Legacy Discord webhook_url" in msg for msg in messages)

    @pytest.mark.unit
    def test_validate_config_with_toplevel_legacy_discord(self, full_config):
        """Test validation detects top-level legacy discord config."""
        # Simulate raw config data with top-level legacy discord section
        raw_data = {
            'discord': {
                'webhook_url': 'https://discord.com/api/webhooks/456/def'
            }
        }
        messages = ConfigLoader.validate_config(full_config, raw_data)
        # Should have message about legacy Discord webhook_url
        assert any("Legacy Discord webhook_url" in msg for msg in messages)

    @pytest.mark.unit
    def test_validate_config_empty_notifications(self, minimal_config):
        """Test validation with no notifications configured."""
        messages = ConfigLoader.validate_config(minimal_config)
        # Should not have warnings about missing Apprise
        assert not any("WARNING" in msg for msg in messages)

    @pytest.mark.unit
    def test_validate_invalid_trigger_on(self, minimal_config):
        """Invalid trigger_on value produces ERROR."""
        minimal_config.local_shutdown.trigger_on = "all"
        messages = ConfigLoader.validate_config(minimal_config)
        errors = [m for m in messages if m.startswith("ERROR")]
        assert any("trigger_on" in m and "'all'" in m for m in errors)

    @pytest.mark.unit
    def test_validate_valid_trigger_on_values(self, minimal_config):
        """Valid trigger_on values ('any', 'none') produce no error."""
        for value in ("any", "none"):
            minimal_config.local_shutdown.trigger_on = value
            messages = ConfigLoader.validate_config(minimal_config)
            assert not any("trigger_on" in m for m in messages)


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
        assert server.parallel is None  # default (unset; behaves as parallel batch)
        assert server.shutdown_safety_margin == 60  # default

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


