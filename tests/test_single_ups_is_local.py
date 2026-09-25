"""6.2: a one-entry ``ups:`` list honours an EXPLICIT ``is_local: false``.

ELI5: one UPS, one house. Since 5.0 Eneru assumed the lone UPS always feeds
its own house, even when the YAML said "not my house" (``is_local: false``),
and switched the lights off here on every trigger. Now an explicit "not my
house" keeps this host up (its remote servers still shut down); an omitted
key keeps the old behaviour plus a warning; the legacy mapping form and
``is_local: true`` are unchanged.

Every display (plan, role/action, ON_BATTERY hint, config check, validate,
TUI/dashboard role) must match what the runtime really does.
"""

import textwrap
import time
from unittest.mock import MagicMock, patch

import pytest

from eneru import MonitorState, UPSGroupMonitor, outlook
from eneru.config import (
    NOT_LOCAL_SKIP,
    SINGLE_UPS_IS_LOCAL_UNSET_WARNING,
    ConfigLoader,
    single_ups_is_local_unset,
    single_ups_owns_host,
)
from eneru.shutdown.plan import build_shutdown_plan

POWEROFF_LINE = "Would execute: shutdown -h now"
REMOTE_LINE = "Would send command 'sudo shutdown -h now' to root@10.0.0.2"

REMOTE = """\
    remote_servers:
      - {name: nas, enabled: true, host: 10.0.0.2, user: root}
"""


def _yaml(form, *, remote=True, local_shutdown=True, dry_run=True):
    """Build a config for one of the single-UPS forms."""
    if form == "legacy":
        body = 'ups:\n  name: "UPS@localhost"\n'
        if remote:
            body += ("remote_servers:\n"
                     "  - {name: nas, enabled: true, host: 10.0.0.2, user: root}\n")
    else:
        body = 'ups:\n  - name: "UPS@localhost"\n'
        if form != "omitted":
            body += f"    is_local: {form}\n"
        if remote:
            body += REMOTE
    body += f"behavior: {{dry_run: {'true' if dry_run else 'false'}}}\n"
    body += f"local_shutdown: {{enabled: {'true' if local_shutdown else 'false'}}}\n"
    body += "notifications: {enabled: false}\n"
    return body


