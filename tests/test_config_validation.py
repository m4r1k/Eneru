"""Tests for cross-field config validation and parsing edge cases."""

import pytest
import yaml
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from eneru import (
    Config,
    ConfigLoader,
    UPSConfig,
    UPSGroupConfig,
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

    # --- C2: shutdown-trigger numeric fields must be validated at load ---
    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["20", "thirty", None, [20], True])
    def test_validate_rejects_nonint_low_battery_threshold(self, minimal_config, bad):
        """Regression (C2): a non-int low_battery_threshold (most commonly a
        quoted '20' from a templating tool) must error at load. Otherwise it
        survives parse as a str and raises TypeError (int < str) on the first
        on-battery poll, killing the monitor loop when a shutdown is due."""
        minimal_config.triggers.low_battery_threshold = bad
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("low_battery_threshold" in m for m in errors), (
            f"low_battery_threshold={bad!r} should ERROR; got {errors!r}")

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [-1, 101, 150])
    def test_validate_rejects_out_of_range_low_battery_threshold(
            self, minimal_config, bad):
        minimal_config.triggers.low_battery_threshold = bad
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("low_battery_threshold" in m for m in errors)

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["600", None, True, [1]])
    def test_validate_rejects_nonint_critical_runtime_threshold(
            self, minimal_config, bad):
        minimal_config.triggers.critical_runtime_threshold = bad
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("critical_runtime_threshold" in m for m in errors)

    @pytest.mark.unit
    def test_validate_rejects_nonnumeric_depletion_and_extended(
            self, minimal_config):
        """depletion.* and extended_time.threshold feed comparisons too."""
        minimal_config.triggers.depletion.critical_rate = "fast"
        minimal_config.triggers.depletion.window = "300"
        minimal_config.triggers.depletion.grace_period = None
        minimal_config.triggers.extended_time.threshold = "900"
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("depletion.critical_rate" in m for m in errors)
        assert any("depletion.window" in m for m in errors)
        assert any("depletion.grace_period" in m for m in errors)
        assert any("extended_time.threshold" in m for m in errors)

    @pytest.mark.unit
    def test_validate_accepts_valid_trigger_numbers(self, minimal_config):
        """Defaults (incl. float critical_rate) produce no trigger-number ERROR."""
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any(
            m.startswith("ERROR") and (
                "low_battery_threshold" in m
                or "critical_runtime_threshold" in m
                or "depletion." in m
                or "extended_time.threshold" in m)
            for m in messages)

    # --- C3: local_shutdown.command must be a non-empty string when enabled ---
    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_validate_rejects_empty_local_shutdown_command(
            self, minimal_config, bad):
        """Regression (C3): null/empty command must error at load, not let
        None.split()/run_command([]) silently skip the host poweroff after the
        drain phases already ran."""
        minimal_config.local_shutdown.enabled = True
        minimal_config.local_shutdown.command = bad
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("local_shutdown.command" in m for m in errors), (
            f"command={bad!r} should ERROR; got {errors!r}")

    @pytest.mark.unit
    def test_validate_accepts_valid_local_shutdown_command(self, minimal_config):
        minimal_config.local_shutdown.enabled = True
        minimal_config.local_shutdown.command = "shutdown -h now"
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any("local_shutdown.command" in m for m in messages)

    @pytest.mark.unit
    def test_validate_ignores_empty_command_when_disabled(self, minimal_config):
        """An empty command is harmless when local shutdown is disabled."""
        minimal_config.local_shutdown.enabled = False
        minimal_config.local_shutdown.command = ""
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any("local_shutdown.command" in m for m in messages)

    @pytest.mark.unit
    def test_validate_new_observability_defaults(self, minimal_config):
        """v5.3 observability defaults are safe and valid."""
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any("api.port" in m for m in messages)
        assert minimal_config.api.enabled is False
        assert minimal_config.api.bind == "127.0.0.1"
        assert minimal_config.api.port == 9191
        assert minimal_config.prometheus.enabled is True
        assert minimal_config.remote_health.enabled is True
        assert minimal_config.remote_health.probe_command == "true"

    @pytest.mark.unit
    def test_validate_rejects_unsafe_remote_health_probe(self, minimal_config):
        """Healthchecks must not be configured to send shutdown commands."""
        minimal_config.remote_health.probe_command = "sudo shutdown -h now"
        messages = ConfigLoader.validate_config(minimal_config)
        errors = [m for m in messages if m.startswith("ERROR")]
        assert any("probe_command" in m for m in errors)

    @pytest.mark.unit
    def test_validate_mqtt_requires_broker_when_enabled(self, minimal_config):
        minimal_config.mqtt.enabled = True
        minimal_config.mqtt.broker = ""
        messages = ConfigLoader.validate_config(minimal_config)
        errors = [m for m in messages if m.startswith("ERROR")]
        assert any("mqtt.broker" in m for m in errors)

    @pytest.mark.unit
    def test_validate_syslog_facility(self, minimal_config):
        minimal_config.logging.syslog.facility = "not-a-facility"
        messages = ConfigLoader.validate_config(minimal_config)
        errors = [m for m in messages if m.startswith("ERROR")]
        assert any("logging.syslog.facility" in m for m in errors)

    @pytest.mark.unit
    def test_validate_normalizes_syslog_facility_case(self, minimal_config):
        minimal_config.logging.syslog.facility = "LOCAL0"
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any("logging.syslog.facility" in m for m in messages)
        assert minimal_config.logging.syslog.facility == "local0"

    @pytest.mark.unit
    @pytest.mark.parametrize("port", [0, -1, 65536, 100000, "abc", None, True])
    def test_validate_rejects_invalid_syslog_port(self, minimal_config, port):
        """Boundary cases for the syslog port validator (1-65535)."""
        minimal_config.logging.syslog.port = port
        messages = ConfigLoader.validate_config(minimal_config)
        errors = [m for m in messages if m.startswith("ERROR")]
        assert any("logging.syslog.port" in m for m in errors), (
            f"port={port!r} should have produced an ERROR; got {errors!r}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("port", [1, 514, 65535])
    def test_validate_accepts_valid_syslog_port(self, minimal_config, port):
        minimal_config.logging.syslog.port = port
        messages = ConfigLoader.validate_config(minimal_config)
        assert not any(
            "logging.syslog.port" in m and m.startswith("ERROR")
            for m in messages
        )


class TestUnknownKeyValidation:
    """Unknown safety keys are hard errors, with legacy aliases preserved."""

    def _errors(self, raw_data: Mapping[str, Any]) -> list[str]:
        config = ConfigLoader._parse_config(raw_data)
        return [
            m for m in ConfigLoader.validate_config(config, raw_data)
            if m.startswith("ERROR:")
        ]

    @pytest.mark.unit
    def test_behavior_dry_run_typo_is_error_with_hint(self):
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "behavior": {"dry-run": True},
        })
        assert any("behavior.dry-run" in e for e in errors)
        assert any("Did you mean 'dry_run'" in e for e in errors)

    @pytest.mark.unit
    def test_api_typo_is_error_with_hint(self):
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "api": {"prot": 9101},
        })
        assert any("api.prot" in e for e in errors)
        assert any("Did you mean 'port'" in e for e in errors)

    @pytest.mark.unit
    def test_top_level_extended_time_typo_is_error_with_hint(self):
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "triggers": {"exteneded_time": {"enabled": True}},
        })
        assert any("triggers.exteneded_time" in e for e in errors)
        assert any("Did you mean 'extended_time'" in e for e in errors)

    @pytest.mark.unit
    def test_redundancy_group_trigger_typo_is_error(self):
        errors = self._errors({
            "ups": [{"name": "UPS-A"}, {"name": "UPS-B"}],
            "redundancy_groups": [{
                "name": "rack",
                "ups_sources": ["UPS-A", "UPS-B"],
                "triggers": {"critical_runtme_threshold": 1200},
            }],
        })
        assert any("redundancy_groups['rack'].triggers" in e for e in errors)
        assert any("critical_runtime_threshold" in e for e in errors)

    @pytest.mark.unit
    def test_multi_ups_trigger_typo_is_error(self):
        errors = self._errors({
            "ups": [{
                "name": "UPS-A",
                "triggers": {"exteneded_time": {"enabled": True}},
            }],
        })
        assert any("ups['UPS-A'].triggers.exteneded_time" in e for e in errors)
        assert any("Did you mean 'extended_time'" in e for e in errors)

    @pytest.mark.unit
    def test_redundancy_group_depletion_window_is_error(self):
        errors = self._errors({
            "ups": [{"name": "UPS-A"}, {"name": "UPS-B"}],
            "redundancy_groups": [{
                "name": "rack",
                "ups_sources": ["UPS-A", "UPS-B"],
                "triggers": {"depletion": {"window": 60}},
            }],
        })
        assert any("redundancy_groups['rack'].triggers.depletion.window" in e
                   for e in errors)

    @pytest.mark.unit
    def test_non_mapping_trigger_sections_are_ignored(self, default_config):
        raw_data = {
            "ups": [
                {"name": "UPS-A", "triggers": []},
                "not-a-mapping",
            ],
            "triggers": [],
            "redundancy_groups": [
                "not-a-mapping",
                {"name": "rack", "triggers": []},
            ],
        }
        errors = [
            m for m in ConfigLoader.validate_config(default_config, raw_data)
            if m.startswith("ERROR:")
        ]
        assert not any("unknown config key" in e for e in errors)

    @pytest.mark.unit
    def test_legacy_docker_and_discord_configs_are_not_unknown_key_errors(self):
        raw_data = {
            "ups": {"name": "UPS@localhost"},
            "docker": {"enabled": True},
            "discord": {"webhook_url": "https://discord.com/api/webhooks/1/a"},
            "notifications": {
                "discord": {"webhook_url": "https://discord.com/api/webhooks/2/b"},
            },
        }
        errors = self._errors(raw_data)
        assert not any("unknown config key" in e for e in errors)

    @pytest.mark.unit
    def test_top_level_remote_server_typo_is_error(self):
        """v5.4: top-level remote_servers list now validates unknown keys."""
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "remote_servers": [{
                "name": "nas",
                "host": "nas.lan",
                "ssh_keypath": "/missing-underscore",
            }],
        })
        assert any("remote_servers['nas']" in e for e in errors)
        assert any("ssh_keypath" in e for e in errors)

    @pytest.mark.unit
    def test_per_ups_remote_server_typo_is_error(self):
        """Multi-UPS ups[].remote_servers list also validates unknown keys."""
        errors = self._errors({
            "ups": [{
                "name": "UPS-A",
                "remote_servers": [{
                    "name": "nas",
                    "host": "nas.lan",
                    "ssh_kee_path": "/typo",
                }],
            }],
        })
        assert any("ups['UPS-A'].remote_servers['nas']" in e for e in errors)
        assert any("ssh_kee_path" in e for e in errors)

    @pytest.mark.unit
    def test_redundancy_group_remote_server_typo_is_error(self):
        """redundancy_groups[].remote_servers list also validates."""
        errors = self._errors({
            "ups": [{"name": "UPS-A"}, {"name": "UPS-B"}],
            "redundancy_groups": [{
                "name": "rack",
                "ups_sources": ["UPS-A", "UPS-B"],
                "remote_servers": [{
                    "name": "node1",
                    "host": "node1.lan",
                    "ssh_keys": "/wrong",
                }],
            }],
        })
        assert any("redundancy_groups['rack'].remote_servers['node1']" in e for e in errors)
        assert any("ssh_keys" in e for e in errors)

    @pytest.mark.unit
    def test_pre_shutdown_command_typo_is_error(self):
        """Nested pre_shutdown_commands entries also have their keys validated."""
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "remote_servers": [{
                "name": "nas",
                "host": "nas.lan",
                "pre_shutdown_commands": [
                    {"action": "wait", "timeout": 5, "extra_key": "boom"},
                ],
            }],
        })
        assert any("remote_servers['nas'].pre_shutdown_commands[0]" in e for e in errors)
        assert any("extra_key" in e for e in errors)

    @pytest.mark.unit
    def test_remote_servers_non_list_validator_is_a_noop(self, default_config):
        """Validator's defensive guard for malformed remote_servers shape:
        when raw_data has a non-list remote_servers value, the unknown-key
        pass must no-op (the dataclass loader surfaces the type error
        elsewhere; we just don't want the validator to raise on top)."""
        raw_data = {
            "ups": {"name": "UPS@localhost"},
            "remote_servers": "not-a-list",
        }
        # Call validate_config directly with a pre-built default config so we
        # bypass the parser's separate type-checking on this field.
        messages = ConfigLoader.validate_config(default_config, raw_data=raw_data)
        # No remote_servers[...] errors and no exception
        assert not any("remote_servers[" in m for m in messages)

    @pytest.mark.unit
    def test_remote_servers_non_dict_entry_is_skipped(self, default_config):
        """Validator skips non-mapping list entries (e.g. a stray string)."""
        raw_data = {
            "ups": {"name": "UPS@localhost"},
            "remote_servers": ["not-a-mapping", {"name": "ok", "host": "h"}],
        }
        messages = ConfigLoader.validate_config(default_config, raw_data=raw_data)
        # Validator should not crash and should not emit a remote_servers['ok']
        # unknown-key error since 'name' and 'host' are valid keys.
        assert not any("remote_servers['ok']" in m and "unknown" in m.lower()
                       for m in messages)

    @pytest.mark.unit
    def test_yaml_unavailable_falls_back_to_default_config(self, tmp_path, capsys):
        """When PyYAML can't be imported, ConfigLoader.load() must return
        a usable default Config and warn the user — not crash."""
        from eneru import config as cfg_mod
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  name: 'X'\n")
        with patch.object(cfg_mod, "YAML_AVAILABLE", False):
            cfg = ConfigLoader.load(str(config_file))
        assert isinstance(cfg, Config)
        out = capsys.readouterr().out
        assert "PyYAML not installed" in out
        assert "pip install pyyaml" in out

    @pytest.mark.unit
    def test_prometheus_section_with_non_dict_falls_back_to_defaults(self, tmp_path):
        """A `prometheus: true` (non-mapping) value must not crash; defaults apply."""
        from eneru import PrometheusConfig
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@localhost"
prometheus: true
""")
        cfg = ConfigLoader.load(str(config_file))
        # Defaults still in place — no AttributeError on parse.
        assert cfg.prometheus.enabled == PrometheusConfig().enabled

    @pytest.mark.unit
    def test_remote_health_section_with_non_dict_falls_back_to_defaults(self, tmp_path):
        from eneru import RemoteHealthConfig
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@localhost"
remote_health: "yes please"
""")
        cfg = ConfigLoader.load(str(config_file))
        assert cfg.remote_health.enabled == RemoteHealthConfig().enabled
        assert cfg.remote_health.interval == RemoteHealthConfig().interval

    @pytest.mark.unit
    def test_mqtt_section_with_non_dict_falls_back_to_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@localhost"
