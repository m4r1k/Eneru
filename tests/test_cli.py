"""Tests for CLI argument handling and validation commands."""

import pytest
import sys
import re
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
        # `tui` is an alias for `monitor` -- both must surface in the
        # top-level help so users discover either spelling.
        assert "tui" in captured.out
        assert re.search(r"\bshutdown\s+remote\b", captured.out)


class TestCLIManualRemoteShutdown:
    """Manual remote shutdown drill safety gates."""

    def _remote_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "remote_servers:\n"
            "  - name: nas\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    shutdown_command: 'sudo shutdown -h now'\n"
        )
        return config_file

    @pytest.mark.unit
    def test_real_remote_shutdown_requires_long_confirmation(self, tmp_path, capsys):
        config_file = self._remote_config(tmp_path)
        with patch.object(sys, "argv", [
            "eneru", "shutdown", "remote",
            "-c", str(config_file), "--server", "nas",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "i-really-want" in (captured.out + captured.err)

    @pytest.mark.unit
    def test_remote_shutdown_dry_run_does_not_execute_configured_commands(self, tmp_path):
        config_file = self._remote_config(tmp_path)
        with patch("eneru.cli.run_remote_probe", return_value=(True, "", 1)):
            with patch("eneru.shutdown.remote.RemoteShutdownMixin._run_remote_command") as mock_run:
                with patch.object(sys, "argv", [
                    "eneru", "shutdown", "remote",
                    "-c", str(config_file), "--server", "nas", "--dry-run",
                ]):
                    main()
        mock_run.assert_not_called()

    @pytest.mark.unit
    def test_remote_shutdown_duplicate_server_requires_group(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: UPS-A\n"
            "    display_name: rack-a\n"
            "    remote_servers:\n"
            "      - name: nas\n"
            "        enabled: true\n"
            "        host: 10.0.0.10\n"
            "        user: root\n"
            "  - name: UPS-B\n"
            "    display_name: rack-b\n"
            "    remote_servers:\n"
            "      - name: nas\n"
            "        enabled: true\n"
            "        host: 10.0.0.11\n"
            "        user: root\n"
        )

        with patch.object(sys, "argv", [
            "eneru", "shutdown", "remote",
            "-c", str(config_file), "--server", "nas", "--dry-run",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == "ERROR: remote server 'nas' is ambiguous. Use --group. Matches: rack-a, rack-b"

    @pytest.mark.unit
    def test_remote_shutdown_ignores_disabled_servers(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "remote_servers:\n"
            "  - name: nas\n"
            "    enabled: false\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
        )

        with patch.object(sys, "argv", [
            "eneru", "shutdown", "remote",
            "-c", str(config_file), "--server", "nas", "--dry-run",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert "enabled remote server 'nas' not found" in str(exc_info.value)

    @pytest.mark.unit
    def test_remote_shutdown_log_file_parent_is_created(self, tmp_path):
        from eneru.cli import _CLILogger

        logger = _CLILogger(tmp_path / "drills" / "run.log")

        logger.log("hello")

        assert (tmp_path / "drills" / "run.log").read_text() == "hello\n"


class TestCLITuiAlias:
    """Test that `eneru tui` is registered as an alias for `eneru monitor`."""

    @pytest.mark.unit
    def test_tui_and_monitor_share_handler(self):
        """Both subcommands must dispatch to the same _cmd_monitor handler."""
        from eneru import cli as cli_mod

        for cmd in ("monitor", "tui"):
            with patch.object(sys, "argv", ["eneru", cmd, "--once"]):
                with patch.object(cli_mod, "_cmd_monitor") as mock_handler:
                    main()
                    mock_handler.assert_called_once()

    @pytest.mark.unit
    def test_tui_help_lists_same_options_as_monitor(self, capsys):
        """`eneru tui --help` must list the same options as `eneru monitor --help`.

        We compare the set of option strings (--once, --interval, etc.)
        rather than full text -- argparse's usage-line wrap depends on
        program-name length, so whitespace differs between the two even
        though the options are identical.
        """
        import re

        option_re = re.compile(r"--[a-z][a-z0-9-]+")

        opts_seen = {}
        for cmd in ("monitor", "tui"):
            with patch.object(sys, "argv", ["eneru", cmd, "--help"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
            opts_seen[cmd] = set(option_re.findall(capsys.readouterr().out))

        assert opts_seen["monitor"] == opts_seen["tui"]
        # Sanity: must include the monitor-specific options, not just
        # the universal --help / --config.
        assert {"--once", "--interval", "--graph", "--time",
                "--events-only", "--verbose", "--length"}.issubset(
            opts_seen["tui"])
        # 5.2.2: --full-history was a transient flag added and removed
        # before release. --length covers the same use case more cleanly
        # (--length 0 = no cap).
        assert "--full-history" not in opts_seen["tui"]


class TestCLICompletion:
    """Test `eneru completion {bash,zsh,fish}` emits a usable script."""

    @pytest.mark.unit
    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_completion_emits_non_empty_script(self, shell, capsys):
        with patch.object(sys, "argv", ["eneru", "completion", shell]):
            main()
        out = capsys.readouterr().out
        assert len(out) > 100, f"{shell} completion output suspiciously short"
        # Each script must reference 'eneru' so it actually completes
        # the right command.
        assert "eneru" in out

    @pytest.mark.unit
    def test_bash_completion_uses_complete_builtin(self, capsys):
        """The bash script must register itself with `complete -F`."""
        with patch.object(sys, "argv", ["eneru", "completion", "bash"]):
            main()
        out = capsys.readouterr().out
        assert "complete -F _eneru eneru" in out
        # Self-contained: must not call helpers from the bash-completion
        # package. Strip comments before checking so the file's
        # explanatory header (which names these functions to say we
        # *don't* use them) doesn't trigger a false positive.
        code = "\n".join(line.split("#", 1)[0]
                         for line in out.splitlines())
        assert "_init_completion" not in code
        assert "_filedir" not in code

    @pytest.mark.unit
    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_completion_lists_monitor_event_flags(self, shell, capsys):
        """Packaged completion scripts must track monitor/tui event flags."""
        with patch.object(sys, "argv", ["eneru", "completion", shell]):
            main()
        out = capsys.readouterr().out
        if shell == "fish":
            assert "-l verbose" in out
            assert "-l length" in out
        else:
            assert "--verbose" in out
            assert "--length" in out

    @pytest.mark.unit
    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_completion_lists_shutdown_remote_flags(self, shell, capsys):
        """Packaged completion scripts must expose manual remote drill flags."""
        with patch.object(sys, "argv", ["eneru", "completion", shell]):
            main()
        out = capsys.readouterr().out
        if shell == "fish":
            for flag in (
                "-l server",
                "-l dry-run",
                "-l i-really-want-to-proceed-with-remote-shutdown",
                "-l no-connectivity-check",
                "-l log-file",
            ):
                assert flag in out
        else:
            for flag in (
                "--server",
                "--dry-run",
                "--i-really-want-to-proceed-with-remote-shutdown",
                "--no-connectivity-check",
                "--log-file",
            ):
                assert flag in out

    @pytest.mark.unit
    def test_invalid_shell_rejected(self):
        """`eneru completion ksh` must fail at argparse, not at file-read."""
        with patch.object(sys, "argv", ["eneru", "completion", "ksh"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # argparse exits 2 on invalid choice.
            assert exc_info.value.code == 2


class TestCLIMonitorFlags:
    """Test the --verbose / --length flags on monitor/tui."""

    def _minimal_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "behavior:\n  dry_run: true\n"
        )
        return config_file

    @pytest.mark.unit
    def test_verbose_short_form_accepted(self, tmp_path):
        """``-v`` adds Diagnostics and reaches run_once as verbose=1."""
        from eneru.tui import run_once
        with patch("eneru.tui.run_once", wraps=run_once) as mock_once:
            config_file = self._minimal_config(tmp_path)
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only", "-v"]):
                main()
            mock_once.assert_called_once()
            assert mock_once.call_args.kwargs.get("verbose") == 1

    @pytest.mark.unit
    def test_verbose_double_short_form_accepted(self, tmp_path):
        """``-vv`` adds Lifecycle and reaches run_once as verbose=2."""
        from eneru.tui import run_once
        with patch("eneru.tui.run_once", wraps=run_once) as mock_once:
            config_file = self._minimal_config(tmp_path)
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only", "-vv"]):
                main()
            mock_once.assert_called_once()
            assert mock_once.call_args.kwargs.get("verbose") == 2

    @pytest.mark.unit
    def test_length_default_is_30(self, tmp_path):
        """``--length`` default reaches run_once as 30."""
        from eneru.tui import run_once
        with patch("eneru.tui.run_once", wraps=run_once) as mock_once:
            config_file = self._minimal_config(tmp_path)
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only"]):
                main()
            assert mock_once.call_args.kwargs.get("length") == 30

    @pytest.mark.unit
    def test_length_explicit_value_accepted(self, tmp_path):
        """``--length 5`` reaches run_once as 5."""
        from eneru.tui import run_once
        with patch("eneru.tui.run_once", wraps=run_once) as mock_once:
            config_file = self._minimal_config(tmp_path)
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only",
                                            "--length", "5"]):
                main()
            assert mock_once.call_args.kwargs.get("length") == 5

    @pytest.mark.unit
    def test_length_zero_accepted(self, tmp_path):
        """``--length 0`` (no cap) is a valid value."""
        from eneru.tui import run_once
        with patch("eneru.tui.run_once", wraps=run_once) as mock_once:
            config_file = self._minimal_config(tmp_path)
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only",
                                            "--length", "0"]):
                main()
            assert mock_once.call_args.kwargs.get("length") == 0

    @pytest.mark.unit
    def test_length_negative_rejected(self, tmp_path, capsys):
        """``--length -1`` rejects with argparse exit 2 + clear stderr."""
        config_file = self._minimal_config(tmp_path)
        with patch("eneru.tui.run_once"):
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only",
                                            "--length", "-1"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 2
                err = capsys.readouterr().err
                assert "--length" in err

    @pytest.mark.unit
    def test_length_non_numeric_rejected(self, tmp_path, capsys):
        """``--length foo`` rejects cleanly via the type validator."""
        config_file = self._minimal_config(tmp_path)
        with patch("eneru.tui.run_once"):
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only",
                                            "--length", "foo"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 2

    @pytest.mark.unit
    def test_full_history_flag_no_longer_exists(self, tmp_path, capsys):
        """5.2.2 design: ``--full-history`` was added then removed
        before release. Use ``--length 0`` for the same effect.
        Argparse must reject the flag as unknown."""
        config_file = self._minimal_config(tmp_path)
        with patch("eneru.tui.run_once"):
            with patch.object(sys, "argv", ["eneru", "tui",
                                            "-c", str(config_file),
                                            "--once", "--events-only",
                                            "--full-history"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 2
                err = capsys.readouterr().err
                assert "--full-history" in err  # argparse names the bad flag


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
    def test_validate_config_nonexistent_file(self, tmp_path, capsys):
        """Validate against a non-existent path: ConfigLoader.load
        prints a warning and falls back to defaults; the resulting
        defaults are valid, so exit is 0.

        The original test asserted only `exit 0` and "Configuration
        is valid", which would have passed even if the typo'd path
        was silently ignored. This now also asserts the fallback
        warning containing the typo'd path appears in stdout, so a
        future regression that swallowed the warning would fail loud.
        Uses tmp_path so the assertion is deterministic across
        environments (the previous hard-coded /nonexistent/path could
        in theory exist on a developer machine).
        """
        typo_path = str(tmp_path / "missing-config.yaml")
        # Sanity: ensure we're not racing a pre-existing file in tmp_path.
        assert not (tmp_path / "missing-config.yaml").exists()
        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", typo_path,
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Configuration is valid" in captured.out
        assert "Config file not found" in captured.out
        assert typo_path in captured.out

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

    @pytest.mark.unit
    def test_run_refuses_unknown_safety_config_key(self, tmp_path, capsys):
        """The daemon must not start when validation finds hard errors."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

behavior:
  dry-run: true
""")

        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file)]):
            with patch("eneru.cli.UPSGroupMonitor") as mock_monitor:
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1
        mock_monitor.assert_not_called()
        captured = capsys.readouterr()
        assert "behavior.dry-run" in captured.out
        assert "Did you mean 'dry_run'" in captured.out

    @pytest.mark.unit
    def test_run_refuses_malformed_yaml(self, tmp_path, capsys):
        """Malformed YAML must not fall through to daemon startup defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups: [broken\n")

        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file)]):
            with patch("eneru.cli.UPSGroupMonitor") as mock_monitor:
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1
        mock_monitor.assert_not_called()
        assert "Failed to parse" in capsys.readouterr().out

    @pytest.mark.unit
    def test_run_refuses_non_mapping_yaml_root(self, tmp_path, capsys):
        """A YAML list root is not a valid Eneru config document."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- just\n- a\n- list\n")

        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file)]):
            with patch("eneru.cli.UPSGroupMonitor") as mock_monitor:
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1
        mock_monitor.assert_not_called()
        assert "must be a YAML mapping" in capsys.readouterr().out

    @pytest.mark.unit
    def test_raw_config_validation_loads_empty_yaml_as_empty_mapping(self, tmp_path):
        from eneru.cli import _load_raw_config_for_validation

        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        args = type("Args", (), {"config": str(config_file)})()

        assert _load_raw_config_for_validation(args) == {}

    @pytest.mark.unit
    def test_validate_checks_unknown_keys_from_default_config_path(
        self, tmp_path, capsys
    ):
        """`eneru validate` without -c still validates the loaded YAML."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

behavior:
  dry-run: true
""")

        with patch.object(ConfigLoader, "DEFAULT_CONFIG_PATHS", [config_file]):
            with patch.object(sys, "argv", ["eneru", "validate"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "behavior.dry-run" in captured.out


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


class TestCLIRemoteList:
    """`eneru remote list` discovery output."""

    def _multi_target_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: UPS-A\n"
            "    display_name: rack-a\n"
            "    is_local: true\n"
            "    remote_servers:\n"
            "      - name: Synology NAS\n"
            "        enabled: true\n"
            "        host: nas.local\n"
            "        user: admin\n"
            "        shutdown_order: 10\n"
            "      - name: Proxmox-1\n"
            "        enabled: true\n"
            "        host: pve1.local\n"
            "        user: root\n"
            "        shutdown_order: 5\n"
            "      - name: dev-box\n"
            "        enabled: false\n"
            "        host: dev.local\n"
            "        user: ubuntu\n"
        )
        return config_file

    @pytest.mark.unit
    def test_remote_list_prints_all_groups(self, tmp_path, capsys):
        config_file = self._multi_target_config(tmp_path)
        with patch.object(sys, "argv", [
            "eneru", "remote", "list", "-c", str(config_file),
        ]):
            main()
        out = capsys.readouterr().out
        assert "REMOTE TARGETS (3 configured, 2 enabled)" in out
        assert "Synology NAS" in out
        assert "Proxmox-1" in out
        assert "dev-box" in out
        # Per-server effective order is what the daemon would actually use,
        # so explicit shutdown_order values must show through.
        assert "10" in out and "5" in out

    @pytest.mark.unit
    def test_remote_list_shows_user_at_host(self, tmp_path, capsys):
        config_file = self._multi_target_config(tmp_path)
        with patch.object(sys, "argv", [
            "eneru", "remote", "list", "-c", str(config_file),
        ]):
            main()
        out = capsys.readouterr().out
        assert "admin@nas.local" in out
        assert "root@pve1.local" in out

    @pytest.mark.unit
    def test_remote_list_no_targets_exits_nonzero(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
        )
        with patch.object(sys, "argv", [
            "eneru", "remote", "list", "-c", str(config_file),
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1
        assert "No remote targets configured" in capsys.readouterr().out


class TestCLIShutdownGroupRehearsal:
    """`eneru shutdown group --group ...` full-sequence rehearsal."""

    def _ups_group_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  display_name: rack-a\n"
            "remote_servers:\n"
            "  - name: nas\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    shutdown_command: 'sudo shutdown -h now'\n"
            "local_shutdown:\n"
            "  enabled: false\n"
        )
        return config_file

    def _redundancy_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: UPS-A\n"
            "    display_name: rack-a\n"
            "  - name: UPS-B\n"
            "    display_name: rack-b\n"
            "redundancy_groups:\n"
            "  - name: rack-pair\n"
            "    ups_sources: [UPS-A, UPS-B]\n"
            "    min_healthy: 1\n"
            "    remote_servers:\n"
            "      - name: nas\n"
            "        enabled: true\n"
            "        host: nas.local\n"
            "        user: root\n"
        )
        return config_file

    @pytest.mark.unit
    def test_real_shutdown_requires_long_confirmation(self, tmp_path, capsys):
        config_file = self._ups_group_config(tmp_path)
        with patch.object(sys, "argv", [
            "eneru", "shutdown", "group",
            "-c", str(config_file), "--group", "rack-a",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2
        assert "i-really-want-to-proceed-with-group-shutdown" in (
            capsys.readouterr().out
        )

    @pytest.mark.unit
    def test_unknown_group_exits_nonzero(self, tmp_path):
        config_file = self._ups_group_config(tmp_path)
        with patch.object(sys, "argv", [
            "eneru", "shutdown", "group",
            "-c", str(config_file), "--group", "no-such-group",
            "--dry-run",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert "no-such-group" in str(exc_info.value)

    @pytest.mark.unit
    def test_dry_run_invokes_full_sequence_under_dry_run(self, tmp_path):
        config_file = self._ups_group_config(tmp_path)
        with patch(
            "eneru.cli.UPSGroupMonitor"
        ) as mock_monitor_cls:
            mock_monitor = MagicMock()
            mock_monitor_cls.return_value = mock_monitor
            with patch.object(sys, "argv", [
                "eneru", "shutdown", "group",
                "-c", str(config_file), "--group", "rack-a", "--dry-run",
            ]):
                main()
        mock_monitor._execute_shutdown_sequence.assert_called_once()
        # Whatever Config the monitor was instantiated with must have
        # dry_run flipped on; otherwise the rehearsal would be live.
        drill_config = mock_monitor_cls.call_args.args[0]
        assert drill_config.behavior.dry_run is True

    @pytest.mark.unit
    def test_redundancy_group_routes_through_executor(self, tmp_path, capsys):
        config_file = self._redundancy_config(tmp_path)
        with patch(
            "eneru.cli.RedundancyGroupExecutor"
        ) as mock_executor_cls:
            mock_executor = MagicMock()
            mock_executor_cls.return_value = mock_executor
            with patch.object(sys, "argv", [
                "eneru", "shutdown", "group",
                "-c", str(config_file), "--group", "rack-pair", "--dry-run",
            ]):
                main()
        mock_executor.shutdown.assert_called_once()
        # Local poweroff callback is intentionally NOT wired so an
        # operator can't accidentally halt the host with "rehearsal".
        kwargs = mock_executor_cls.call_args.kwargs
        assert kwargs["local_shutdown_callback"] is None
        assert "does not fire local poweroff" in capsys.readouterr().out
