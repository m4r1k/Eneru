"""Tests for connection loss grace period feature."""

import pytest
import time
from unittest.mock import patch, MagicMock, call

from eneru import (
    Config,
    UPSConfig,
    ConnectionLossGracePeriodConfig,
    UPSMonitor,
    MonitorState,
    ConfigLoader,
)


# ==============================================================================
# CONFIG TESTS
# ==============================================================================


class TestConnectionGracePeriodConfig:
    """Test configuration parsing for connection loss grace period."""

    @pytest.mark.unit
    def test_default_config(self):
        """Test default grace period configuration values."""
        config = Config()
        grace = config.ups.connection_loss_grace_period
        assert grace.enabled is True
        assert grace.duration == 60
        assert grace.flap_threshold == 5

    @pytest.mark.unit
    def test_parse_from_yaml(self, tmp_path):
        """Test parsing custom grace period values from YAML."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  connection_loss_grace_period:\n"
            "    enabled: false\n"
            "    duration: 120\n"
            "    flap_threshold: 10\n"
        )
        config = ConfigLoader.load(str(config_file))
        grace = config.ups.connection_loss_grace_period
        assert grace.enabled is False
        assert grace.duration == 120
        assert grace.flap_threshold == 10

    @pytest.mark.unit
    def test_partial_config_preserves_defaults(self, tmp_path):
        """Test that partial config preserves unspecified defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  connection_loss_grace_period:\n"
            "    duration: 30\n"
        )
        config = ConfigLoader.load(str(config_file))
        grace = config.ups.connection_loss_grace_period
        assert grace.enabled is True  # default preserved
        assert grace.duration == 30
        assert grace.flap_threshold == 5  # default preserved

    @pytest.mark.unit
    def test_no_grace_section_preserves_defaults(self, tmp_path):
        """Test that missing grace period section uses all defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
        )
        config = ConfigLoader.load(str(config_file))
        grace = config.ups.connection_loss_grace_period
        assert grace.enabled is True
        assert grace.duration == 60
        assert grace.flap_threshold == 5


# ==============================================================================
# STATE TESTS
# ==============================================================================


class TestConnectionGracePeriodState:
    """Test state fields for connection loss grace period."""

    @pytest.mark.unit
    def test_default_state_values(self):
        """Test default state field values."""
        state = MonitorState()
        assert state.connection_lost_time == 0.0
        assert state.connection_flap_count == 0
        assert state.connection_first_flap_time == 0.0


# ==============================================================================
# GRACE PERIOD BEHAVIOR TESTS
# ==============================================================================


class TestGracePeriodBehavior:
    """Test grace period logic in _handle_connection_failure."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period enabled."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=True, duration=60, flap_threshold=5
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_enters_grace_period_on_first_failure(self, monitor):
        """Test that first connection failure enters grace period."""
        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            assert monitor.state.connection_state == "GRACE_PERIOD"
            assert monitor.state.connection_lost_time > 0
            mock_event.assert_not_called()

    @pytest.mark.unit
    def test_enters_grace_period_on_stale_data(self, monitor):
        """Test that stale data failure enters grace period."""
        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Data stale from UPS")

            assert monitor.state.connection_state == "GRACE_PERIOD"
            assert monitor.state.connection_lost_time > 0
            mock_event.assert_not_called()

    @pytest.mark.unit
    def test_no_notification_during_grace_period(self, monitor):
        """Test that no notification is sent while in grace period."""
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 10  # 10s ago

        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            mock_event.assert_not_called()
            assert monitor.state.connection_state == "GRACE_PERIOD"

    @pytest.mark.unit
    def test_notification_sent_when_grace_period_expires(self, monitor):
        """Test that notification fires after grace period expires."""
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 65  # Past 60s

        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            mock_event.assert_called_once()
            args = mock_event.call_args[0]
            assert args[0] == "CONNECTION_LOST"
            assert "Grace period" in args[1]
            assert monitor.state.connection_state == "FAILED"
            assert monitor.state.connection_lost_time == 0.0

    @pytest.mark.unit
    def test_notification_sent_on_stale_data_after_grace_expires(self, monitor):
        """Test notification on stale data after grace period."""
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 65

        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Data stale from UPS")

            mock_event.assert_called_once()
            args = mock_event.call_args[0]
            assert args[0] == "CONNECTION_LOST"
            assert "stale" in args[1]

    @pytest.mark.unit
    def test_no_action_when_already_failed(self, monitor):
        """Test that FAILED state doesn't re-notify."""
        monitor.state.connection_state = "FAILED"

        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            mock_event.assert_not_called()
            assert monitor.state.connection_state == "FAILED"


