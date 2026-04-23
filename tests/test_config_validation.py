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


class TestNotificationsSuppressValidation:
    """Issue #27 / B3: per-event notification suppression with safety blocklist."""

    def _errors(self, messages):
        return [m for m in messages if m.startswith("ERROR:")]

    @pytest.mark.unit
    def test_default_suppress_is_empty_and_validates(self, minimal_config):
        # No suppress entries -> no errors, behaviour unchanged from rc5.
        assert minimal_config.notifications.suppress == []
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("suppress" in e for e in errors)

    @pytest.mark.unit
    def test_suppress_accepts_known_event_names(self, minimal_config):
        minimal_config.notifications.suppress = [
            "AVR_BOOST_ACTIVE", "AVR_TRIM_ACTIVE",
            "VOLTAGE_NORMALIZED", "VOLTAGE_FLAP_SUPPRESSED",
        ]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("suppress" in e for e in errors)

    @pytest.mark.unit
    def test_suppress_rejects_safety_critical_events(self, minimal_config):
        minimal_config.notifications.suppress = [
            "OVER_VOLTAGE_DETECTED", "AVR_BOOST_ACTIVE",
        ]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("safety-critical" in e for e in errors)
        assert any("OVER_VOLTAGE_DETECTED" in e for e in errors)

    @pytest.mark.unit
    @pytest.mark.parametrize("ev", [
        "OVER_VOLTAGE_DETECTED", "BROWNOUT_DETECTED", "OVERLOAD_ACTIVE",
        "BYPASS_MODE_ACTIVE", "ON_BATTERY", "CONNECTION_LOST",
    ])
    def test_each_safety_critical_event_is_blocked(self, minimal_config, ev):
        minimal_config.notifications.suppress = [ev]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("safety-critical" in e for e in errors), \
            f"Expected {ev} to be rejected as safety-critical"

    @pytest.mark.unit
    def test_suppress_rejects_shutdown_prefix(self, minimal_config):
        # Anything starting with SHUTDOWN is dynamically blocked.
        minimal_config.notifications.suppress = ["SHUTDOWN_INITIATED"]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("safety-critical" in e for e in errors)

    @pytest.mark.unit
    def test_suppress_rejects_unknown_event_names(self, minimal_config):
        # Typo-catching: not in SUPPRESSIBLE_EVENTS, not safety-critical.
        minimal_config.notifications.suppress = ["AVRR_BOOST_ACTIVE"]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("unknown event names" in e for e in errors)
        assert any("AVRR_BOOST_ACTIVE" in e for e in errors)

    @pytest.mark.unit
    def test_suppress_normalizes_case(self, minimal_config):
        # Lowercase entries are valid (we upper-case before checking).
        minimal_config.notifications.suppress = ["avr_boost_active"]
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("suppress" in e for e in errors)


class TestVoltageHysteresisValidation:
    """Issue #27 / B2: notifications.voltage_hysteresis_seconds validation."""

    def _errors(self, messages):
        return [m for m in messages if m.startswith("ERROR:")]

    @pytest.mark.unit
    def test_default_value_is_30(self, minimal_config):
        assert minimal_config.notifications.voltage_hysteresis_seconds == 30
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("voltage_hysteresis_seconds" in e for e in errors)

    @pytest.mark.unit
    def test_zero_is_accepted_and_means_immediate(self, minimal_config):
        minimal_config.notifications.voltage_hysteresis_seconds = 0
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("voltage_hysteresis_seconds" in e for e in errors)

    @pytest.mark.unit
    def test_negative_value_rejected(self, minimal_config):
        minimal_config.notifications.voltage_hysteresis_seconds = -5
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_hysteresis_seconds" in e for e in errors)

    @pytest.mark.unit
    def test_non_integer_value_rejected(self, minimal_config):
        minimal_config.notifications.voltage_hysteresis_seconds = "thirty"
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_hysteresis_seconds" in e for e in errors)

    @pytest.mark.unit
    def test_very_long_dwell_warns(self, minimal_config):
        minimal_config.notifications.voltage_hysteresis_seconds = 900
        messages = ConfigLoader.validate_config(minimal_config)
        warnings = [m for m in messages if m.startswith("WARNING:")]
        assert any("voltage_hysteresis_seconds" in w for w in warnings)


