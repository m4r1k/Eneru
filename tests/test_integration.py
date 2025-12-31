"""Integration tests for Eneru.

These tests verify that components work together correctly.
They may require mocking external dependencies but test real interactions.
"""

import pytest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from eneru import (
    UPSMonitor,
    ConfigLoader,
    Config,
    MonitorState,
)


class TestConfigToMonitor:
    """Test configuration loading and monitor initialization."""

    @pytest.mark.integration
    def test_full_config_loads_and_initializes(self, tmp_path):
        """Test that a full configuration file can be loaded and used."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"
  check_interval: 2

triggers:
  low_battery_threshold: 25
  critical_runtime_threshold: 900

behavior:
  dry_run: true

logging:
  file: null
  state_file: "{tmp}/state"
  battery_history_file: "{tmp}/history"
  shutdown_flag_file: "{tmp}/flag"

virtual_machines:
  enabled: false

containers:
  enabled: false

local_shutdown:
  enabled: false
""".format(tmp=str(tmp_path)))

        config = ConfigLoader.load(str(config_file))

        assert config.ups.name == "TestUPS@localhost"
        assert config.ups.check_interval == 2
        assert config.triggers.low_battery_threshold == 25
        assert config.behavior.dry_run is True

        # Create monitor with the config
        monitor = UPSMonitor(config)
        assert monitor.config.ups.name == "TestUPS@localhost"

    @pytest.mark.integration
    def test_monitor_initialization_sequence(self, minimal_config, tmp_path):
        """Test that monitor initializes correctly."""
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None

        with patch("eneru.monitor.run_command") as mock_run:
            # Mock upsc responses
            mock_run.return_value = (0, """ups.status: OL CHRG
battery.charge: 100
battery.runtime: 3600
ups.load: 20
input.voltage: 230.5
input.voltage.nominal: 230
input.transfer.low: 180
input.transfer.high: 270
""", "")

            with patch("eneru.monitor.command_exists", return_value=True):
                monitor = UPSMonitor(minimal_config)

                # Initialize without running the main loop
                with patch.object(monitor, "_main_loop"):
                    monitor._initialize()

                    # Verify state is initialized
                    assert monitor.state is not None
                    assert monitor.logger is not None


class TestShutdownSequence:
    """Test the shutdown sequence integration."""

    @pytest.fixture
    def shutdown_monitor(self, minimal_config, tmp_path):
        """Create a monitor configured for shutdown testing."""
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None
        minimal_config.behavior.dry_run = True  # Always dry-run for tests

        # Enable all shutdown components
        minimal_config.virtual_machines.enabled = True
        minimal_config.containers.enabled = True
        minimal_config.containers.runtime = "docker"
        minimal_config.filesystems.sync_enabled = True
        minimal_config.filesystems.unmount.enabled = True
        minimal_config.filesystems.unmount.mounts = [
            {"path": "/mnt/test", "options": ""}
        ]
        minimal_config.local_shutdown.enabled = True

        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._container_runtime = "docker"

        return monitor

    @pytest.mark.integration
    def test_dry_run_shutdown_sequence(self, shutdown_monitor):
        """Test that dry-run shutdown sequence executes without errors."""
        with patch("eneru.monitor.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            with patch("os.sync"):
                # The flag is created at the start of _execute_shutdown_sequence
                shutdown_monitor._execute_shutdown_sequence()

                # In dry-run mode with local_shutdown enabled, the flag is removed at the end
                # So we check that logging happened instead
                assert shutdown_monitor.logger.log.called

                # Check that the shutdown sequence logged the expected messages
                log_calls = [str(call) for call in shutdown_monitor.logger.log.call_args_list]
                log_output = " ".join(log_calls)

                # Verify key shutdown steps were logged
                assert "SHUTDOWN SEQUENCE" in log_output or "DRY-RUN" in log_output

    @pytest.mark.integration
    def test_trigger_immediate_shutdown_sets_flag(self, shutdown_monitor):
        """Test that triggering shutdown sets the flag file."""
        flag_path = shutdown_monitor._shutdown_flag_path

        assert not flag_path.exists()

        with patch("eneru.monitor.run_command", return_value=(0, "", "")):
            with patch("os.sync"):
                # Mock _execute_shutdown_sequence to prevent full execution
                # but still test that _trigger_immediate_shutdown sets the flag
                with patch.object(shutdown_monitor, "_execute_shutdown_sequence") as mock_exec:
                    shutdown_monitor._trigger_immediate_shutdown("Test reason")

                    # Flag should be set before _execute_shutdown_sequence is called
                    assert flag_path.exists()

                    # And the shutdown sequence should have been called
                    mock_exec.assert_called_once()

        # Clean up
        flag_path.unlink(missing_ok=True)

    @pytest.mark.integration
    def test_duplicate_shutdown_prevented(self, shutdown_monitor):
        """Test that shutdown cannot be triggered twice."""
        # Create the flag file first
        shutdown_monitor._shutdown_flag_path.touch()

        with patch.object(shutdown_monitor, "_execute_shutdown_sequence") as mock_exec:
            shutdown_monitor._trigger_immediate_shutdown("Test reason")

            # Should not execute because flag already exists
            mock_exec.assert_not_called()

        # Clean up
        shutdown_monitor._shutdown_flag_path.unlink(missing_ok=True)

    @pytest.mark.integration
    def test_shutdown_flag_file_created_during_sequence(self, shutdown_monitor, tmp_path):
        """Test that the shutdown flag file is created during shutdown sequence."""
        flag_path = shutdown_monitor._shutdown_flag_path

        # Ensure flag doesn't exist initially
        flag_path.unlink(missing_ok=True)
        assert not flag_path.exists()

        flag_was_created = False

        def check_flag_exists(*args, **kwargs):
            nonlocal flag_was_created
            if flag_path.exists():
                flag_was_created = True
            return (0, "", "")

        with patch("eneru.monitor.run_command", side_effect=check_flag_exists):
            with patch("os.sync"):
                shutdown_monitor._execute_shutdown_sequence()

        # The flag should have been created at some point during execution
        assert flag_was_created

    @pytest.mark.integration
    def test_shutdown_logs_dry_run_message(self, shutdown_monitor):
        """Test that dry-run mode is clearly indicated in logs."""
        with patch("eneru.monitor.run_command", return_value=(0, "", "")):
            with patch("os.sync"):
                shutdown_monitor._execute_shutdown_sequence()

        # Check that DRY-RUN was logged
        log_calls = [str(call) for call in shutdown_monitor.logger.log.call_args_list]
        log_output = " ".join(log_calls)

        assert "DRY-RUN" in log_output