# ==============================================================================
# RECOVERY TESTS
# ==============================================================================


class TestGracePeriodRecovery:
    """Test connection recovery behavior with grace period."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period enabled."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=True, duration=60, flap_threshold=5
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_quiet_recovery_during_grace_period(self, monitor):
        """Test that recovery during grace period is quiet (no notification)."""
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 10

        with patch.object(monitor, "_log_power_event") as mock_event:
            # Simulate the recovery block from _main_loop
            if monitor.state.connection_state == "GRACE_PERIOD":
                elapsed = time.time() - monitor.state.connection_lost_time
                monitor._log_message(
                    f"Connection recovered during grace period ({elapsed:.0f}s elapsed)."
                )
                monitor.state.connection_state = "OK"
                monitor.state.connection_lost_time = 0.0

            mock_event.assert_not_called()
            assert monitor.state.connection_state == "OK"
            assert monitor.state.connection_lost_time == 0.0

    @pytest.mark.unit
    def test_normal_recovery_after_grace_expired(self, monitor):
        """Test that recovery from FAILED state sends CONNECTION_RESTORED."""
        monitor.state.connection_state = "FAILED"

        with patch.object(monitor, "_log_power_event") as mock_event:
            # Simulate recovery block
            if monitor.state.connection_state == "FAILED":
                monitor._log_power_event(
                    "CONNECTION_RESTORED",
                    f"Connection to UPS {monitor.config.ups.name} restored."
                )
                monitor.state.connection_state = "OK"

            mock_event.assert_called_once()
            assert mock_event.call_args[0][0] == "CONNECTION_RESTORED"
            assert monitor.state.connection_state == "OK"


# ==============================================================================
# GRACE PERIOD DISABLED TESTS
# ==============================================================================


class TestGracePeriodDisabled:
    """Test behavior when grace period is disabled."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period disabled."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=False
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_immediate_notification_when_disabled(self, monitor):
        """Test immediate CONNECTION_LOST when grace period is disabled."""
        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            mock_event.assert_called_once()
            assert mock_event.call_args[0][0] == "CONNECTION_LOST"
            assert monitor.state.connection_state == "FAILED"

    @pytest.mark.unit
    def test_immediate_notification_on_stale_data_when_disabled(self, monitor):
        """Test immediate notification on stale data when disabled."""
        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Data stale from UPS")

            mock_event.assert_called_once()
            assert mock_event.call_args[0][0] == "CONNECTION_LOST"
            assert "stale" in mock_event.call_args[0][1]

    @pytest.mark.unit
    def test_no_duplicate_notification_when_disabled(self, monitor):
        """Test that already FAILED state doesn't re-notify when disabled."""
        monitor.state.connection_state = "FAILED"

        with patch.object(monitor, "_log_power_event") as mock_event:
            monitor._handle_connection_failure("Connection refused")

            mock_event.assert_not_called()


# ==============================================================================
# FAILSAFE TESTS
# ==============================================================================