class TestVoltageSensitivityValidation:
    """Issue #4 / v5.1.2: triggers.voltage_sensitivity strict-enum validation."""

    def _errors(self, messages):
        return [m for m in messages if m.startswith("ERROR:")]

    @pytest.mark.unit
    @pytest.mark.parametrize("preset", ["tight", "normal", "loose"])
    def test_known_presets_accepted(self, minimal_config, preset):
        minimal_config.triggers.voltage_sensitivity = preset
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("voltage_sensitivity" in e for e in errors)

    @pytest.mark.unit
    def test_default_is_normal(self, minimal_config):
        assert minimal_config.triggers.voltage_sensitivity == "normal"
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert not any("voltage_sensitivity" in e for e in errors)

    @pytest.mark.unit
    def test_unknown_preset_rejected(self, minimal_config):
        minimal_config.triggers.voltage_sensitivity = "bogus"
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_sensitivity" in e and "bogus" in e for e in errors)

    @pytest.mark.unit
    def test_typo_with_capital_rejected(self, minimal_config):
        # Strict enum -- "Normal" (capitalised) is NOT accepted. Common
        # YAML-fingers typo; we want a hard error rather than a silent
        # fallback to default.
        minimal_config.triggers.voltage_sensitivity = "Normal"
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_sensitivity" in e for e in errors)

    @pytest.mark.unit
    def test_empty_string_rejected(self, minimal_config):
        minimal_config.triggers.voltage_sensitivity = ""
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_sensitivity" in e for e in errors)

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [
        ["tight"],          # list literal in YAML
        {"value": "tight"},  # mapping in YAML
        42,                 # int (also unhashable-for-our-purposes if we ever add lists)
        None,               # YAML null
    ])
    def test_non_string_value_rejected_without_typeerror(self, minimal_config, bad):
        # Cubic P2: a malformed YAML like `voltage_sensitivity: [tight]`
        # parses as a list -- the membership check `value not in ...`
        # would TypeError on unhashable inputs and bypass the validator.
        # Type-check guards both the validator and the mixin's enum lookup.
        minimal_config.triggers.voltage_sensitivity = bad
        errors = self._errors(ConfigLoader.validate_config(minimal_config))
        assert any("voltage_sensitivity" in e for e in errors), (
            f"expected validator to reject {bad!r} cleanly, got {errors}"
        )

    @pytest.mark.unit
    def test_yaml_round_trip_sets_explicit_flag(self, tmp_path):
        # The mixin uses voltage_sensitivity_explicit to suppress the
        # one-time migration warning. The flag must round-trip from YAML:
        # absent in file -> False; present (any value) -> True.
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text(
            "triggers:\n  voltage_sensitivity: loose\n"
        )
        cfg = ConfigLoader.load(str(explicit))
        assert cfg.triggers.voltage_sensitivity == "loose"
        assert cfg.triggers.voltage_sensitivity_explicit is True

        absent = tmp_path / "absent.yaml"
        absent.write_text("triggers:\n  low_battery_threshold: 25\n")
        cfg2 = ConfigLoader.load(str(absent))
        assert cfg2.triggers.voltage_sensitivity == "normal"
        assert cfg2.triggers.voltage_sensitivity_explicit is False

    @pytest.mark.unit
    def test_per_ups_validation_rejects_bogus(self, tmp_path):
        # Multi-UPS path: each per-UPS triggers block is validated.
        cfg_path = tmp_path / "multi.yaml"
        cfg_path.write_text(
            "ups:\n"
            "  - name: 'UPS-A@10.0.0.1'\n"
            "    triggers:\n"
            "      voltage_sensitivity: bogus\n"
            "  - name: 'UPS-B@10.0.0.2'\n"
            "    triggers:\n"
            "      voltage_sensitivity: tight\n"
        )
        cfg = ConfigLoader.load(str(cfg_path))
        errors = self._errors(ConfigLoader.validate_config(cfg))
        assert any("voltage_sensitivity" in e and "bogus" in e for e in errors)
        # Valid sibling UPS is not flagged.
        assert not any("UPS-B" in e and "voltage_sensitivity" in e for e in errors)


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


