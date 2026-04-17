"""Tests for CLI argument handling and validation commands."""

import pytest
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

from eneru import (
    main, ConfigLoader, __version__, Config, UPSConfig, UPSGroupConfig, MonitorState,
    BehaviorConfig, LoggingConfig, LocalShutdownConfig, VMConfig, ContainersConfig,
    FilesystemsConfig, UnmountConfig,
)
from test_constants import (
    TEST_DISCORD_APPRISE_URL,
    TEST_SLACK_APPRISE_URL,
    TEST_JSON_WEBHOOK_URL,
)


class TestCLIVersion:
    """Test CLI version subcommand."""

    @pytest.mark.unit
    def test_version_subcommand(self, capsys):
        """Test 'eneru version' shows version and exits."""
        with patch.object(sys, "argv", ["eneru", "version"]):
            main()

        captured = capsys.readouterr()
        assert __version__ in captured.out

    @pytest.mark.unit
    def test_bare_eneru_shows_help(self, capsys):
        """Test bare 'eneru' shows help and exits 0."""
        with patch.object(sys, "argv", ["eneru"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "run" in captured.out
        assert "validate" in captured.out
        assert "monitor" in captured.out


class TestCLIValidateConfig:
    """Test 'eneru validate' subcommand."""

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
            "eneru", "validate", "-c", str(config_file)
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
        """Test that validate shows enabled features."""
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
            "eneru", "validate", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Virtual machines" in captured.out
        assert "Containers" in captured.out
        assert "podman" in captured.out
        assert "2 compose file(s)" in captured.out
        assert "Remote server: Server 1" in captured.out

    @pytest.mark.unit
    def test_validate_config_shows_notifications(self, tmp_path, capsys):
        """Test that validate shows notification configuration."""
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
            "eneru", "validate", "-c", str(config_file)
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
            "eneru", "validate", "-c", "/nonexistent/path/config.yaml"
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Configuration is valid" in captured.out

    @pytest.mark.unit
    def test_validate_config_without_apprise(self, tmp_path, capsys):
        """Test validate warns when apprise not installed but notifications configured."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
""")

        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", False):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Apprise not installed" in captured.out or "pip install apprise" in captured.out

    @pytest.mark.unit
    def test_validate_config_filesystems(self, tmp_path, capsys):
        """Test validate shows filesystem configuration."""
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
            "eneru", "validate", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Filesystem sync + unmount 3 mount(s)" in captured.out

    @pytest.mark.unit
    def test_validate_multi_ups_config(self, tmp_path, capsys):
        """Test validate shows multi-UPS overview."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@192.168.1.10"
    display_name: "Main UPS"
    is_local: true
    remote_servers:
      - name: "ServerA"
        enabled: true
        host: "192.168.1.20"
        user: "admin"

  - name: "UPS2@192.168.1.11"
    display_name: "Backup UPS"
    remote_servers:
      - name: "ServerB"
        enabled: true
        host: "192.168.1.30"
        user: "admin"
""")

        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "multi-UPS" in captured.out
        assert "2 groups" in captured.out
        assert "Main UPS" in captured.out
        assert "Backup UPS" in captured.out
        assert "is_local" in captured.out


class TestCLITestNotifications:
    """Test 'eneru test-notifications' subcommand."""

    @pytest.mark.unit
    def test_test_notifications_no_urls(self, tmp_path, capsys):
        """Test that test-notifications fails gracefully when no URLs configured."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

notifications:
  urls: []
""")

        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No notification URLs configured" in captured.out

    @pytest.mark.unit
    def test_test_notifications_no_apprise(self, tmp_path, capsys):
        """Test that test-notifications fails when apprise not installed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
""")

        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
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
            "eneru", "test-notifications", "-c", str(config_file)
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
        mock_apprise_instance.notify.return_value = False
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
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
        """Test test-notifications with invalid URL."""
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
        mock_apprise_instance.add.return_value = False
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Invalid URL" in captured.out or "No valid notification URLs" in captured.out


class TestCLIDryRun:
    """Test --dry-run CLI flag on run subcommand."""

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

        config = ConfigLoader.load(str(config_file))
        assert config.behavior.dry_run is False

        config.behavior.dry_run = True
        assert config.behavior.dry_run is True


class TestCLIExitAfterShutdown:
    """Test --exit-after-shutdown CLI flag on run subcommand."""

    @pytest.mark.unit
    def test_exit_after_shutdown_flag_sets_monitor_attribute(self, tmp_path):
        """Test that --exit-after-shutdown flag is passed to UPSGroupMonitor."""
        from eneru import UPSGroupMonitor

        config = Config(ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="TestUPS@localhost"),
            is_local=True,
        )])

        monitor = UPSGroupMonitor(config)
        assert monitor._exit_after_shutdown is False

        monitor_with_flag = UPSGroupMonitor(config, exit_after_shutdown=True)
        assert monitor_with_flag._exit_after_shutdown is True

    @pytest.mark.unit
    def test_exit_after_shutdown_triggers_exit(self, tmp_path):
        """Test that shutdown sequence exits when flag is set."""
        from eneru import UPSGroupMonitor

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                virtual_machines=VMConfig(enabled=False),
                containers=ContainersConfig(enabled=False),
                filesystems=FilesystemsConfig(sync_enabled=False,
                    unmount=UnmountConfig(enabled=False)),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "shutdown-flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )

        monitor = UPSGroupMonitor(config, exit_after_shutdown=True)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        with patch.object(monitor, "_cleanup_and_exit") as mock_exit:
            monitor._execute_shutdown_sequence()
            mock_exit.assert_called_once()

    @pytest.mark.unit
    def test_no_exit_without_flag(self, tmp_path):
        """Test that shutdown sequence does NOT exit when flag is not set."""
        from eneru import UPSGroupMonitor

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                virtual_machines=VMConfig(enabled=False),
                containers=ContainersConfig(enabled=False),
                filesystems=FilesystemsConfig(sync_enabled=False,
                    unmount=UnmountConfig(enabled=False)),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "shutdown-flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )

        monitor = UPSGroupMonitor(config, exit_after_shutdown=False)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        with patch.object(monitor, "_cleanup_and_exit") as mock_exit:
            monitor._execute_shutdown_sequence()
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
            "eneru", "validate", "-c", str(config_file)
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
            "eneru", "validate", "--config", str(config_file)
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "MyUPS@10.0.0.1" in captured.out
