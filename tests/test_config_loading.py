"""Tests for Config defaults and YAML file-loading paths."""

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