def _errors(messages):
    return [m for m in messages if m.startswith("ERROR")]


def _warnings(messages):
    return [m for m in messages if m.startswith("WARNING")]


class TestRedundancyGroupValidation:
    """Cross-field validation for the ``redundancy_groups`` section."""

    def _write(self, tmp_path, body: str):
        path = tmp_path / "config.yaml"
        path.write_text(body)
        return ConfigLoader.load(str(path))

    @pytest.mark.unit
    def test_baseline_valid_redundancy_group(self, tmp_path):
        """A well-formed dual-PSU group passes validation."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        assert _errors(ConfigLoader.validate_config(config)) == []

    @pytest.mark.unit
    def test_min_healthy_zero_rejected(self, tmp_path):
        """``min_healthy=0`` is rejected with an explanation."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    min_healthy: 0
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("min_healthy must be >= 1" in m for m in errors)

    @pytest.mark.unit
    def test_min_healthy_negative_rejected(self, tmp_path):
        """Negative ``min_healthy`` is rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    min_healthy: -1
""")
        assert _errors(ConfigLoader.validate_config(config))

    @pytest.mark.unit
    def test_min_healthy_exceeds_sources_rejected(self, tmp_path):
        """``min_healthy`` > number of sources is impossible to satisfy."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    min_healthy: 3
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("exceeds the number of UPS sources" in m for m in errors)

    @pytest.mark.unit
    def test_min_healthy_equals_sources_warned(self, tmp_path):
        """``min_healthy == len(ups_sources)`` is allowed but warned about."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    min_healthy: 2