def _load(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    cfg = ConfigLoader.load(str(path))
    cfg.logging.shutdown_flag_file = str(tmp_path / "flag")
    cfg.logging.state_file = str(tmp_path / "state")
    cfg.logging.battery_history_file = str(tmp_path / "bh")
    cfg.logging.file = None
    cfg.statistics.db_directory = str(tmp_path / "db")
    return cfg


def _monitor(cfg):
    monitor = UPSGroupMonitor(cfg)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    monitor._send_notification = MagicMock()
    monitor._stats_store = MagicMock()
    return monitor


def _powered_off(argv):
    """True when the host poweroff argv (plus its wall message) ran."""
    return any(a[:3] == ["shutdown", "-h", "now"] for a in argv)


def _logged(monitor):
    return [c.args[0] for c in monitor.logger.log.call_args_list]


def _run_sequence(monitor):
    """Run the real shutdown sequence (commands mocked); return (lines, argv)."""
    with patch("eneru.monitor.run_command", return_value=(0, "", "")) as rc, \
            patch("eneru.shutdown.remote.run_command",
                  return_value=(0, "", "")):
        monitor._execute_shutdown_sequence()
    return _logged(monitor), [c.args[0] for c in rc.call_args_list]


# (form, remote, host powers off?)
CASES = [
    ("legacy", True, True),
    ("legacy", False, True),
    ("omitted", True, True),
    ("omitted", False, True),
    ("true", True, True),
    ("true", False, True),
    ("false", True, False),
    ("false", False, False),
]


# ---------------------------------------------------------------------------
# Parsing + the shared predicate
# ---------------------------------------------------------------------------

class TestParsing:

    @pytest.mark.unit
    @pytest.mark.parametrize("form,is_local,explicit", [
        ("legacy", True, False), ("omitted", False, False),
        ("true", True, True), ("false", False, True)])
    def test_explicit_flag(self, tmp_path, form, is_local, explicit):
        cfg = _load(tmp_path, _yaml(form))
        group = cfg.ups_groups[0]
        assert group.is_local is is_local
        assert group.is_local_explicit is explicit
        assert single_ups_owns_host(group) is (form != "false")

    @pytest.mark.unit
    def test_predicate_defaults(self):
        assert single_ups_owns_host(None) is True
        # A programmatic group (no YAML) keeps the pre-6.2 behaviour.
        from eneru.config import UPSGroupConfig
        assert single_ups_owns_host(UPSGroupConfig(is_local=False)) is True
        assert single_ups_owns_host(
            UPSGroupConfig(is_local=False, is_local_explicit=True)) is False


# ---------------------------------------------------------------------------
# Warning for an omitted is_local
# ---------------------------------------------------------------------------

class TestOmittedWarning:

    @pytest.mark.unit
    @pytest.mark.parametrize("form,warns", [
        ("legacy", False), ("omitted", True), ("true", False), ("false", False)])
    def test_validate_warning(self, tmp_path, form, warns):
        cfg = _load(tmp_path, _yaml(form))
        msgs = ConfigLoader.validate_config(cfg)
        expected = "WARNING: " + SINGLE_UPS_IS_LOCAL_UNSET_WARNING
        assert (expected in msgs) is warns
        assert single_ups_is_local_unset(cfg) is warns

    @pytest.mark.unit
    def test_no_warning_without_local_shutdown(self, tmp_path):
        cfg = _load(tmp_path, _yaml("omitted", local_shutdown=False))
        assert not single_ups_is_local_unset(cfg)

    @pytest.mark.unit
    def test_no_warning_for_multi_ups(self, tmp_path):
        text = ('ups:\n  - name: "A@h"\n  - name: "B@h"\n'
                "local_shutdown: {enabled: true, trigger_on: none}\n")
        cfg = _load(tmp_path, text)
        assert not single_ups_is_local_unset(cfg)

    @pytest.mark.unit
    def test_warning_text(self):
        assert SINGLE_UPS_IS_LOCAL_UNSET_WARNING == (
            "ups[0] has no is_local; as the only UPS it powers off this host on "
            "a shutdown trigger. Set is_local: true to confirm, or is_local: "
            "false to only monitor it / shut down its remote servers.")

    @pytest.mark.unit
    @pytest.mark.parametrize("form,warning,info", [
        ("omitted", True, False), ("false", False, True), ("true", False, False),
        ("legacy", False, False)])
    def test_startup_log(self, tmp_path, form, warning, info):
        monitor = _monitor(_load(tmp_path, _yaml(form)))
        monitor._log_host_ownership()
        text = "\n".join(_logged(monitor))
        assert (SINGLE_UPS_IS_LOCAL_UNSET_WARNING in text) is warning
        assert ("is_local: false -- this host is never powered off" in text) is info

    @pytest.mark.unit
    def test_startup_log_silent_in_coordinator_mode(self, tmp_path):
        monitor = _monitor(_load(tmp_path, _yaml("omitted")))
        monitor._coordinator_mode = True
        monitor._log_host_ownership()
        assert _logged(monitor) == []

    @pytest.mark.unit
    def test_startup_log_silent_without_local_shutdown(self, tmp_path):
        monitor = _monitor(_load(tmp_path, _yaml("false", local_shutdown=False)))
        monitor._log_host_ownership()
        assert _logged(monitor) == []


# ---------------------------------------------------------------------------
# Runtime == plan == role/action (parity)
# ---------------------------------------------------------------------------

class TestRuntimeParity:

    @pytest.mark.unit
    @pytest.mark.parametrize("form,remote,powers_off", CASES)
    def test_dry_run_sequence_plan_and_outlook_agree(self, tmp_path, form,
                                                     remote, powers_off):
        cfg = _load(tmp_path, _yaml(form, remote=remote))
        monitor = _monitor(cfg)
        group = cfg.ups_groups[0]
        plan = build_shutdown_plan(cfg, is_local=group.is_local)
        role = outlook.monitor_role(monitor)
        action = outlook.trigger_action(role)["label"]
        hint = monitor._on_battery_trigger_hint()

        lines, _argv = _run_sequence(monitor)
        text = "\n".join(lines)
        ran = {ph["id"]: ph["state"]
               for ph in monitor._shutdown_progress.snapshot()["phases"]}

        # The runtime.
        assert (POWEROFF_LINE in text) is powers_off
        assert (REMOTE_LINE in text) is remote
        # The plan.
        planned = {p["id"]: p["enabled"] for p in plan["phases"]}
        assert planned["local-poweroff"] is powers_off
        assert planned["local-poweroff"] is (ran["local-poweroff"] != "skipped")
        # The role + one-line action + ON_BATTERY hint.
        assert role["shutsDownLocalHost"] is powers_off
        assert ("Shuts down this host" in action) is powers_off
        assert ("Shuts down this host" in hint) is powers_off
        if not powers_off:
            assert ran["local-poweroff"] == "skipped"
            assert "this host stays up" in text
            assert role["kind"] == ("remote-only" if remote else "monitor-only")
            by = {p["id"]: p for p in plan["phases"]}
            assert by["local-poweroff"]["skipped"] == NOT_LOCAL_SKIP
            assert "never powered off" in plan["note"]
        else:
            assert role["kind"] == "local"

    @pytest.mark.unit
    @pytest.mark.parametrize("form,remote,powers_off", CASES)
    def test_tui_state_file_role_matches(self, tmp_path, form, remote,
                                         powers_off):
        """The TUI (``eneru monitor``) builds its role from state files."""
        cfg = _load(tmp_path, _yaml(form, remote=remote))
        blocks = outlook.state_file_outlook(
            cfg, cfg.ups_groups[0],
            {"STATUS": "OB DISCHRG", "BATTERY": "80", "RUNTIME": "3000",
             "EPOCH": str(int(time.time()))})
        assert blocks["role"]["shutsDownLocalHost"] is powers_off
        action = blocks["triggerOutlook"]["action"]["label"]
        assert ("Shuts down this host" in action) is powers_off

    @pytest.mark.unit
    @pytest.mark.parametrize("form,powers_off", [
        ("legacy", True), ("omitted", True), ("true", True), ("false", False)])
    def test_real_mode_poweroff_command(self, tmp_path, form, powers_off):
        """Not dry-run: the poweroff argv is (or is not) executed."""
        cfg = _load(tmp_path, _yaml(form, dry_run=False))
        monitor = _monitor(cfg)
        monitor._notification_worker = None
        lines, argv = _run_sequence(monitor)
        assert _powered_off(argv) is powers_off
        if not powers_off:
            assert any("SHUTDOWN SEQUENCE COMPLETE (is_local: false" in line
                       for line in lines)
            body = monitor._send_notification.call_args.args[0]
            assert "does not power this host" in body
            # The shutdown flag is cleared: future triggers can run again.
            assert not monitor._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_local_shutdown_disabled_keeps_old_wording(self, tmp_path):
        cfg = _load(tmp_path, _yaml("false", local_shutdown=False))
        lines, _ = _run_sequence(_monitor(cfg))
        assert any("(local shutdown disabled)" in line for line in lines)

    @pytest.mark.unit
    @pytest.mark.parametrize("form,powers_off", [
        ("omitted", True), ("true", True), ("false", False)])
    def test_trigger_path_t1_to_t4_and_fsd(self, tmp_path, form, powers_off):
        """T1-T4 and FSD enter through _trigger_immediate_shutdown."""
        cfg = _load(tmp_path, _yaml(form, dry_run=False))
        monitor = _monitor(cfg)
        monitor._notification_worker = None
        with patch("eneru.monitor.run_command", return_value=(0, "", "")) as rc, \
                patch("eneru.shutdown.remote.run_command",
                      return_value=(0, "", "")):
            monitor._trigger_immediate_shutdown("UPS signals FSD")
        argv = [c.args[0] for c in rc.call_args_list]
        assert _powered_off(argv) is powers_off

    @pytest.mark.unit
    @pytest.mark.parametrize("form,powers_off", [
        ("omitted", True), ("false", False)])
    def test_failsafe_connection_loss_path(self, tmp_path, form, powers_off):
        """FAILSAFE (NUT lost on battery) calls the sequence directly."""
        cfg = _load(tmp_path, _yaml(form, dry_run=False))
        cfg.ups_groups[0].ups.max_stale_data_tolerance = 1
        monitor = _monitor(cfg)
        monitor._notification_worker = None
        monitor._stats_store = None
        monitor.state.previous_status = "OB DISCHRG"

        def stop(*_a, **_k):
            monitor._stop_event.set()

        with patch.object(monitor, "_get_all_ups_data",
                          return_value=(False, {}, "Connection refused")), \
                patch.object(monitor._stop_event, "wait", side_effect=stop), \
                patch.object(monitor, "_run_ups_name_diagnostic"), \
                patch("eneru.monitor.run_command",
                      return_value=(0, "", "")) as rc, \
                patch("eneru.shutdown.remote.run_command",
                      return_value=(0, "", "")):
            monitor._main_loop()
        argv = [c.args[0] for c in rc.call_args_list]
        assert monitor._failsafe_initiated
        assert _powered_off(argv) is powers_off

    @pytest.mark.unit
    @pytest.mark.parametrize("form,remote,monitor_only", [
        ("omitted", False, False),  # T5 powers the host off, like T1-T4
        ("omitted", True, False),
        ("true", False, False),
        ("false", False, True),     # T5 only alerts
        ("false", True, False),     # T5 runs the remote servers
    ])
    def test_t5_self_test_failure_matches(self, tmp_path, form, remote,
                                          monitor_only):
        cfg = _load(tmp_path, _yaml(form, remote=remote))
        monitor = _monitor(cfg)
        assert monitor._is_monitor_only_group() is monitor_only
        role = outlook.monitor_role(monitor)
        assert (role["kind"] == "monitor-only") is monitor_only

    @pytest.mark.unit
    def test_t5_omitted_without_local_shutdown_is_monitor_only(self, tmp_path):
        cfg = _load(tmp_path, _yaml("omitted", remote=False,
                                    local_shutdown=False))
        assert _monitor(cfg)._is_monitor_only_group() is True


# ---------------------------------------------------------------------------
# Multi-UPS: unchanged behaviour, louder config check
# ---------------------------------------------------------------------------

MULTI = textwrap.dedent("""\
    ups:
      - name: "A@h"{a}
        remote_servers:
          - {{name: a, enabled: true, host: 10.0.0.2, user: root}}
      - name: "B@h"{b}
    local_shutdown: {{enabled: true, trigger_on: {trigger_on}}}
""")


def _multi(tmp_path, a="", b="", trigger_on="any"):
    fmt = {"a": f"\n    is_local: {a}" if a else "",
           "b": f"\n    is_local: {b}" if b else "",
           "trigger_on": trigger_on}
    return _load(tmp_path, MULTI.format(**fmt))


class TestMultiUps:

    @pytest.mark.unit
    @pytest.mark.parametrize("a,b", [("", ""), ("false", "false"), ("false", "")])
    def test_coordinator_handoff_unchanged(self, tmp_path, a, b):
        """No local group + trigger_on: any -> any group hands the poweroff to
        the coordinator, explicit or not (unchanged since 5.0)."""
        from eneru.config_check import _plan_for_group
        cfg = _multi(tmp_path, a, b)
        for group in cfg.ups_groups:
            plan = _plan_for_group(cfg, group)
            final = next(p for p in plan["phases"] if p["id"] == "local-poweroff")
            assert final["enabled"] and plan["coordinatorMode"]

    @pytest.mark.unit
    def test_all_explicit_false_with_any_is_an_error(self, tmp_path):
        from eneru.config_check import LEVEL_ERROR, _behavior_findings
        cfg = _multi(tmp_path, "false", "false")
        found = [f for f in _behavior_findings(cfg) if "Every UPS says" in f.message]
        assert len(found) == 1 and found[0].level == LEVEL_ERROR
        assert "trigger_on: none" in found[0].hint

    @pytest.mark.unit
    @pytest.mark.parametrize("a,b,trigger_on", [
        ("false", "", "any"), ("", "", "any"), ("false", "false", "none")])
    def test_other_multi_cases_keep_existing_findings(self, tmp_path, a, b,
                                                      trigger_on):
        from eneru.config_check import LEVEL_WARN, _behavior_findings
        cfg = _multi(tmp_path, a, b, trigger_on)
        findings = _behavior_findings(cfg)
        assert not any("Every UPS says" in f.message for f in findings)
        if trigger_on == "any":
            assert any(f.level == LEVEL_WARN and "No UPS is marked is_local"
                       in f.message for f in findings)

    @pytest.mark.unit
    def test_loader_warning_m7_kept(self, tmp_path):
        msgs = ConfigLoader.validate_config(_multi(tmp_path, "false", "false"))
        assert any("multi-UPS config has no is_local group" in m for m in msgs)


# ---------------------------------------------------------------------------
# Displays: config check, validate, root reasons, hot reload
# ---------------------------------------------------------------------------

class TestDisplays:

    @pytest.mark.unit
    def test_config_check_findings(self, tmp_path):
        from eneru.config_check import (
            LEVEL_INFO, LEVEL_WARN, _behavior_findings, _validation_findings)
        omitted = _load(tmp_path, _yaml("omitted"))
        warn = [f for f in _validation_findings(omitted, None)
                if SINGLE_UPS_IS_LOCAL_UNSET_WARNING in f.message]
        assert len(warn) == 1 and warn[0].level == LEVEL_WARN
        false = _load(tmp_path, _yaml("false"))
        info = [f for f in _behavior_findings(false)
                if "is_local: false on the only UPS" in f.message]
        assert len(info) == 1 and info[0].level == LEVEL_INFO
        # Explicit false + local_shutdown disabled: no "powers this host" nag.
        off = _load(tmp_path, _yaml("false", local_shutdown=False))
        assert not any("A UPS powers this host" in f.message
                       for f in _behavior_findings(off))

    @pytest.mark.unit
    @pytest.mark.parametrize("form,role,stays_on", [
        ("legacy", "protects this host", False),
        ("omitted", "protects this host", False),
        ("true", "protects this host", False),
        ("false", "monitoring / remote-only", True)])
    def test_order_tree_and_power_loss_plan(self, tmp_path, form, role, stays_on):
        from eneru.config_check import (
            format_order_tree, power_loss_plan, shutdown_order_tree)
        cfg = _load(tmp_path, _yaml(form))
        tree = shutdown_order_tree(cfg)
        assert tree[0]["role"] == role
        assert tree[0]["hostStaysOn"] is stays_on
        assert tree[0]["hostStaysOnReason"] == ("is_local: false" if stays_on
                                                else None)
        kinds = [p["kind"] for p in tree[0]["phases"]]
        assert ("final" in kinds) is (not stays_on)
        lines = "\n".join(format_order_tree(tree))
        assert ("This host stays on (is_local: false)." in lines) is stays_on
        preview = "\n".join(power_loss_plan(cfg))
        assert ("Power off this host" in preview) is (not stays_on)
        assert (f"({role.replace('this', 'THIS')})" in preview)

    @pytest.mark.unit
    @pytest.mark.parametrize("form,tree_has_local,tag", [
        ("legacy", True, " [is_local]"),
        ("omitted", True, ""),
        ("true", True, " [is_local]"),
        ("false", False, " [is_local: false, this host stays on]")])
    def test_validate_shutdown_sequence_tree(self, tmp_path, capsys, form,
                                             tree_has_local, tag):
        from eneru.cli import _print_group_summary
        cfg = _load(tmp_path, _yaml(form))
        capsys.readouterr()  # drop the loader's "Configuration loaded" line
        _print_group_summary(cfg.ups_groups[0], 1, cfg.multi_ups)
        out = capsys.readouterr().out
        assert ("Local shutdown" in out) is tree_has_local
        # Drains run only for an is_local group (the runtime's rule), even
        # when the lone UPS still powers the host off with is_local omitted.
        assert ("Filesystem sync" in out) is (form in ("legacy", "true"))
        assert out.splitlines()[0] == "  UPS: UPS@localhost" + tag

    @pytest.mark.unit
    @pytest.mark.parametrize("form,needs_root", [
        ("omitted", True), ("true", True), ("false", False)])
    def test_root_reasons(self, tmp_path, form, needs_root):
        from eneru.cli import _root_required_reasons
        cfg = _load(tmp_path, _yaml(form))
        reasons = _root_required_reasons(cfg)
        assert ("local_shutdown can power off the Eneru host" in reasons) \
            is needs_root

    @pytest.mark.unit
    @pytest.mark.parametrize("form,required", [
        ("legacy", True), ("omitted", True), ("true", True), ("false", False)])
    def test_readiness_requires_host_poweroff(self, tmp_path, form, required):
        """/ready must not demand a poweroff capability the runtime never
        uses (a remote-only container would otherwise read not-ready)."""
        from eneru.status import _required_capabilities
        cfg = _load(tmp_path, _yaml(form))
        assert ("local_host_poweroff" in _required_capabilities(cfg)) is required

    @pytest.mark.unit
    def test_host_poweroff_possible_edges(self, tmp_path):
        from eneru.config import host_poweroff_possible
        assert not host_poweroff_possible(
            _load(tmp_path, _yaml("omitted", local_shutdown=False)))
        # Multi-UPS keeps the trigger_on rule, explicit false or not.
        assert host_poweroff_possible(_multi(tmp_path, "false", "false"))
        assert not host_poweroff_possible(
            _multi(tmp_path, "false", "false", "none"))
        assert host_poweroff_possible(_multi(tmp_path, "true", "", "none"))

    @pytest.mark.unit
    def test_reload_marks_is_local_change_restart_required(self, tmp_path):
        from eneru.reload import apply_reload
        primary = _load(tmp_path, _yaml("omitted"))
        (tmp_path / "b").mkdir()
        new = _load(tmp_path / "b", _yaml("false"))
        report = apply_reload(primary, [primary], new)
        assert "ups_groups:UPS@localhost" in report["restartRequired"]
        assert primary.ups_groups[0].is_local_explicit is False  # not applied