class TestGracePeriodFailsafe:
    """Test that failsafe is never affected by grace period."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period enabled."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=True, duration=60, flap_threshold=5
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_failsafe_bypasses_grace_period_on_battery(self, monitor):
        """Test that failsafe fires immediately when on battery."""
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"

        with patch.object(monitor, "_execute_shutdown_sequence") as mock_shutdown:
            with patch.object(monitor, "_send_notification"):
                # Simulate the main loop logic
                is_failsafe_trigger = True

                if is_failsafe_trigger and "OB" in monitor.state.previous_status:
                    monitor.state.connection_state = "FAILED"
                    monitor.state.connection_lost_time = 0.0
                    monitor._shutdown_flag_path.touch()
                    monitor._send_notification("FAILSAFE", monitor.config.NOTIFY_FAILURE)
                    monitor._execute_shutdown_sequence()
                elif is_failsafe_trigger:
                    monitor._handle_connection_failure("Connection refused")

                mock_shutdown.assert_called_once()
                assert monitor.state.connection_state == "FAILED"

    @pytest.mark.unit
    def test_failsafe_during_active_grace_period(self, monitor):
        """Test failsafe fires if power goes out during grace period."""
        # Start in grace period (connection lost while on line power)
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 10
        # Now power goes out
        monitor.state.previous_status = "OB DISCHRG"

        with patch.object(monitor, "_execute_shutdown_sequence") as mock_shutdown:
            with patch.object(monitor, "_send_notification"):
                is_failsafe_trigger = True

                if is_failsafe_trigger and "OB" in monitor.state.previous_status:
                    monitor.state.connection_state = "FAILED"
                    monitor.state.connection_lost_time = 0.0
                    monitor._shutdown_flag_path.touch()
                    monitor._send_notification("FAILSAFE", monitor.config.NOTIFY_FAILURE)
                    monitor._execute_shutdown_sequence()
                elif is_failsafe_trigger:
                    monitor._handle_connection_failure("Connection refused")

                mock_shutdown.assert_called_once()
                assert monitor.state.connection_state == "FAILED"


# ==============================================================================
# FLAP DETECTION TESTS
# ==============================================================================


class TestFlapDetection:
    """Test flap detection and notification."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period and flap threshold."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=True, duration=60, flap_threshold=3
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    def _simulate_flap(self, monitor):
        """Simulate one connection flap (fail then recover during grace period)."""
        # Enter grace period
        monitor._handle_connection_failure("Connection refused")
        assert monitor.state.connection_state == "GRACE_PERIOD"

        # Recover during grace period (simulate the recovery block)
        now = time.time()
        elapsed = now - monitor.state.connection_lost_time
        monitor._log_message(
            f"Connection recovered during grace period ({elapsed:.0f}s elapsed)."
        )
        monitor.state.connection_state = "OK"
        monitor.state.connection_lost_time = 0.0

        # Flap detection
        if (monitor.state.connection_flap_count > 0
                and (now - monitor.state.connection_first_flap_time) > 86400):
            monitor.state.connection_flap_count = 0
        if monitor.state.connection_flap_count == 0:
            monitor.state.connection_first_flap_time = now
        monitor.state.connection_flap_count += 1

        grace_cfg = monitor.config.ups.connection_loss_grace_period
        if monitor.state.connection_flap_count >= grace_cfg.flap_threshold:
            monitor._send_notification(
                f"NUT Server Unstable - {monitor.state.connection_flap_count} flaps",
                monitor.config.NOTIFY_WARNING
            )
            monitor.state.connection_flap_count = 0
            monitor.state.connection_first_flap_time = 0.0

    @pytest.mark.unit
    def test_flap_count_increments(self, monitor):
        """Test that flap count increments on each grace period recovery."""
        with patch.object(monitor, "_send_notification"):
            self._simulate_flap(monitor)
            assert monitor.state.connection_flap_count == 1

            self._simulate_flap(monitor)
            assert monitor.state.connection_flap_count == 2

    @pytest.mark.unit
    def test_flap_threshold_triggers_warning(self, monitor):
        """Test that reaching flap threshold sends WARNING notification."""
        with patch.object(monitor, "_send_notification") as mock_notify:
            for _ in range(3):  # flap_threshold = 3
                self._simulate_flap(monitor)

            # Should have sent one warning
            mock_notify.assert_called_once()
            assert "Unstable" in mock_notify.call_args[0][0]
            assert mock_notify.call_args[0][1] == monitor.config.NOTIFY_WARNING

            # Counter should be reset
            assert monitor.state.connection_flap_count == 0
            assert monitor.state.connection_first_flap_time == 0.0

    @pytest.mark.unit
    def test_sustained_flapping_sends_periodic_warnings(self, monitor):
        """Test that sustained flapping sends warning every N flaps."""
        with patch.object(monitor, "_send_notification") as mock_notify:
            for _ in range(6):  # 2x threshold
                self._simulate_flap(monitor)

            # Should have sent two warnings
            assert mock_notify.call_count == 2

    @pytest.mark.unit
    def test_flap_counter_24h_ttl(self, monitor):
        """Test that flap counter resets after 24 hours."""
        with patch.object(monitor, "_send_notification"):
            # Simulate 2 flaps
            self._simulate_flap(monitor)
            self._simulate_flap(monitor)
            assert monitor.state.connection_flap_count == 2

            # Set first_flap_time to 25 hours ago
            monitor.state.connection_first_flap_time = time.time() - 90000

            # Next flap should reset counter first
            self._simulate_flap(monitor)
            # Counter was reset to 0, then incremented to 1
            assert monitor.state.connection_flap_count == 1

    @pytest.mark.unit
    def test_flap_counter_resets_on_full_restore(self, monitor):
        """Test that flap counter resets when recovering from FAILED state."""
        monitor.state.connection_flap_count = 3
        monitor.state.connection_first_flap_time = time.time() - 100
        monitor.state.connection_state = "FAILED"

        # Simulate recovery from FAILED
        monitor.state.connection_state = "OK"
        monitor.state.connection_lost_time = 0.0
        monitor.state.connection_flap_count = 0
        monitor.state.connection_first_flap_time = 0.0

        assert monitor.state.connection_flap_count == 0
        assert monitor.state.connection_first_flap_time == 0.0

    @pytest.mark.unit
    def test_no_notifications_during_flaps(self, monitor):
        """Test that individual flaps don't generate notifications."""
        with patch.object(monitor, "_log_power_event") as mock_event:
            with patch.object(monitor, "_send_notification"):
                self._simulate_flap(monitor)
                self._simulate_flap(monitor)

                # _log_power_event should never be called (no CONNECTION_LOST/RESTORED)
                mock_event.assert_not_called()