mqtt: 42
""")
        cfg = ConfigLoader.load(str(config_file))
        assert cfg.mqtt.enabled is False

    @pytest.mark.unit
    def test_pre_shutdown_commands_non_list_is_skipped(self, default_config):
        """A `pre_shutdown_commands: 42` (non-list) on a remote_server
        entry must not crash validation — skip it and move on."""
        raw_data = {
            "ups": {"name": "UPS@localhost"},
            "remote_servers": [{
                "name": "nas",
                "host": "nas.lan",
                "pre_shutdown_commands": 42,  # not a list
            }],
        }
        # No exception, no unrelated errors
        ConfigLoader.validate_config(default_config, raw_data=raw_data)

    @pytest.mark.unit
    def test_pre_shutdown_commands_non_dict_entry_is_skipped(self, default_config):
        """A list whose entries aren't all dicts is partially skipped."""
        raw_data = {
            "ups": {"name": "UPS@localhost"},
            "remote_servers": [{
                "name": "nas",
                "host": "nas.lan",
                "pre_shutdown_commands": [
                    "not-a-dict",  # skipped
                    {"action": "wait", "timeout": 5},  # valid
                ],
            }],
        }
        messages = ConfigLoader.validate_config(default_config, raw_data=raw_data)
        # No errors about pre_shutdown_commands[0] (the string)
        assert not any("pre_shutdown_commands[0]" in m for m in messages)

    @pytest.mark.unit
    def test_logging_format_invalid_value_errors(self):
        """logging.format only accepts 'text' or 'json'."""
        from eneru import LoggingConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS"))],
            logging=LoggingConfig(format="xml"),  # invalid
        )
        messages = ConfigLoader.validate_config(config)
        errors = [m for m in messages if m.startswith("ERROR:")]
        assert any("logging.format must be 'text' or 'json'" in e for e in errors)

    @pytest.mark.unit
    def test_apprise_unavailable_warning_when_notifications_enabled(self):
        """When apprise isn't installed but notifications.enabled=True,
        emit a WARNING (not ERROR) so validate doesn't fail but the
        operator gets a clear pip-install hint."""
        # APPRISE_AVAILABLE lives in eneru.notifications; validate_config
        # imports it inside the function, so patch the source module.
        from eneru import notifications as notif_mod
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS"))],
            notifications=NotificationsConfig(enabled=True, urls=["discord://x/y"]),
        )
        with patch.object(notif_mod, "APPRISE_AVAILABLE", False):
            messages = ConfigLoader.validate_config(config)
        assert any("apprise package not installed" in m for m in messages)
        # And it's a WARNING, not an ERROR
        assert any(m.startswith("WARNING") and "apprise" in m for m in messages)

    @pytest.mark.unit
    def test_remote_server_known_keys_dont_trigger_errors(self):
        """All documented remote_server keys must be accepted."""
        errors = self._errors({
            "ups": {"name": "UPS@localhost"},
            "remote_servers": [{
                "name": "nas",
                "enabled": True,
                "host": "nas.lan",
                "user": "ups",
                "connect_timeout": 5,
                "command_timeout": 30,
                "shutdown_command": "sudo shutdown -h now",
                "ssh_key_path": "/root/.ssh/id_ups",
                "ssh_options": ["-o", "StrictHostKeyChecking=no"],
                "pre_shutdown_commands": [
                    {"action": "wait", "timeout": 5},
                ],
                "parallel": True,
                "shutdown_order": 1,
                "shutdown_safety_margin": 30,
            }],
        })
        # No remote_servers-related unknown-key errors at all.
        assert not any("remote_servers['nas']" in e and "unknown" in e.lower()
                       for e in errors), errors


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
    def test_remote_server_empty_ssh_key_path_rejected(self, tmp_path):
        """ssh_key_path must be useful when provided."""
        config = self._write(tmp_path, """
ups:
  - name: "UPS-A@10.0.0.1"
    remote_servers:
      - name: "nas"
        enabled: true
        host: "10.0.0.10"
        user: "ups"
        ssh_key_path: ""
""")
        errors = _errors(ConfigLoader.validate_config(config))
        assert any("ssh_key_path" in m and "non-empty" in m for m in errors)

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
