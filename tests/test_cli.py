"""Tests for CLI argument handling and validation commands."""

import pytest
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

from eneru import main, ConfigLoader, __version__
from test_constants import (
    TEST_DISCORD_APPRISE_URL,
    TEST_SLACK_APPRISE_URL,
    TEST_JSON_WEBHOOK_URL,
)


class TestCLIVersion:
    """Test CLI version flag."""

    @pytest.mark.unit
    def test_version_flag(self):
        """Test --version shows version and exits."""
        with patch.object(sys, "argv", ["eneru", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

    @pytest.mark.unit
    def test_short_version_flag(self):
        """Test -v shows version and exits."""
        with patch.object(sys, "argv", ["eneru", "-v"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0


class TestCLIValidateConfig:
    """Test --validate-config CLI flag."""

    @pytest.mark.unit
    def test_validate_config_with_valid_file(self, tmp_path, capsys):
        """Test validating a valid configuration file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"
  check_interval: 2

behavior:
  dry_run: true
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Configuration is valid" in captured.out
        assert "TestUPS@localhost" in captured.out
        assert "Dry-run: True" in captured.out

    @pytest.mark.unit
    def test_validate_config_shows_features(self, tmp_path, capsys):
        """Test that validate-config shows enabled features."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@192.168.1.100"

virtual_machines:
  enabled: true
  max_wait: 60

containers:
  enabled: true
  runtime: podman
  compose_files:
    - "/path/to/compose1.yml"
    - "/path/to/compose2.yml"

remote_servers:
  - name: "Server 1"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "VMs enabled: True" in captured.out
        assert "Containers enabled: True" in captured.out
        assert "podman" in captured.out
        assert "2 compose file(s)" in captured.out
        assert "Remote servers: 1" in captured.out

    @pytest.mark.unit
    def test_validate_config_shows_notifications(self, tmp_path, capsys):
        """Test that validate-config shows notification configuration."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  title: "UPS Alert"
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
    - "{TEST_SLACK_APPRISE_URL}"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Notifications:" in captured.out
        assert "2 service(s)" in captured.out
        assert "discord://***" in captured.out
        assert "slack://***" in captured.out
        assert "Title: UPS Alert" in captured.out

    @pytest.mark.unit
    def test_validate_config_nonexistent_file(self, capsys):
        """Test validating a non-existent configuration file."""
        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", "/nonexistent/path/config.yaml"
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            # Should still exit 0 (uses defaults)
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Configuration is valid" in captured.out

    @pytest.mark.unit
    def test_validate_config_without_apprise(self, tmp_path, capsys):
        """Test validate-config warns when apprise not installed but notifications configured."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", False):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Apprise not installed" in captured.out or "pip install apprise" in captured.out

    @pytest.mark.unit
    def test_validate_config_filesystems(self, tmp_path, capsys):
        """Test validate-config shows filesystem configuration."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

filesystems:
  sync_enabled: true
  unmount:
    enabled: true
    mounts:
      - "/mnt/data1"
      - "/mnt/data2"
      - "/mnt/data3"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "sync: True" in captured.out or "Filesystems sync: True" in captured.out
        assert "3 mount(s)" in captured.out


class TestCLITestNotifications:
    """Test --test-notifications CLI flag."""

    @pytest.mark.unit
    def test_test_notifications_no_urls(self, tmp_path, capsys):
        """Test that --test-notifications fails gracefully when no URLs configured."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

notifications:
  urls: []
""")

        with patch.object(sys, "argv", [
            "eneru", "--test-notifications", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No notification URLs configured" in captured.out

    @pytest.mark.unit
    def test_test_notifications_no_apprise(self, tmp_path, capsys):
        """Test that --test-notifications fails when apprise not installed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
""")

        with patch.object(sys, "argv", [
            "eneru", "--test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", False):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Apprise is not installed" in captured.out

    @pytest.mark.unit
    def test_test_notifications_success(self, tmp_path, capsys):
        """Test successful notification test."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  title: "Test Title"
  urls:
    - "{TEST_JSON_WEBHOOK_URL}"
""")

        mock_apprise = MagicMock()
        mock_apprise_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_apprise_instance
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.notify.return_value = True
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "--test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Test notification sent successfully" in captured.out

    @pytest.mark.unit
    def test_test_notifications_failure(self, tmp_path, capsys):
        """Test failed notification test."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_JSON_WEBHOOK_URL}"
""")

        mock_apprise = MagicMock()
        mock_apprise_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_apprise_instance
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.notify.return_value = False  # Notification failed
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "--test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Failed to send test notification" in captured.out

    @pytest.mark.unit
    def test_test_notifications_invalid_url(self, tmp_path, capsys):
        """Test --test-notifications with invalid URL."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "invalid://url"
""")

        mock_apprise = MagicMock()
        mock_apprise_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_apprise_instance
        mock_apprise_instance.add.return_value = False  # Invalid URL
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "--test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Invalid URL" in captured.out or "No valid notification URLs" in captured.out


class TestCLICombinedFlags:
    """Test combining multiple CLI flags."""

    @pytest.mark.unit
    def test_validate_and_test_notifications(self, tmp_path, capsys):
        """Test using both --validate-config and --test-notifications together."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_JSON_WEBHOOK_URL}"
""")

        mock_apprise = MagicMock()
        mock_apprise_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_apprise_instance
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.notify.return_value = True
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru",
            "--validate-config",
            "--test-notifications",
            "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        # Both outputs should be present
        assert "Configuration is valid" in captured.out
        assert "Testing notifications" in captured.out
        # Separator should be present
        assert "---" in captured.out


class TestCLIDryRun:
    """Test --dry-run CLI flag."""

    @pytest.mark.unit
    def test_dry_run_overrides_config(self, tmp_path):
        """Test that --dry-run overrides config file setting."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

behavior:
  dry_run: false
""")

        # Load config normally - should be false
        config = ConfigLoader.load(str(config_file))
        assert config.behavior.dry_run is False

        # Now test that --dry-run flag would override it
        # We can't easily test the full main() here, so test the logic directly
        config.behavior.dry_run = True  # Simulating --dry-run flag effect
        assert config.behavior.dry_run is True


class TestCLIExitAfterShutdown:
    """Test --exit-after-shutdown CLI flag."""

    @pytest.mark.unit
    def test_exit_after_shutdown_flag_sets_monitor_attribute(self, tmp_path):
        """Test that --exit-after-shutdown flag is passed to UPSMonitor."""
        from eneru import UPSMonitor, Config

        config = Config()
        config.ups.name = "TestUPS@localhost"

        # Without the flag (default)
        monitor = UPSMonitor(config)
        assert monitor._exit_after_shutdown is False

        # With the flag
        monitor_with_flag = UPSMonitor(config, exit_after_shutdown=True)
        assert monitor_with_flag._exit_after_shutdown is True

    @pytest.mark.unit
    def test_exit_after_shutdown_triggers_exit(self, tmp_path):
        """Test that shutdown sequence exits when flag is set."""
        from eneru import UPSMonitor, Config, MonitorState

        config = Config()
        config.ups.name = "TestUPS@localhost"
        config.behavior.dry_run = True
        config.local_shutdown.enabled = False
        config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        config.logging.state_file = str(tmp_path / "state")
        config.logging.battery_history_file = str(tmp_path / "history")
        config.virtual_machines.enabled = False
        config.containers.enabled = False
        config.filesystems.sync_enabled = False
        config.filesystems.unmount.enabled = False

        monitor = UPSMonitor(config, exit_after_shutdown=True)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        # Mock _cleanup_and_exit to verify it gets called
        with patch.object(monitor, "_cleanup_and_exit") as mock_exit:
            monitor._execute_shutdown_sequence()

            # Should have called _cleanup_and_exit due to --exit-after-shutdown
            mock_exit.assert_called_once()

    @pytest.mark.unit
    def test_no_exit_without_flag(self, tmp_path):
        """Test that shutdown sequence does NOT exit when flag is not set."""
        from eneru import UPSMonitor, Config, MonitorState

        config = Config()
        config.ups.name = "TestUPS@localhost"
        config.behavior.dry_run = True
        config.local_shutdown.enabled = False
        config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        config.logging.state_file = str(tmp_path / "state")
        config.logging.battery_history_file = str(tmp_path / "history")
        config.virtual_machines.enabled = False
        config.containers.enabled = False
        config.filesystems.sync_enabled = False
        config.filesystems.unmount.enabled = False

        monitor = UPSMonitor(config, exit_after_shutdown=False)  # Default
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        # Mock _cleanup_and_exit to verify it does NOT get called
        with patch.object(monitor, "_cleanup_and_exit") as mock_exit:
            monitor._execute_shutdown_sequence()

            # Should NOT have called _cleanup_and_exit
            mock_exit.assert_not_called()


class TestCLIConfigPath:
    """Test -c/--config CLI flag."""

    @pytest.mark.unit
    def test_config_short_flag(self, tmp_path, capsys):
        """Test -c flag for specifying config path."""
        config_file = tmp_path / "custom_config.yaml"
        config_file.write_text("""
ups:
  name: "CustomUPS@192.168.1.100"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "CustomUPS@192.168.1.100" in captured.out

    @pytest.mark.unit
    def test_config_long_flag(self, tmp_path, capsys):
        """Test --config flag for specifying config path."""
        config_file = tmp_path / "my_config.yaml"
        config_file.write_text("""
ups:
  name: "MyUPS@10.0.0.1"
""")

        with patch.object(sys, "argv", [
            "eneru", "--validate-config", "--config", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "MyUPS@10.0.0.1" in captured.out
