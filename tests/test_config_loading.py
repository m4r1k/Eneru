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
    RedundancyGroupConfig,
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


class TestRedundancyGroupLoading:
    """Tests for the redundancy_groups YAML section."""

    @pytest.mark.unit
    def test_default_no_redundancy_groups(self, default_config):
        """Default ``Config`` exposes an empty ``redundancy_groups`` list."""
        assert default_config.redundancy_groups == []

    @pytest.mark.unit
    def test_load_minimal_redundancy_group(self, temp_config_file):
        """Minimal redundancy group parses with documented defaults."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources:
      - "UPS-A@10.0.0.1"
      - "UPS-B@10.0.0.2"
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert len(config.redundancy_groups) == 1
        rg = config.redundancy_groups[0]
        assert isinstance(rg, RedundancyGroupConfig)
        assert rg.name == "rack-1"
        assert rg.ups_sources == ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
        assert rg.min_healthy == 1
        assert rg.degraded_counts_as == "healthy"
        assert rg.unknown_counts_as == "critical"
        assert rg.is_local is False
        assert rg.remote_servers == []
        assert rg.virtual_machines.enabled is False

    @pytest.mark.unit
    def test_load_redundancy_group_with_overrides(self, temp_config_file):
        """All redundancy-group fields round-trip through the loader."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
redundancy_groups:
  - name: "triple-feed"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2", "UPS-C@10.0.0.3"]
    min_healthy: 2
    degraded_counts_as: "critical"
    unknown_counts_as: "degraded"
    is_local: true
    triggers:
      low_battery_threshold: 25
    remote_servers:
      - name: "Switch"
        enabled: true
        host: "10.0.0.50"
        user: "admin"
    virtual_machines:
      enabled: true
      max_wait: 45
    containers:
      enabled: true
      runtime: "podman"
    filesystems:
      sync_enabled: true
      unmount:
        enabled: true
        mounts:
          - "/mnt/data"
""")
        config = ConfigLoader.load(str(temp_config_file))
        rg = config.redundancy_groups[0]
        assert rg.min_healthy == 2
        assert rg.degraded_counts_as == "critical"
        assert rg.unknown_counts_as == "degraded"
        assert rg.is_local is True
        assert rg.triggers.low_battery_threshold == 25
        # Inherited from global defaults
        assert rg.triggers.critical_runtime_threshold == 600
        assert len(rg.remote_servers) == 1
        assert rg.remote_servers[0].host == "10.0.0.50"
        assert rg.virtual_machines.enabled is True
        assert rg.virtual_machines.max_wait == 45
        assert rg.containers.runtime == "podman"
        assert rg.filesystems.unmount.enabled is True
        assert rg.filesystems.unmount.mounts == [{"path": "/mnt/data", "options": ""}]

    @pytest.mark.unit
    def test_load_multiple_redundancy_groups(self, temp_config_file):
        """A config can declare multiple redundancy groups."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
  - name: "UPS-D@10.0.0.4"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
  - name: "rack-2"
    ups_sources: ["UPS-C@10.0.0.3", "UPS-D@10.0.0.4"]
    min_healthy: 1
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert len(config.redundancy_groups) == 2
        assert {g.name for g in config.redundancy_groups} == {"rack-1", "rack-2"}

    @pytest.mark.unit
    def test_load_redundancy_group_inherits_global_triggers(self, temp_config_file):
        """When ``triggers:`` is omitted, the group inherits global triggers."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
triggers:
  low_battery_threshold: 30
  critical_runtime_threshold: 1200
redundancy_groups:
  - name: "inherits"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        config = ConfigLoader.load(str(temp_config_file))
        rg = config.redundancy_groups[0]
        assert rg.triggers.low_battery_threshold == 30
        assert rg.triggers.critical_runtime_threshold == 1200

    @pytest.mark.unit
    def test_load_redundancy_group_no_section(self, temp_config_file):
        """Configs without a ``redundancy_groups`` key still load cleanly."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert config.redundancy_groups == []

    @pytest.mark.unit
    def test_load_redundancy_group_preserves_remote_server_ordering(self, temp_config_file):
        """``remote_servers`` ordering and ``shutdown_order`` round-trip."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rg"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    remote_servers:
      - name: "first"
        enabled: true
        host: "10.0.0.10"
        user: "root"
        shutdown_order: 1
      - name: "second"
        enabled: true
        host: "10.0.0.11"
        user: "root"
        shutdown_order: 2
""")
        config = ConfigLoader.load(str(temp_config_file))
        servers = config.redundancy_groups[0].remote_servers
        assert [s.name for s in servers] == ["first", "second"]
        assert [s.shutdown_order for s in servers] == [1, 2]

    @pytest.mark.unit
    def test_load_redundancy_group_skips_non_dict_entries(self, temp_config_file):
        """Malformed YAML entries (e.g., bare strings) are skipped silently."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - "bare-string"
  - name: "good"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert len(config.redundancy_groups) == 1
        assert config.redundancy_groups[0].name == "good"

    @pytest.mark.unit
    def test_load_redundancy_group_empty_section_yields_empty_list(self, temp_config_file):
        """An empty (or null) ``redundancy_groups:`` block parses as ``[]``."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
redundancy_groups:
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert config.redundancy_groups == []

    @pytest.mark.unit
    def test_load_redundancy_group_string_coerced(self, temp_config_file):
        """``name`` and ``ups_sources`` entries are coerced to strings."""
        temp_config_file.write_text("""
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: 12345
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        config = ConfigLoader.load(str(temp_config_file))
        assert config.redundancy_groups[0].name == "12345"
        assert all(isinstance(s, str)
                   for s in config.redundancy_groups[0].ups_sources)