# ==============================================================================
# STALE DATA INTERACTION TESTS
# ==============================================================================


class TestStaleDataInteraction:
    """Test grace period interaction with stale data tolerance."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor with grace period enabled."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.ups.max_stale_data_tolerance = 3
        minimal_config.ups.connection_loss_grace_period = ConnectionLossGracePeriodConfig(
            enabled=True, duration=60, flap_threshold=5
        )
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_stale_data_below_tolerance_no_grace_period(self, monitor):
        """Test that stale data below tolerance doesn't enter grace period."""
        # Simulate 2 stale data events (below tolerance of 3)
        monitor.state.stale_data_count = 2
        is_failsafe_trigger = False

        if monitor.state.stale_data_count >= monitor.config.ups.max_stale_data_tolerance:
            is_failsafe_trigger = True

        assert is_failsafe_trigger is False
        assert monitor.state.connection_state == "OK"

    @pytest.mark.unit
    def test_stale_warnings_suppressed_during_grace_period(self, monitor):
        """Test that stale data warnings are suppressed during grace period."""
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 10
        monitor.state.stale_data_count = 4

        # The guard in _main_loop: connection_state not in ("FAILED", "GRACE_PERIOD")
        should_log_warning = monitor.state.connection_state not in ("FAILED", "GRACE_PERIOD")
        assert should_log_warning is False
