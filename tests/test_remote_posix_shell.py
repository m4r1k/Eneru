"""Issue #128: remotes without a POSIX shell (RouterOS, Cisco, Junos, Windows).

``posix_shell`` (auto / true / false) decides whether Eneru wraps remote
commands in ``sh -c``; ``probe_command`` overrides the health probe per
server. RouterOS facts behind these tests were measured on a CRS310 running
7.21.5: errors go to stdout, and a failed command exits 0 or 1 at random.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eneru import Config, ConfigLoader, RemoteServerConfig, RemoteCommandConfig
from eneru import cli, config_catalog as cat
from eneru import remote_health as rh
from eneru.monitor import UPSGroupMonitor
from eneru.shutdown.remote import remote_shell_command
from eneru.state import MonitorState

pytestmark = pytest.mark.unit

ROUTEROS = '/system scheduler add name=x on-event="/system shutdown"'


def srv(**kw):
    base = dict(name="Switch", enabled=True, host="10.0.0.9", user="admin",
                shutdown_command=ROUTEROS)
    base.update(kw)
    return RemoteServerConfig(**base)


# ---------------------------------------------------------------------------
# Detection (real _posix_probe, run_command mocked)
# ---------------------------------------------------------------------------

@pytest.mark.real_posix_detection
class TestDetection:
    @pytest.mark.parametrize("rc,stdout,stderr,verdict", [
        (0, "eneru-posix-ok", "", True),
        (1, "eneru-posix-ok", "", True),
        (0, "syntax error (line 1 column 7)\n", "", False),   # RouterOS, rc 0
        (1, "syntax error (line 1 column 7)\n", "", False),   # RouterOS, rc 1
        (0, "% Invalid input: sh -c 'printf eneru-%s-ok posix'", "", False),
        (255, "", "Permission denied (publickey).", None),
        (124, "", "Command timed out", None),
        (127, "", "Command not found: ssh", None),
        (1, "", "local OSError", None),
        (0, "   \n", "", None),
    ])
    def test_verdicts(self, rc, stdout, stderr, verdict):
        with patch("eneru.remote_health.run_command",
                   return_value=(rc, stdout, stderr)) as run:
            assert rh.detect_posix_shell(srv()) is verdict
        # The marker is assembled remotely; it is never in the command text.
        sent = run.call_args[0][0][-1]
        assert sent == rh.POSIX_DETECT_COMMAND
        assert rh.POSIX_DETECT_MARKER not in sent

    def test_ssh_error_text_is_kept(self):
        with patch("eneru.remote_health.run_command",
                   return_value=(255, "", "No route to host")):
            assert rh._posix_probe(srv()) == (None, "No route to host", False)
        with patch("eneru.remote_health.run_command", return_value=(255, "", "")):
            assert rh._posix_probe(srv()) == (None, "exit code 255", False)
        with patch("eneru.remote_health.run_command", return_value=(124, "", "")):
            assert rh._posix_probe(srv(connect_timeout=7)) == (
                None, "timed out after 17s", False)
        with patch("eneru.remote_health.run_command", return_value=(0, "", "")):
            assert rh._posix_probe(srv()) == (None, "no output (exit code 0)",
                                              True)

    def test_dangling_ssh_option_is_unknown(self):
        assert rh._posix_probe(srv(ssh_options=["-i"]))[0] is None


class TestEffectiveMode:
    def test_loopback_is_always_posix(self):
        with patch("eneru.remote_health._posix_probe") as probe:
            assert rh.uses_posix_shell(srv(is_host_loopback=True,
                                           posix_shell=False))
        probe.assert_not_called()

    @pytest.mark.parametrize("value", [True, False])
    def test_explicit_value_wins_without_probing(self, value):
        with patch("eneru.remote_health._posix_probe") as probe:
            assert rh.uses_posix_shell(srv(posix_shell=value)) is value
        probe.assert_not_called()

    def test_auto_detects_once_and_caches(self):
        server = srv()
        with patch("eneru.remote_health._posix_probe",
                   return_value=(False, "", True)) as probe:
            assert rh.uses_posix_shell(server) is False
            assert rh.uses_posix_shell(server) is False
        probe.assert_called_once()
        assert server._detected_posix is False

    def test_auto_unknown_stays_posix_and_retries(self):
        server = srv()
        with patch("eneru.remote_health._posix_probe",
                   return_value=(None, "down", False)) as probe:
            assert rh.uses_posix_shell(server) is True
            assert rh.uses_posix_shell(server) is True
        assert probe.call_count == 2

    def test_detect_false_never_probes(self):
        with patch("eneru.remote_health._posix_probe") as probe:
            assert rh.uses_posix_shell(srv(), detect=False) is True
        probe.assert_not_called()

    @pytest.mark.parametrize("kw,cache,label", [
        (dict(is_host_loopback=True), None, "POSIX shell (configured)"),
        (dict(posix_shell=True), None, "POSIX shell (configured)"),
        (dict(posix_shell=False), None, "no POSIX shell (configured)"),
        ({}, True, "POSIX shell (detected)"),
        ({}, False, "no POSIX shell (detected)"),
        ({}, None, "POSIX shell (assumed: not detected)"),
    ])
    def test_labels(self, kw, cache, label):
        server = srv(**kw)
        server._detected_posix = cache
        assert rh.posix_mode_label(server) == label


class TestServerProbe:
    def test_probe_command_wins(self):
        server = srv(probe_command=":put ok")
        with patch("eneru.remote_health.run_remote_probe",
                   return_value=(True, "", 4)) as rp, \
                patch("eneru.remote_health._posix_probe") as probe:
            assert rh.run_server_probe(server, "true") == (True, "", 4)
        rp.assert_called_once_with(server, ":put ok", None)
        probe.assert_not_called()

    def test_shell_less_device_answering_is_healthy(self):
        server = srv()
        with patch("eneru.remote_health._posix_probe", return_value=(False, "", True)), \
                patch("eneru.remote_health.run_remote_probe") as rp:
            ok, err, _ = rh.run_server_probe(server, "true")
        assert (ok, err) == (True, "")
        rp.assert_not_called()  # `true` is no RouterOS command: never sent
        assert server._detected_posix is False

    def test_shell_less_device_down_reports_the_ssh_error(self):
        with patch("eneru.remote_health._posix_probe",
                   return_value=(None, "No route to host", False)):
            ok, err, _ = rh.run_server_probe(srv(posix_shell=False), "true")
        assert (ok, err) == (False, "No route to host")

    def test_configured_false_with_a_shell_is_still_healthy(self):
        with patch("eneru.remote_health._posix_probe", return_value=(True, "", True)), \
                patch("eneru.remote_health.run_remote_probe") as rp:
            ok, _, _ = rh.run_server_probe(srv(posix_shell=False), "true")
        assert ok
        rp.assert_not_called()

    def test_auto_posix_runs_the_global_probe_with_its_own_latency(self):
        server = srv()
        with patch("eneru.remote_health._posix_probe", return_value=(True, "", True)), \
                patch("eneru.remote_health.run_remote_probe",
                      return_value=(True, "", 40)) as rp:
            ok, _, latency = rh.run_server_probe(server, "true")
        assert ok and latency == 40  # the one-off detection is not counted
        rp.assert_called_once_with(server, "true")
        assert server._detected_posix is True

    def test_posix_without_safe_probe(self):
        ok, err, _ = rh.run_server_probe(srv(posix_shell=True), None)
        assert (ok, err) == (False, "unsafe probe command rejected")

    def test_auto_cached_posix_skips_detection(self):
        server = srv()
        server._detected_posix = True
        with patch("eneru.remote_health._posix_probe") as probe, \
                patch("eneru.remote_health.run_remote_probe",
                      return_value=(True, "", 1)):
            rh.run_server_probe(server, "true")
        probe.assert_not_called()


def test_health_manager_marks_shell_less_switch_healthy(tmp_path):
    config = Config()
    config.remote_health.enabled = True
    server = srv()
    manager = rh.RemoteHealthManager(
        config=config, group_label="Rack", servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(), log_fn=lambda m: None)
    with patch("eneru.remote_health._posix_probe", return_value=(False, "", True)), \
            patch("eneru.remote_health.run_remote_probe") as rp:
        rows = manager.check_once()
    assert rows[0]["status"] == rh.REMOTE_HEALTH_HEALTHY
    rp.assert_not_called()


# ---------------------------------------------------------------------------
# Runtime: the command actually sent over SSH
# ---------------------------------------------------------------------------

@pytest.fixture
def monitor(minimal_config, tmp_path):
    minimal_config.logging.state_file = str(tmp_path / "state")
    minimal_config.logging.battery_history_file = str(tmp_path / "history")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
    minimal_config.logging.file = None
    minimal_config.behavior.dry_run = False
    m = UPSGroupMonitor(minimal_config)
    m.state = MonitorState()
    m.logger = MagicMock()
    m._notification_worker = MagicMock()
    return m


class TestRuntime:
    def test_shell_less_gets_the_command_verbatim(self, monitor):
        server = srv(posix_shell=False)
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(0, "", "")) as run:
            ok, note = monitor._run_remote_command(server, ROUTEROS, 30, "x")
        assert (ok, note) == (True, "")
        assert run.call_args[0][0][-1] == ROUTEROS

    def test_auto_detected_shell_less_gets_the_command_verbatim(self, monitor):
        with patch("eneru.remote_health._posix_probe", return_value=(False, "", True)), \
                patch("eneru.shutdown.remote.run_command",
                      return_value=(0, "", "")) as run:
            monitor._run_remote_command(srv(), ROUTEROS, 30, "x")
        assert run.call_args[0][0][-1] == ROUTEROS

    def test_posix_keeps_the_wrapper(self, monitor):
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(0, "", "")) as run:
            monitor._run_remote_command(srv(), "poweroff", 30, "x")
        assert run.call_args[0][0][-1] == remote_shell_command("poweroff")

    def test_device_reply_is_surfaced_on_exit_zero(self, monitor):
        # RouterOS reports a bad command with exit 0 about half the time.
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(0, "\nbad command name sh (line 1 column 1)\n", "")):
            ok, note = monitor._run_remote_command(
                srv(posix_shell=False), "sh -c x", 30, "x")
        assert ok
        assert note.startswith("the device replied: bad command name sh")
        assert "not reliable" in note

    def test_posix_output_is_not_a_note(self, monitor):
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(0, "some output", "")):
            assert monitor._run_remote_command(srv(posix_shell=True), "x", 30,
                                               "x") == (True, "")

    def test_shutdown_logs_say_as_written(self, monitor):
        server = srv(posix_shell=False)
        logs = []
        with patch.object(monitor, "_log_message", side_effect=logs.append), \
                patch("eneru.shutdown.remote.run_command",
                      return_value=(0, "", "")):
            result = monitor._shutdown_remote_server(server)
        assert result.shutdown_sent
        assert any("Sending shutdown command (as written: no POSIX shell): "
                   + ROUTEROS in line for line in logs)


# ---------------------------------------------------------------------------
# Config: parse, validate, catalog
# ---------------------------------------------------------------------------

def load(tmp_path, entry):
    raw = {"remote_servers": [dict(dict(name="Switch", enabled=True,
                                        host="10.0.0.9", user="admin"), **entry)]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = ConfigLoader.load(str(path))
    return config, ConfigLoader.validate_config(config, raw_data=raw)


class TestConfig:
    @pytest.mark.parametrize("value,parsed", [
        (None, None), ("auto", None), ("AUTO", None), (True, True), (False, False),
    ])
    def test_parse(self, tmp_path, value, parsed):
        entry = {"shutdown_command": ROUTEROS}
        if value is not None:
            entry["posix_shell"] = value
        config, messages = load(tmp_path, entry)
        assert config.remote_servers[0].posix_shell is parsed
        assert not [m for m in messages if m.startswith("ERROR") and "posix_shell" in m]

    def test_default_is_auto(self):
        assert RemoteServerConfig().posix_shell is None
        assert RemoteServerConfig().probe_command is None

    def test_bad_value(self, tmp_path):
        _, messages = load(tmp_path, {"posix_shell": "yes"})
        assert any("posix_shell must be true, false or auto, got 'yes'" in m
                   for m in messages)

    def test_new_keys_are_known(self, tmp_path):
        _, messages = load(tmp_path, {"posix_shell": False, "shutdown_command":
                                      ROUTEROS, "probe_command": ":put ok"})
        assert not [m for m in messages if "Unknown" in m or "ERROR" in m]

    @pytest.mark.parametrize("entry,needle", [
        ({"is_host_loopback": True}, "combined with is_host_loopback"),
        ({"use_sudo": True}, "combined with use_sudo"),
        ({"pre_shutdown_commands": [{"command": "x"}]},
         "combined with pre_shutdown_commands"),
    ])
    def test_shell_less_rejects_posix_only_settings(self, tmp_path, entry, needle):
        _, messages = load(tmp_path, dict(posix_shell=False,
                                          shutdown_command=ROUTEROS, **entry))
        assert any(m.startswith("ERROR") and needle in m for m in messages)

    @pytest.mark.parametrize("command", [None, "sudo shutdown -h now", "  "])
    def test_shell_less_needs_its_own_command(self, tmp_path, command):
        entry = {"posix_shell": False}
        if command is not None:
            entry["shutdown_command"] = command
        _, messages = load(tmp_path, entry)
        assert any("needs an explicit shutdown_command" in m for m in messages)

    def test_shell_less_has_no_use_sudo_warning(self, tmp_path):
        _, messages = load(tmp_path, {"posix_shell": False,
                                      "shutdown_command": ROUTEROS})
        assert not [m for m in messages if "use_sudo is false" in m]
        _, messages = load(tmp_path, {"shutdown_command": "poweroff"})
        assert [m for m in messages if "use_sudo is false" in m]

    @pytest.mark.parametrize("probe", ["true; reboot", "/system shutdown", 5])
    def test_unsafe_probe_command(self, tmp_path, probe):
        _, messages = load(tmp_path, {"probe_command": probe})
        assert any("probe_command must be a harmless" in m for m in messages)

    def test_leftover_augment_remote_path_false_warns(self, tmp_path):
        _, messages = load(tmp_path, {"augment_remote_path": False})
        assert any(m.startswith("WARNING") and "set posix_shell: false" in m
                   for m in messages)
        _, messages = load(tmp_path, {"augment_remote_path": True})
        assert not [m for m in messages if "augment_remote_path" in m]

    def test_catalog_offers_both_keys(self):
        keys = {o.key: o for o in cat.REMOTE_SERVER_SECTION.children
                if hasattr(o, "kind")}
        assert keys["posix_shell"].kind == "tristate"
        assert keys["posix_shell"].default is None
        assert keys["probe_command"].nullable


def test_drill_prints_the_shell_mode(tmp_path, capsys):
    raw = {"ups": {"name": "UPS@localhost"},
           "remote_servers": [{"name": "Switch", "enabled": True,
                               "host": "10.0.0.9", "user": "admin",
                               "posix_shell": False,
                               "shutdown_command": ROUTEROS}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with patch("eneru.cli.run_server_probe", return_value=(True, "", 3)):
        with patch("sys.argv", ["eneru", "shutdown", "remote", "--server",
                                "Switch", "--dry-run", "--config", str(path)]):
            try:
                cli.main()
            except SystemExit as exc:
                assert exc.code in (0, None)
    out = capsys.readouterr().out
    assert "Remote shell: no POSIX shell (configured)" in out
    assert "Sending shutdown command (as written: no POSIX shell)" in out


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------

@pytest.mark.real_posix_detection
class TestAnsweredWithoutVerdict:
    def test_windows_configured_shell_less_is_healthy(self):
        # cmd.exe answers on stderr only: no verdict, but it is up.
        with patch("eneru.remote_health.run_command",
                   return_value=(1, "", "'sh' is not recognized as an internal "
                                        "or external command")):
            ok, err, _ = rh.run_server_probe(srv(posix_shell=False), "true")
        assert (ok, err) == (True, "")

    def test_auto_inconclusive_but_answering_falls_back_to_the_probe(self):
        # e.g. an authorized_keys allowlist wrapper that prints nothing.
        server = srv()
        with patch("eneru.remote_health.run_command", return_value=(1, "", "")), \
                patch("eneru.remote_health.run_remote_probe",
                      return_value=(True, "", 9)) as rp:
            assert rh.run_server_probe(server, "true") == (True, "", 9)
        rp.assert_called_once_with(server, "true")
        assert server._detected_posix is None

    def test_detection_timeout_is_passed_through(self):
        with patch("eneru.remote_health.run_command",
                   return_value=(0, "eneru-posix-ok", "")) as run:
            assert rh.detect_posix_shell(srv(), timeout=7) is True
        assert run.call_args.kwargs["timeout"] == 7

    def test_inconclusive_never_overwrites_the_cache(self):
        server = srv()
        server._detected_posix = False
        with patch("eneru.remote_health.run_command", return_value=(255, "", "x")):
            assert rh.detect_posix_shell(server) is None
        assert server._detected_posix is False


class TestConfigWinsOverDetection:
    @pytest.mark.parametrize("kw", [
        dict(use_sudo=True),
        dict(pre_shutdown_commands=[RemoteCommandConfig(command="x")]),
    ])
    def test_shell_settings_keep_posix(self, kw):
        server = srv(**kw)
        with patch("eneru.remote_health._posix_probe",
                   return_value=(False, "", True)):
            assert rh.uses_posix_shell(server) is True
        assert rh.posix_shell_conflict(server)

    def test_no_conflict_when_configured(self):
        server = srv(posix_shell=False, use_sudo=True)
        server._detected_posix = False
        assert not rh.posix_shell_conflict(server)


class TestRuntimeBudget:
    def test_no_spare_time_skips_detection(self, monitor):
        import time
        server = srv()
        with patch("eneru.remote_health._posix_probe") as probe:
            assert monitor._remote_uses_posix_shell(
                server, 30, time.monotonic() + 40) is True
        probe.assert_not_called()
        assert server._runtime_detect_tried is False

    def test_spare_time_caps_the_probe(self, monitor):
        import time
        server = srv(connect_timeout=10)
        with patch("eneru.remote_health._posix_probe",
                   return_value=(False, "", True)) as probe:
            assert monitor._remote_uses_posix_shell(
                server, 30, time.monotonic() + 72) is False
        assert probe.call_args.args[1] <= 12

    def test_unreachable_host_is_probed_once_per_run(self, monitor):
        server = srv()
        with patch("eneru.remote_health._posix_probe",
                   return_value=(None, "down", False)) as probe:
            assert monitor._remote_uses_posix_shell(server, 30, None) is True
            assert monitor._remote_uses_posix_shell(server, 30, None) is True
        probe.assert_called_once()
        assert probe.call_args.args[1] == server.connect_timeout + 10

    def test_log_names_a_detected_device(self, monitor):
        logs = []
        with patch("eneru.remote_health._posix_probe",
                   return_value=(False, "", True)), \
                patch.object(monitor, "_log_message", side_effect=logs.append), \
                patch("eneru.shutdown.remote.run_command",
                      return_value=(0, "", "")) as run:
            monitor._shutdown_remote_server(srv())
        assert any("(as written: no POSIX shell)" in line for line in logs)
        assert run.call_args[0][0][-1] == ROUTEROS

    def test_dry_run_never_detects(self, monitor):
        monitor.config.behavior.dry_run = True
        with patch("eneru.remote_health._posix_probe") as probe:
            result = monitor._shutdown_remote_server(srv())
        assert result.dry_run
        probe.assert_not_called()


def test_empty_probe_command_means_unset(tmp_path):
    config, messages = load(tmp_path, {"probe_command": ""})
    assert config.remote_servers[0].probe_command is None
    assert not [m for m in messages if "probe_command" in m]


def test_auto_unreachable_reports_the_ssh_error():
    with patch("eneru.remote_health._posix_probe",
               return_value=(None, "No route to host", False)), \
            patch("eneru.remote_health.run_remote_probe") as rp:
        ok, err, _ = rh.run_server_probe(srv(), "true")
    assert (ok, err) == (False, "No route to host")
    rp.assert_not_called()


@pytest.mark.real_posix_detection
def test_detection_timeout_text_is_the_real_cap():
    with patch("eneru.remote_health.run_command", return_value=(124, "", "")):
        assert rh._posix_probe(srv(connect_timeout=10), 7)[1] == "timed out after 7s"
        assert rh._posix_probe(srv(connect_timeout=10))[1] == "timed out after 20s"


def test_shell_less_failure_keeps_the_device_message(monitor):
    with patch("eneru.shutdown.remote.run_command",
               return_value=(1, "\nbad command name x (line 1 column 1)\n", "")):
        assert monitor._run_remote_command(srv(posix_shell=False), "x", 30, "x") \
            == (False, "bad command name x (line 1 column 1)")
    with patch("eneru.shutdown.remote.run_command", return_value=(1, "", "")):
        assert monitor._run_remote_command(srv(posix_shell=False), "x", 30, "x") \
            == (False, "exit code 1")
    with patch("eneru.shutdown.remote.run_command", return_value=(1, "out", "")):
        assert monitor._run_remote_command(srv(posix_shell=True), "x", 30, "x") \
            == (False, "exit code 1")


def test_each_shutdown_run_may_detect_again(monitor):
    server = srv()
    server._runtime_detect_tried = True
    monitor.config.ups_groups[0].remote_servers = [server]
    with patch.object(monitor, "_shutdown_servers_parallel", return_value=[]), \
            patch.object(monitor, "_shutdown_remote_server"):
        monitor._shutdown_remote_servers()
    assert server._runtime_detect_tried is False


def test_drill_label_reflects_detection_with_a_probe_command(tmp_path, capsys):
    raw = {"ups": {"name": "UPS@localhost"},
           "remote_servers": [{"name": "Switch", "enabled": True,
                               "host": "10.0.0.9", "user": "admin",
                               "probe_command": ":put ok",
                               "shutdown_command": ROUTEROS}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with patch("eneru.remote_health.run_remote_probe", return_value=(True, "", 3)), \
            patch("eneru.remote_health._posix_probe",
                  return_value=(False, "", True)), \
            patch("sys.argv", ["eneru", "shutdown", "remote", "--server",
                               "Switch", "--dry-run", "--config", str(path)]):
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code in (0, None)
    assert "Remote shell: no POSIX shell (detected)" in capsys.readouterr().out


@pytest.mark.real_posix_detection
@pytest.mark.parametrize("rc,stdout,stderr,verdict", [
    # ssh forwards a remote 127; only run_command's own message is local.
    (127, "bad command name sh (line 1 column 1)", "", False),
    (127, "", "Command not found: ssh", None),
    (127, "eneru-posix-ok", "", True),
    # 255 with output came from the remote, not from ssh itself.
    (255, "bad command name sh (line 1 column 1)", "", False),
    (255, "", "Connection refused", None),
])
def test_forwarded_exit_codes(rc, stdout, stderr, verdict):
    with patch("eneru.remote_health.run_command", return_value=(rc, stdout, stderr)):
        assert rh.detect_posix_shell(srv()) is verdict


def test_unsafe_per_server_probe_is_never_sent():
    # config check probes even a config that failed validation.
    with patch("eneru.remote_health.run_remote_probe") as rp:
        assert rh.run_server_probe(srv(probe_command="reboot"), "true") == (
            False, "unsafe probe_command rejected", 0)
    rp.assert_not_called()


@pytest.mark.parametrize("value", [0, False])
def test_falsy_probe_command_is_validated_not_dropped(tmp_path, value):
    config, messages = load(tmp_path, {"probe_command": value})
    assert config.remote_servers[0].probe_command is value
    assert any("probe_command must be a harmless" in m for m in messages)


def test_non_string_shutdown_command_is_an_error_not_a_crash(tmp_path):
    _, messages = load(tmp_path, {"posix_shell": False, "shutdown_command": 5})
    assert any("needs an explicit shutdown_command" in m for m in messages)


# ---------------------------------------------------------------------------
# probe_expect: a CLI whose exit code can't be trusted (RouterOS)
# ---------------------------------------------------------------------------

class TestProbeExpect:
    @pytest.mark.parametrize("rc,stdout,expect,result", [
        (0, "eneru-ok\n", "eneru-ok", (True, "")),
        (0, "eneru-ok\n", None, (True, "")),
        # RouterOS: a typo'd probe exits 0 with an error on stdout.
        (0, "bad command name putt (line 1 column 2)\n", "eneru-ok",
         (False, "probe output did not contain 'eneru-ok' "
                 "(got: bad command name putt (line 1 column 2))")),
        (0, "", "eneru-ok",
         (False, "probe output did not contain 'eneru-ok' (got: no output)")),
        # RouterOS also exits 1 for the same typo at random: same verdict.
        (1, "bad command name putt (line 1 column 2)\n", "eneru-ok",
         (False, "probe output did not contain 'eneru-ok' "
                 "(got: bad command name putt (line 1 column 2))")),
        (1, "eneru-ok", "eneru-ok", (False, "exit code 1")),
        (255, "", "eneru-ok", (False, "exit code 255")),
        # A local exec error (run_command's catch-all) is no probe output.
        (1, "", "eneru-ok", (False, "exit code 1")),
    ])
    def test_run_remote_probe(self, rc, stdout, expect, result):
        with patch("eneru.remote_health.run_command", return_value=(rc, stdout, "")):
            ok, err, _ = rh.run_remote_probe(srv(), ":put eneru-ok", expect)
        assert (ok, err) == result

    def test_server_probe_passes_probe_expect(self):
        server = srv(probe_command=":put eneru-ok", probe_expect="eneru-ok")
        with patch("eneru.remote_health.run_remote_probe",
                   return_value=(True, "", 2)) as rp:
            rh.run_server_probe(server, "true")
        rp.assert_called_once_with(server, ":put eneru-ok", "eneru-ok")

    def test_config_check_blames_the_probe_not_ssh(self):
        from eneru import config_check as cc
        server = srv(posix_shell=False, probe_command=":putt eneru-ok",
                     probe_expect="eneru-ok")
        with patch("eneru.remote_health.run_command",
                   return_value=(0, "bad command name putt", "")), \
                patch.object(cc, "command_exists", return_value=True):
            out = cc.probe_remote(Config(), server)
        assert len(out) == 1 and out[0].level == cc.LEVEL_ERROR
        assert out[0].message == (
            "Switch: probe_command ':putt eneru-ok': probe output did not "
            "contain 'eneru-ok' (got: bad command name putt)")
        assert "typos" in out[0].hint and "BatchMode" not in out[0].hint

    def test_config(self, tmp_path):
        config, messages = load(tmp_path, {"probe_command": ":put eneru-ok",
                                           "probe_expect": "eneru-ok"})
        assert config.remote_servers[0].probe_expect == "eneru-ok"
        assert not [m for m in messages if "probe_expect" in m]
        for blank in ("", "   "):
            config, messages = load(tmp_path, {"probe_expect": blank})
            assert config.remote_servers[0].probe_expect is None
            assert not [m for m in messages if "probe_expect" in m]

    @pytest.mark.parametrize("entry,needle", [
        ({"probe_expect": "eneru-ok"}, "needs a probe_command"),
        ({"probe_command": ":put x", "probe_expect": 5}, "must be non-empty text"),
    ])
    def test_invalid(self, tmp_path, entry, needle):
        _, messages = load(tmp_path, entry)
        assert any(m.startswith("ERROR") and needle in m for m in messages)

    def test_catalog(self):
        keys = {o.key: o for o in cat.REMOTE_SERVER_SECTION.children
                if hasattr(o, "kind")}
        assert keys["probe_expect"].kind == "str"
        assert keys["probe_expect"].nullable


def test_probe_expect_is_trimmed_and_non_text_never_crashes(tmp_path):
    config, _ = load(tmp_path, {"probe_command": ":put eneru-ok",
                                "probe_expect": "  eneru-ok "})
    assert config.remote_servers[0].probe_expect == "eneru-ok"
    with patch("eneru.remote_health.run_command", return_value=(0, "x", "")):
        assert rh.run_remote_probe(srv(), ":put x", 5)[0] is True