""")
        msgs = ConfigLoader.validate_config(config)
        assert _errors(msgs) == []
        assert any(m.startswith("WARNING") and "no redundancy" in m for m in msgs)

    @pytest.mark.unit
    def test_min_healthy_non_integer_rejected(self, tmp_path):
        """Non-integer ``min_healthy`` (e.g., a string) is rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    min_healthy: "many"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("min_healthy must be an integer" in m for m in errors)

    @pytest.mark.unit
    def test_unknown_ups_source_rejected(self, tmp_path):
        """References to UPS names not declared in ``ups:`` are rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-MISSING@10.0.0.99"]
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("UPS-MISSING@10.0.0.99" in m for m in errors)

    @pytest.mark.unit
    def test_duplicate_ups_source_rejected(self, tmp_path):
        """Listing the same UPS twice in ``ups_sources`` is rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-A@10.0.0.1"]
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("duplicate UPS source" in m for m in errors)

    @pytest.mark.unit
    def test_empty_ups_sources_rejected(self, tmp_path):
        """A redundancy group with no UPS sources is rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
redundancy_groups:
  - name: "empty"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("'ups_sources' is empty" in m for m in errors)

    @pytest.mark.unit
    def test_missing_group_name_rejected(self, tmp_path):
        """Each redundancy group must have a non-empty ``name``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("missing 'name'" in m for m in errors)

    @pytest.mark.unit
    def test_duplicate_group_names_rejected(self, tmp_path):
        """Two redundancy groups cannot share the same ``name``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
  - name: "UPS-D@10.0.0.4"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
  - name: "rack"
    ups_sources: ["UPS-C@10.0.0.3", "UPS-D@10.0.0.4"]
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("Duplicate redundancy group name" in m for m in errors)

    @pytest.mark.unit
    def test_invalid_degraded_counts_as_rejected(self, tmp_path):
        """``degraded_counts_as`` must be 'healthy' or 'critical'."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    degraded_counts_as: "fine"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("degraded_counts_as must be" in m for m in errors)

    @pytest.mark.unit
    def test_invalid_unknown_counts_as_rejected(self, tmp_path):
        """``unknown_counts_as`` must be one of three documented values."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    unknown_counts_as: "ignore"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("unknown_counts_as must be" in m for m in errors)

    @pytest.mark.unit
    def test_non_local_with_vms_rejected(self, tmp_path):
        """A non-``is_local`` group cannot declare ``virtual_machines.enabled``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    virtual_machines:
      enabled: true
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("virtual_machines" in m and "is_local" in m for m in errors)

    @pytest.mark.unit
    def test_non_local_with_containers_rejected(self, tmp_path):
        """A non-``is_local`` group cannot declare ``containers.enabled``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    containers:
      enabled: true
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("containers" in m and "is_local" in m for m in errors)

    @pytest.mark.unit
    def test_non_local_with_filesystems_rejected(self, tmp_path):
        """A non-``is_local`` group cannot enable ``filesystems.unmount``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    filesystems:
      unmount:
        enabled: true
        mounts: ["/mnt/x"]
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("filesystem unmount" in m and "is_local" in m for m in errors)

    @pytest.mark.unit
    def test_local_redundancy_with_vms_allowed(self, tmp_path):
        """An ``is_local: true`` redundancy group may declare local resources."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "local-rack"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    is_local: true
    virtual_machines:
      enabled: true
""")
        assert _errors(ConfigLoader.validate_config(config)) == []

    @pytest.mark.unit
    def test_two_local_redundancy_groups_rejected(self, tmp_path):
        """At most one redundancy group can be ``is_local``."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
  - name: "UPS-D@10.0.0.4"
redundancy_groups:
  - name: "local-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    is_local: true
  - name: "local-2"
    ups_sources: ["UPS-C@10.0.0.3", "UPS-D@10.0.0.4"]
    is_local: true
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("Multiple groups marked as is_local" in m for m in errors)

    @pytest.mark.unit
    def test_one_local_ups_plus_one_local_redundancy_rejected(self, tmp_path):
        """An ``is_local`` UPS group plus an ``is_local`` redundancy group is rejected."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
    is_local: true
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
redundancy_groups:
  - name: "local-rg"
    ups_sources: ["UPS-B@10.0.0.2", "UPS-C@10.0.0.3"]
    is_local: true
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("Multiple groups marked as is_local" in m for m in errors)

    @pytest.mark.unit
    def test_remote_server_in_both_tiers_rejected(self, tmp_path):
        """A remote server (host+user) cannot belong to both tiers at once."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
    remote_servers:
      - name: "shared-srv"
        enabled: true
        host: "10.0.0.50"
        user: "root"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rg"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    remote_servers:
      - name: "shared-srv"
        enabled: true
        host: "10.0.0.50"
        user: "root"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("owned by both" in m and "shared-srv" in m for m in errors)

    @pytest.mark.unit
    def test_remote_server_in_two_redundancy_groups_rejected(self, tmp_path):
        """The same remote server cannot live in two redundancy groups."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
  - name: "UPS-C@10.0.0.3"
  - name: "UPS-D@10.0.0.4"
redundancy_groups:
  - name: "rg-1"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
    remote_servers:
      - name: "shared"
        enabled: true
        host: "10.0.0.50"
        user: "root"
  - name: "rg-2"
    ups_sources: ["UPS-C@10.0.0.3", "UPS-D@10.0.0.4"]
    remote_servers:
      - name: "shared"
        enabled: true
        host: "10.0.0.50"
        user: "root"
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("appears in two redundancy groups" in m for m in errors)

    @pytest.mark.unit
    def test_ups_in_both_independent_and_redundancy_allowed(self, tmp_path):
        """A UPS may legitimately appear in both an independent and a redundancy group."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
  - name: "UPS-B@10.0.0.2"
redundancy_groups:
  - name: "rg"
    ups_sources: ["UPS-A@10.0.0.1", "UPS-B@10.0.0.2"]
""")
        assert _errors(ConfigLoader.validate_config(config)) == []

    @pytest.mark.unit
    def test_validation_runs_without_redundancy_section(self, default_config):
        """Configs with no ``redundancy_groups`` section never reference them."""
        msgs = ConfigLoader.validate_config(default_config)
        assert not any("redundancy" in m.lower() for m in msgs)


