"""Tests for `eneru config check` (src/eneru/config_check.py) and its CLI."""

import argparse
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eneru import cli
from eneru import config_check as cc
from eneru.actions import REMOTE_ACTIONS
from eneru.config import Config, RemoteCommandConfig, RemoteServerConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

ALL_BINS = {"upsc", "upscmd", "ssh", "logger", "virsh", "docker", "podman",
            "shutdown", "umount", "sync"}


@pytest.fixture
def env(monkeypatch):
    """Bare-metal runtime, root, every binary present. Tests narrow it."""
    state = types.SimpleNamespace(bins=set(ALL_BINS), runtime="bare process",
                                  euid=0)
    monkeypatch.setattr("eneru.runtime._detect_runtime_context",
                        lambda: state.runtime)
    monkeypatch.setattr(cc, "command_exists", lambda c: c in state.bins)
    monkeypatch.setattr(os, "geteuid", lambda: state.euid, raising=False)
    return state


def build(data):
    config, findings = cc.build_config(data)
    assert config is not None, findings
    return config


def msgs(findings, level=None):
    return [f.message for f in findings if level is None or f.level == level]


def joined(findings):
    return "\n".join(f"{f.level}|{f.section}|{f.message}|{f.hint}"
                     for f in findings)


def multi(*entries, **extra):
    """Multi-UPS mapping; pads to TWO entries so config.multi_ups is True."""
    entries = list(entries)
    names = {e.get("name") for e in entries}
    n = 0
    while len(entries) < 2:
        n += 1
        if f"pad{n}@h" not in names:
            entries.append({"name": f"pad{n}@h"})
    data = {"ups": entries}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Classification + small helpers
# ---------------------------------------------------------------------------

class TestClassify:
    @pytest.mark.parametrize("msg,section", [
        ("ERROR: remote_health.interval must be", "features"),
        ("ERROR: Redundancy group 'x': min_healthy", "redundancy"),
        ("ERROR: Remote server 'nas': use_sudo must", "remote"),
        ("ERROR: ups['a'].remote_servers[0] bad", "remote"),
        ("ERROR: UPS group 'x' has containers enabled", "local"),
        ("ERROR: ups[0].virtual_machines.max_wait must", "local"),
        ("WARNING: Notifications enabled but apprise package", "notifications"),
        ("INFO: Legacy Discord webhook_url detected", "notifications"),
        ("ERROR: ups['x'].triggers.low_battery_threshold", "safety"),
        # Regression: "suppress" in a trigger message is NOT notifications.
        ("WARNING: ups['U'].triggers.on_battery_stabilization_delay (30s) >= "
         "critical_runtime_threshold (25s): the stabilization window can "
         "suppress the runtime trigger", "safety"),
        ("ERROR: api.port must be", "features"),
        ("ERROR: mqtt.broker bad", "features"),
        ("ERROR: nut_control.allowed_commands must be", "ups"),
        ("ERROR: ups[0].check_interval must be", "ups"),
        ("ERROR: unknown top-level config key 'foo'.", "file"),
    ])
    def test_rules(self, msg, section):
        assert cc.classify_message(msg) == section

    def test_strip_level(self):
        assert cc._strip_level("ERROR: x") == "x"
        assert cc._strip_level("WARNING: y") == "y"
        assert cc._strip_level("INFO: z") == "z"
        assert cc._strip_level("  plain ") == "plain"

    def test_findings_from_printed(self):
        out = cc._findings_from_printed(
            "ERROR: bad thing\n  - reason one\n\nWARNING: careful\nbanner\n",
            "runtime")
        assert [f.level for f in out] == ["error", "warning", "info"]
        assert out[0].hint == "- reason one"
        # A continuation line with nothing before it becomes its own finding.
        assert cc._findings_from_printed("  - orphan", "x")[0].message == "- orphan"

    def test_report_counts(self):
        r = cc.CheckReport(findings=[cc.Finding("error", "file", "a"),
                                     cc.Finding("ok", "file", "b")])
        assert r.count("error") == 1 and r.has_errors
        assert not cc.CheckReport().has_errors

    def test_nut_helpers(self):
        assert cc._nut_host("ups@h:3493") == "h:3493"
        assert cc._nut_host("ups@") == "localhost"
        assert cc._nut_host("ups") == "localhost"
        assert cc._nut_name("ups@h") == "ups"
        assert cc.parse_upsc("a: 1\nnoise\nb.c: x y") == {"a": "1", "b.c": "x y"}

    def test_poweroff_binary(self):
        assert cc.poweroff_binary("shutdown -h now") == "shutdown"
        assert cc.poweroff_binary("") is None
        with patch("eneru.monitor.poweroff_command_parts",
                   side_effect=ValueError("x")):
            assert cc.poweroff_binary("shutdown") is None

    def test_group_label_redundancy(self):
        assert cc._group_label(types.SimpleNamespace(name="")) == \
            "redundancy group '(unnamed)'"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class TestLoading:
    def test_missing_file(self, tmp_path):
        data, f = cc.load_raw(str(tmp_path / "nope.yaml"))
        assert data is None and f[0].level == "error"

    def test_parse_error(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("a: [unclosed\n")
        data, f = cc.load_raw(str(p))
        assert data is None and "YAML parse error" in f[0].message

    def test_empty_and_non_mapping(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("")
        data, f = cc.load_raw(str(p))
        assert data == {} and f[0].level == "ok"
        p.write_text("- a\n- b\n")
        data, f = cc.load_raw(str(p))
        assert data is None and "mapping" in f[0].message

    def test_build_structural_error(self):
        config, f = cc.build_config({"ups": "just-a-string"})
        assert config is None and f and f[0].level == "error"

    def test_build_section_error(self):
        from eneru.config import ConfigSectionError
        with patch.object(cc.ConfigLoader, "_parse_config",
                          side_effect=ConfigSectionError("ERROR: x bad\nERROR: y bad")):
            config, f = cc.build_config({})
        assert config is None and len(f) == 2

    def test_build_generic_crash(self):
        with patch.object(cc.ConfigLoader, "_parse_config",
                          side_effect=RuntimeError("boom")):
            config, f = cc.build_config({})
        assert config is None and "boom" in f[0].message

    def test_check_file_missing_and_ok(self, tmp_path, env):
        r = cc.check_file(str(tmp_path / "missing.yaml"), probes=False)
        assert r.has_errors and not r.plan
        p = tmp_path / "c.yaml"
        p.write_text("ups:\n  name: ups@localhost\nlocal_shutdown:\n  enabled: false\n")
        r = cc.check_file(str(p), probes=False)
        assert r.findings[0].message.startswith("YAML parsed")
        assert r.plan

    def test_check_mapping_invalid_stops(self, env):
        r = cc.check_mapping({"ups": 5}, probes=False)
        assert r.has_errors and r.plan == []

    def test_check_mapping_with_probes(self, env):
        with patch.object(cc, "probe_findings",
                          return_value=[cc.Finding("ok", "ups", "probed")]) as pf:
            r = cc.check_mapping({"ups": {"name": "u@h"}}, probes=True)
        pf.assert_called_once()
        assert "probed" in msgs(r.findings)


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

class TestStatic:
    def test_validation_error_and_info(self, env):
        config = build({"ups": {"name": "u@h"}})
        with patch.object(cc.ConfigLoader, "validate_config",
                          return_value=["ERROR: triggers.x bad", "INFO: note",
                                        "WARNING: meh"]):
            out = cc._validation_findings(config, {})
        assert [f.level for f in out] == ["error", "info", "warning"]
        with patch.object(cc.ConfigLoader, "validate_config", return_value=[]):
            out = cc._validation_findings(config, {})
        assert out[-1].level == "ok"

    def test_static_prepare_systemexit_swallowed(self, env):
        config = build({"ups": {"name": "u@h"}})
        with patch.object(cli, "_prepare_runtime_config",
                          side_effect=SystemExit(1)):
            out = cc.static_findings(config, {})
        assert out

    def test_loopback_contract(self, env):
        def boom(_config):
            print("ERROR: no loopback")
            raise SystemExit(1)
        with patch.object(cli, "_exit_on_missing_loopback_contract", boom):
            out = cc._loopback_contract_findings(Config())
        assert out[0].level == "error"

    def test_dependencies_missing_everything(self, env):
        env.bins = set()
        config = build({
            "ups": {"name": "u@h"},
            "virtual_machines": {"enabled": True},
            "containers": {"enabled": True, "runtime": "auto"},
            "remote_servers": [{"name": "r", "enabled": True, "host": "h",
                                "user": "root"}],
        })
        out = cc._dependency_findings(config)
        text = joined(out)
        assert "Required command 'upsc'" in text
        assert "local_shutdown.command binary 'shutdown'" in text
        assert "'logger' not found" in text
        assert "'virsh' not found" in text
        assert "no container runtime found (podman / docker)" in text
        assert "'ssh' not found but remote servers" in text

    def test_dependencies_explicit_runtime_and_ok(self, env):
        env.bins = {"upsc", "shutdown", "logger", "podman"}
        config = build({"ups": {"name": "u@h"},
                        "containers": {"enabled": True, "runtime": "docker"}})
        out = cc._dependency_findings(config)
        assert out[0].level == "ok"
        assert "no container runtime found (docker)" in joined(out)

    def test_dependencies_container_delegating(self, env):
        env.runtime = "container (Docker)"
        env.bins = {"upsc"}
        with patch("eneru.runtime._uses_loopback_delegate", return_value=True):
            out = cc._dependency_findings(Config())
        text = joined(out)
        assert "'ssh' not found but local actions are delegated" in text
        assert "logger" not in text

    def test_optional_modules(self, env):
        config = build({
            "notifications": {"urls": ["json://x"]},
            "api": {"enabled": True, "auth": {"enabled": True}},
            "mqtt": {"enabled": True, "broker": "mqtt://h"},
        })
        real_import = __import__

        def fake_import(name, *a, **k):
            if name in ("apprise", "bcrypt", "paho.mqtt.client"):
                raise ImportError(name)
            return real_import(name, *a, **k)
        with patch("builtins.__import__", fake_import):
            out = cc._optional_module_findings(config)
        assert [f.level for f in out] == ["error", "error", "warning"]
        fake_apprise = types.ModuleType("apprise")
        with patch.dict(sys.modules, {"apprise": fake_apprise}):
            ok = cc._optional_module_findings(
                build({"notifications": {"urls": ["json://x"]}}))
        assert ok[0].level == "ok"

    def test_feature_findings(self, env):
        out = cc._feature_findings(build({"api": {"enabled": True,
                                                  "bind": "0.0.0.0"}}))
        assert [f.level for f in out] == ["warning", "info"]
        out = cc._feature_findings(build({"api": {"enabled": True,
                                                  "bind": "0.0.0.0",
                                                  "auth": {"enabled": True}}}))
        assert len(out) == 1
        assert cc._feature_findings(build({"api": {"enabled": True}})) == []
        out = cc._feature_findings(build({"mqtt": {"enabled": True,
                                                   "broker": "host:1883"}}))
        assert "no mqtt:// or mqtts://" in out[0].message
        out = cc._feature_findings(build({"mqtt": {
            "enabled": True, "broker": "mqtt://u:p@host:1883"}}))
        assert "cleartext" in out[0].hint
        assert cc._feature_findings(build({"mqtt": {
            "enabled": True, "broker": "mqtts://u:p@host"}})) == []

    def test_is_loopback_bind(self):
        assert cc._is_loopback_bind("127.0.0.5")
        assert cc._is_loopback_bind("::1")
        assert not cc._is_loopback_bind("10.0.0.1")

    def test_behavior_dry_run_and_local_disabled(self, env):
        out = cc._behavior_findings(build({
            "ups": {"name": "u@h"}, "behavior": {"dry_run": True},
            "local_shutdown": {"enabled": False}}))
        text = joined(out)
        assert "Dry-run is ON" in text
        assert "local_shutdown is disabled" in text
        assert "Monitoring-only config" in text
        assert "No notification service" in text

    def test_behavior_multi_no_local(self, env):
        cfg = build(multi({"name": "a@h"}, {"name": "b@h"}))
        assert cfg.multi_ups
        out = cc._behavior_findings(cfg)
        assert "trigger_on: any" in joined(out)
        out = cc._behavior_findings(build(multi(
            {"name": "a@h"}, {"name": "b@h"},
            local_shutdown={"trigger_on": "none"})))
        assert "never powers itself off" in joined(out)
        assert "Monitoring-only config" in joined(out)
        # With an is_local group neither multi-UPS notice appears.
        out = cc._behavior_findings(build(multi(
            {"name": "a@h", "is_local": True}, {"name": "b@h"})))
        assert "trigger_on: any" not in joined(out)
        assert "never powers itself off" not in joined(out)

    def test_behavior_ok(self, env):
        out = cc._behavior_findings(build({
            "ups": {"name": "u@h"}, "notifications": {"urls": ["json://x"]}}))
        assert out[0].level == "ok"
        assert "Monitoring-only" not in joined(out)

    def test_budget(self, env):
        cfg = build({"ups": {"name": "u@h"},
                     "triggers": {"critical_runtime_threshold": 20},
                     "virtual_machines": {"enabled": True, "max_wait": 300}})
        out = cc._budget_findings(cfg)
        assert out and "may need up to" in out[0].message
        assert cc._budget_findings(build({"ups": {"name": "u@h"}})) == []
        cfg.ups_groups[0].triggers.critical_runtime_threshold = "bad"
        assert cc._budget_findings(cfg) == []

    def test_paths(self, env, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        cfg = build({"logging": {"file": str(ro / "log"), "state_file": ""},
                     "statistics": {"db_directory": str(tmp_path / "missing")}})
        with patch.object(os, "access", return_value=False):
            out = cc._path_findings(cfg)
        assert len(out) == 1 and "logging.file" in out[0].message
        env.euid = 1000
        assert cc._path_findings(cfg) == []

    def test_privilege(self, env):
        assert "without root" in cc._privilege_findings(
            build(multi({"name": "a@h"},
                        local_shutdown={"enabled": False})))[0].message
        local = build({"ups": {"name": "u@h"}})
        assert cc._privilege_findings(local)[0].level == "ok"
        env.euid = 1000
        out = cc._privilege_findings(local)
        assert out[0].level == "warning" and "uid 1000" in out[0].hint
        env.runtime = "container (Docker)"
        with patch.object(cli, "_find_host_loopback", return_value=("x", object())):
            out = cc._privilege_findings(local)
        assert "delegated" in out[0].message

    def test_static_end_to_end(self, env):
        out = cc.static_findings(build({"ups": {"name": "u@h"}}), {"ups": {"name": "u@h"}})
        assert any(f.message == "Schema and semantic validation passed" for f in out)


# ---------------------------------------------------------------------------
# NUT probe
# ---------------------------------------------------------------------------

UPSC_OK = ("ups.status: OL\nbattery.charge: 100\nbattery.runtime: 1800\n"
           "device.mfr: APC\ndevice.model: Smart-UPS\n"
           "ups.realpower.nominal: 900\n")


def fake_run(listing="ups\n", vars_=UPSC_OK, list_code=0, vars_code=0):
    def run(cmd, timeout=30, env_overrides=None, **_):
        if cmd[:2] == ["upsc", "-l"]:
            return list_code, listing, "conn refused" if list_code else ""
        return vars_code, vars_, "var fail" if vars_code else ""
    return run


class TestProbeUps:
    def probe(self, env, data, run, commands=(True, ["test.battery.start"], "")):
        config = build(data)
        with patch.object(cc, "_run", run), \
                patch("eneru.nut_control.list_commands",
                      return_value=commands) as lc:
            out = cc.probe_ups(config, config.ups_groups[0])
        return out, lc

    def test_no_upsc(self, env):
        env.bins = set()
        out, _ = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run())
        assert "not installed" in out[0].message

    def test_unreachable(self, env):
        out, _ = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run(list_code=1))
        assert out[0].level == "error" and "conn refused" in out[0].message

    def test_name_missing(self, env):
        out, _ = self.probe(env, {"ups": {"name": "bob@h"}},
                            fake_run(listing="apc\nInit SSL: x\n"))
        assert "does not exist" in out[0].message and "apc" in out[0].message
        out, _ = self.probe(env, {"ups": {"name": "bob@h"}}, fake_run(listing=""))
        assert "available: none" in out[0].message

    def test_vars_fail(self, env):
        out, _ = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run(vars_code=1))
        assert out[-1].level == "error" and "var fail" in out[-1].message

    def test_ok_anonymous(self, env):
        out, lc = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run())
        text = joined(out)
        assert "APC Smart-UPS: status OL, charge 100%, runtime 30m 0s" in text
        assert "listed anonymously" in text
        assert not [f for f in out if f.level in ("error", "warning")]
        assert lc.call_args.kwargs["username"] == ""

    def test_warnings(self, env):
        vars_ = "ups.status: OB DISCHRG\nups.realpower.nominal: 500\n"
        data = {"ups": {"name": "ups@h"}, "energy": {"nominal_power": 900}}
        with patch.object(cc.time, "monotonic", side_effect=[0.0, 5.0]):
            out, _ = self.probe(env, data, fake_run(vars_=vars_))
        text = joined(out)
        assert "answered slowly" in text
        assert "ON BATTERY" in text
        assert "battery.charge" in text and "battery.runtime" in text
        assert "nominal_power (900 W)" in text

    def test_runtime_below_threshold(self, env):
        vars_ = "ups.status: OL\nbattery.charge: 90\nbattery.runtime: 300\n"
        out, _ = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run(vars_=vars_))
        assert "at or below" in joined(out)

    def test_credentials(self, env):
        data = {"ups": {"name": "ups@h"},
                "nut_control": {"username": "u", "password": "p"}}
        out, lc = self.probe(env, data, fake_run())
        assert "NUT login as 'u' works" in joined(out)
        assert lc.call_args.kwargs["password"] == "p"
        out, _ = self.probe(env, data, fake_run(), commands=(False, [], "denied"))
        assert "failed: denied" in joined(out)

    def test_no_upscmd(self, env):
        env.bins = {"upsc"}
        out, lc = self.probe(env, {"ups": {"name": "ups@h"}}, fake_run())
        lc.assert_not_called()

    def test_self_test_not_exposed(self, env):
        data = {"ups": {"name": "ups@h"},
                "self_test": {"enabled": True, "command": "test.battery.start"}}
        out, _ = self.probe(env, data, fake_run(), commands=(
            True, ["test.battery.start.quick", "beeper.toggle"], ""))
        f = [x for x in out if "not exposed" in x.message][0]
        assert f.hint == "Available test commands: test.battery.start.quick"
        out, _ = self.probe(env, data, fake_run(), commands=(True, ["beeper.on"], ""))
        assert "none" in [x for x in out if "not exposed" in x.message][0].hint
        out, _ = self.probe(env, data, fake_run())
        assert "not exposed" not in joined(out)


# ---------------------------------------------------------------------------
# Remote command analysis
# ---------------------------------------------------------------------------

class TestCommandBinary:
    @pytest.mark.parametrize("cmd,expected", [
        ("shutdown -h now", ("shutdown", False, ["-h", "now"])),
        ("sudo -i synoshutdown -s", ("synoshutdown", True, ["-s"])),
        ("sudo -n -u admin poweroff", ("poweroff", True, [])),
        ("/usr/bin/sudo systemctl poweroff", ("systemctl", True, ["poweroff"])),
        ("LANG=C FOO=1 poweroff", ("poweroff", False, [])),
        ("systemctl stop foo | tee x; echo y", ("systemctl", False, ["stop", "foo"])),
        ("sudo -u", (None, True, [])),
        ("FOO=1", (None, False, [])),
        ("", (None, False, [])),
        ("echo 'unterminated", (None, False, [])),
    ])
    def test_cases(self, cmd, expected):
        assert cc.command_binary(cmd) == expected


class TestCommandChecks:
    def kinds(self, checks):
        return [(c.kind, c.binary) for c in checks]

    def test_final_use_sudo_prefixes(self):
        checks, notes = cc.command_checks("shutdown -h now", True, final=True,
                                          user="bob")
        assert self.kinds(checks) == [("exists", "shutdown"), ("sudo", "shutdown")]
        assert notes == []

    def test_final_non_root_without_sudo_note(self):
        checks, notes = cc.command_checks("poweroff", False, final=True,
                                          user="bob")
        assert self.kinds(checks) == [("exists", "poweroff")]
        assert "most systems refuse" in notes[0]
        _, notes = cc.command_checks("poweroff", False, final=True, user="root")
        assert notes == []

    def test_custom_command_use_sudo_applies(self):
        # 6.2: use_sudo prefixes custom commands too, like the runtime.
        checks, notes = cc.command_checks("systemctl stop foo", True)
        assert self.kinds(checks) == [("exists", "systemctl"), ("sudo", "systemctl")]
        assert checks[-1].script == "sudo -n -l systemctl stop foo"
        assert notes == []
        checks, notes = cc.command_checks("sudo -n systemctl stop foo", True)
        assert self.kinds(checks) == [("exists", "systemctl"), ("sudo", "systemctl")]
        assert notes == []
        checks, notes = cc.command_checks("systemctl stop foo", False)
        assert self.kinds(checks) == [("exists", "systemctl")]

    def test_pipeline_under_sudo_notes_first_command_only(self):
        _, notes = cc.command_checks("systemctl stop x | tee /root/log", True)
        assert "only its first command runs under sudo" in notes[0]
        _, notes = cc.command_checks("systemctl stop x | tee log", False)
        assert notes == []

    def test_unparseable(self):
        checks, notes = cc.command_checks("'oops", False)
        assert checks == [] and "could not parse" in notes[0]

    @pytest.mark.parametrize("action", sorted(REMOTE_ACTIONS))
    def test_every_action_has_checks(self, action):
        checks = cc.action_checks(action, True, path="/a b.yml",
                                  mounts=["/mnt/x", {"path": "/mnt/y"},
                                          {"path": ""}, 5])
        assert checks and all(c.script for c in checks)
        assert not any(c.label.startswith("unknown action") for c in checks)

    def test_action_details(self):
        compose = cc.action_checks("stop_compose", False, path="/a b.yml")
        assert "test -r '/a b.yml'" in compose[-1].script
        assert len(cc.action_checks("stop_compose", False)) == 2
        um = cc.action_checks("unmount_filesystems", False, mounts=["/m"])
        assert [c.kind for c in um] == ["exists", "run"]
        assert um[1].fail_level == "warning"
        assert cc.action_checks("stop_vms", True)[1].script.startswith("sudo -n ")
        assert cc.action_checks("bogus", False)[0].label == "unknown action 'bogus'"


class TestRemoteScript:
    def test_runs_locally_and_parses(self, tmp_path):
        spaced = tmp_path / "dir with space" / "compose.yml"
        spaced.parent.mkdir()
        spaced.write_text("x")
        checks = [
            cc.RemoteCheck("sh exists", "command -v sh", "exists", binary="sh"),
            cc.RemoteCheck("file", f"test -r {cc._q(str(spaced))}", "run"),
            cc.RemoteCheck("missing file", f"test -r {cc._q(str(spaced) + 'x')}", "run"),
            cc.RemoteCheck("echo", "echo 'first line'; echo second", "run"),
        ]
        script = cc.build_remote_script(checks)
        out = subprocess.run(["sh", "-c", script], capture_output=True,
                             text=True, timeout=30)
        res = cc.parse_remote_output(out.stdout + "\nnoise\n__ENERU_CHECK__ x 0\n")
        assert res[0][0] == 0 and res[1][0] == 0
        assert res[2][0] != 0
        assert res[3] == (0, "first line")

    def test_parse_edge_cases(self):
        assert cc.parse_remote_output("") == {}
        assert cc.parse_remote_output("__ENERU_CHECK__ 1 nan x") == {}
        assert cc.parse_remote_output("__ENERU_CHECK__ 1") == {}
        assert cc.parse_remote_output("__ENERU_CHECK__ 2 0") == {2: (0, "")}


# ---------------------------------------------------------------------------
# Remote probe
# ---------------------------------------------------------------------------

def server(**kw):
    base = dict(name="nas", enabled=True, host="10.0.0.2", user="bob",
                shutdown_command="sudo shutdown -h now")
    base.update(kw)
    return RemoteServerConfig(**base)


class TestProbeRemote:
    def run_probe(self, env, srv, *, reach=(True, "", 12), script_out=None,
                  identity=(True, "", 1), config=None):
        config = config or Config()

        def run(cmd, timeout=30, **_):
            checks, _n = cc.remote_checks(config, srv)
            if script_out is not None:
                return script_out(checks)
            lines = [f"__ENERU_CHECK__ {i} 0 ok" for i in range(len(checks))]
            return 0, "\n".join(lines), ""
        with patch("eneru.remote_health.run_remote_probe", return_value=reach) as rp, \
                patch("eneru.remote_health.run_loopback_identity_probe",
                      return_value=identity), \
                patch.object(cc, "_run", side_effect=run):
            out = cc.probe_remote(config, srv)
        return out, rp

    def test_loopback_identity_is_auto_populated_like_the_daemon(self, env, tmp_path):
        # The daemon reads the bind-mounted identity file at startup
        # (RemoteHealthManager); the checker must do the same before probing,
        # or a correct /etc/machine-id mount reads as "identity unknown".
        ident = tmp_path / "machine-id"
        ident.write_text("abc123\n")
        srv = server(is_host_loopback=True,
                     host_identity_command=f"cat {ident}")
        seen = {}

        def probe(s):
            seen["expected"] = s.expected_host_identity
            return True, "", 1
        with patch("eneru.remote_health.run_remote_probe", return_value=(True, "", 1)), \
                patch("eneru.remote_health.run_loopback_identity_probe", side_effect=probe), \
                patch.object(cc, "_run", return_value=(0, "", "")):
            cc.probe_remote(Config(), srv)
        assert seen["expected"] == "abc123"
        # An explicit value is never overwritten.
        srv2 = server(is_host_loopback=True, expected_host_identity="set",
                      host_identity_command=f"cat {ident}")
        with patch("eneru.remote_health.run_remote_probe", return_value=(True, "", 1)), \
                patch("eneru.remote_health.run_loopback_identity_probe", side_effect=probe), \
                patch.object(cc, "_run", return_value=(0, "", "")):
            cc.probe_remote(Config(), srv2)
        assert seen["expected"] == "set"

    def test_disabled(self, env):
        out, rp = self.run_probe(env, server(enabled=False))
        assert out[0].level == "info"
        rp.assert_not_called()

    def test_no_ssh(self, env):
        env.bins = set()
        out, _ = self.run_probe(env, server())
        assert "'ssh' client" in out[0].message

    def test_unsafe_probe_falls_back_to_true(self, env):
        cfg = Config()
        cfg.remote_health.probe_command = "true; reboot"
        _, rp = self.run_probe(env, server(), config=cfg)
        assert rp.call_args.args[1] == "true"

    def test_dangling_option_value_error(self, env):
        with patch("eneru.remote_health.run_remote_probe",
                   side_effect=ValueError("dangling -i")):
            out = cc.probe_remote(Config(), server())
        assert out[0].message.endswith("dangling -i")

    def test_ssh_fails(self, env):
        out, _ = self.run_probe(env, server(), reach=(False, "Permission denied", 5))
        assert out[-1].level == "error" and "Permission denied" in out[-1].message

    def test_all_ok_with_loopback(self, env):
        srv = server(is_host_loopback=True, pre_shutdown_commands=[
            RemoteCommandConfig(action="sync")])
        out, _ = self.run_probe(env, srv)
        assert all(f.level == "ok" for f in out)
        assert "host identity matches" in joined(out)

    def test_identity_mismatch(self, env):
        out, _ = self.run_probe(env, server(is_host_loopback=True),
                                identity=(False, "host identity mismatch", 1))
        assert "host identity mismatch" in msgs(out, "error")[0]

    def test_notes_and_failures(self, env):
        srv = server(user="bob", use_sudo=True, shutdown_command="synoshutdown -s",
                     pre_shutdown_commands=[
                         RemoteCommandConfig(command="systemctl stop x"),
                         RemoteCommandConfig(action="stop_vms"),
                         RemoteCommandConfig()])

        def script_out(checks):
            lines = []
            for i, c in enumerate(checks):
                if c.binary == "synoshutdown":
                    lines.append(f"__ENERU_CHECK__ {i} 1 ")  # missing -> mute sudo
                elif c.kind == "run":
                    lines.append(f"__ENERU_CHECK__ {i} 1 error: failed to connect")
                elif c.kind == "sudo":
                    lines.append(f"__ENERU_CHECK__ {i} 1 sudo: a password is required")
                else:
                    lines.append(f"__ENERU_CHECK__ {i} 0 /bin/x")
            return 0, "\n".join(lines), ""
        out, _ = self.run_probe(env, srv, script_out=script_out)
        text = joined(out)
        assert "sudo refuses it without a password: sudo allows 'systemctl stop x'" in text
        assert "'synoshutdown' is NOT installed" in text
        assert "sudo allows 'synoshutdown'" not in text  # muted
        assert "failed: listing running VMs works (error: failed to connect)" in text
        assert "only its binary was checked" in text

    def test_sudo_refused(self, env):
        srv = server(shutdown_command="sudo shutdown -h now")

        def script_out(checks):
            return 0, "\n".join(
                f"__ENERU_CHECK__ {i} {1 if c.kind == 'sudo' else 0} "
                for i, c in enumerate(checks)), ""
        out, _ = self.run_probe(env, srv, script_out=script_out)
        assert "sudo refuses it without a password" in joined(out)

    def test_missing_result_row(self, env):
        out, _ = self.run_probe(env, server(),
                                script_out=lambda c: (0, "__ENERU_CHECK__ 0 0 x", ""))
        assert "(no result)" in joined(out)

    def test_script_did_not_run(self, env):
        out, _ = self.run_probe(env, server(),
                                script_out=lambda c: (255, "", "conn reset"))
        assert "did not run: conn reset" in msgs(out, "error")[0]
        out, _ = self.run_probe(env, server(),
                                script_out=lambda c: (255, "", ""))
        assert "exit 255" in msgs(out, "error")[0]

    def test_no_checks(self, env):
        with patch.object(cc, "remote_checks", return_value=([], [])):
            out, _ = self.run_probe(env, server())
        assert [f.level for f in out] == ["ok"]

    def test_loopback_unmount_uses_owner_mounts(self, env):
        cfg = build({"ups": {"name": "u@h"},
                     "filesystems": {"unmount": {"enabled": True,
                                                 "mounts": ["/mnt/a"]}}})
        srv = server(is_host_loopback=True, pre_shutdown_commands=[
            RemoteCommandConfig(action="unmount_filesystems")])
        checks, _ = cc.remote_checks(cfg, srv)
        assert any("/mnt/a" in c.label for c in checks)
        assert cc._remote_owner_mounts(build(multi({"name": "a@h"})), srv) == []


# ---------------------------------------------------------------------------
# Local probe + aggregation
# ---------------------------------------------------------------------------

class TestProbeLocal:
    def test_skips_without_owner_or_when_delegated(self, env):
        assert cc.probe_local(build(multi({"name": "a@h"}))) == []
        with patch("eneru.runtime._uses_loopback_delegate", return_value=True):
            assert cc.probe_local(build({"ups": {"name": "u@h"},
                                         "virtual_machines": {"enabled": True}})) == []

    def test_all_branches(self, env, tmp_path):
        cfg = build({"ups": {"name": "u@h"},
                     "virtual_machines": {"enabled": True},
                     "containers": {"enabled": True, "runtime": "auto",
                                    "compose_files": [str(tmp_path / "nope.yml")]},
                     "filesystems": {"unmount": {"enabled": True,
                                                 "mounts": ["/", str(tmp_path)]}}})
        calls = {"n": 0}

        def run(cmd, timeout=30, **_):
            calls["n"] += 1
            return {"virsh": (0, "vm1\n\nvm2\n", ""),
                    "podman": (1, "", "boom")}[cmd[0]]
        with patch.object(cc, "_run", side_effect=run):
            out = cc.probe_local(cfg)
        text = joined(out)
        assert "virsh works: 2 VM(s) running" in text
        assert "podman ps failed: boom" in text
        assert "'podman compose' is not available" in text
        assert "compose file not found" in text
        assert "/ is mounted" in text and "is not a mount point" in text

    def test_virsh_fail_docker_ok(self, env):
        env.bins = ALL_BINS - {"podman"}
        cfg = build({"ups": {"name": "u@h"},
                     "virtual_machines": {"enabled": True},
                     "containers": {"enabled": True, "runtime": "docker",
                                    "compose_files": ["/etc/hostname"]}})

        def run(cmd, timeout=30, **_):
            if cmd[0] == "virsh":
                return 1, "", ""
            return 0, "", ""
        with patch.object(cc, "_run", side_effect=run):
            out = cc.probe_local(cfg)
        text = joined(out)
        assert "virsh list failed: 1" in text
        assert "docker ps works" in text
        assert "compose" not in text

    def test_single_entry_list_falls_back_to_only_group(self, env):
        # A one-entry `ups:` list is NOT multi-UPS; its lone group is treated
        # as the local owner even without is_local, like the daemon does.
        cfg = build({"ups": [{"name": "solo@h",
                              "virtual_machines": {"enabled": True}}]})
        assert not cfg.multi_ups and not cfg.ups_groups[0].is_local
        with patch.object(cc, "_run", return_value=(0, "", "")):
            out = cc.probe_local(cfg)
        assert msgs(out) == ["virsh works: 0 VM(s) running"]

    def test_no_runtime_present(self, env):
        env.bins = {"upsc"}
        cfg = build({"ups": {"name": "u@h"}, "containers": {"enabled": True}})
        assert cc.probe_local(cfg) == []


class TestProbeFindings:
    def test_order_and_crash(self, env):
        cfg = build(multi({"name": "b@h"}, {"name": "a@h", "is_local": True,
                           "remote_servers": [{"name": "r", "host": "h",
                                               "user": "u", "enabled": True}]}))
        with patch.object(cc, "probe_ups", return_value=[cc.Finding("ok", "ups", "U")]), \
                patch.object(cc, "probe_local", side_effect=RuntimeError("bad")), \
                patch.object(cc, "probe_remote",
                             return_value=[cc.Finding("ok", "remote", "R")]):
            out = cc.probe_findings(cfg)
        assert cfg.multi_ups
        assert msgs(out) == ["U", "U", "probe crashed: bad", "R"]


# ---------------------------------------------------------------------------
# Power-loss preview
# ---------------------------------------------------------------------------

class TestPlan:
    def test_single_dry_run(self, env):
        lines = cc.power_loss_plan(build({
            "ups": {"name": "u@h"}, "behavior": {"dry_run": True},
            "virtual_machines": {"enabled": True, "max_wait": 45},
            "triggers": {"extended_time": {"enabled": False}},
            "remote_servers": [
                {"name": "a", "host": "a", "user": "u", "enabled": True,
                 "shutdown_order": 1},
                {"name": "b", "host": "b", "user": "u", "enabled": True,
                 "shutdown_order": 1}]}))
        text = "\n".join(lines)
        assert lines[0].startswith("DRY-RUN")
        assert "protects THIS host" in text
        assert "on battery" not in text.split("Starts when:")[1].split("\n")[0]
        assert "[parallel]" in text
        assert "Worst case" in text

    def test_multi_ups_coordinator_handoff(self, env):
        cfg = build(multi({"name": "a@h", "is_local": True,
                           "virtual_machines": {"enabled": True}},
                          {"name": "b@h", "remote_servers": [
                              {"name": "r", "host": "h", "user": "u",
                               "enabled": True}]},
                          local_shutdown={"trigger_on": "none"}))
        assert cfg.multi_ups
        text = "\n".join(cc.power_loss_plan(cfg))
        local, remote = text.split("UPS b@h")
        assert "UPS a@h (protects THIS host)" in local
        assert "Group handoff" in local and "coordinator" in local
        assert "(monitoring / remote-only)" in remote
        assert "Group handoff" not in remote
        assert "Remote servers" in remote

    def test_multi_ups_trigger_on_any_handoff(self, env):
        cfg = build(multi({"name": "a@h"}, {"name": "b@h"}))
        text = "\n".join(cc.power_loss_plan(cfg))
        # No is_local group + trigger_on: any -> every group hands off.
        assert text.count("Group handoff") == 2

    def test_redundancy_and_advisory(self, env):
        cfg = build({
            "ups": [{"name": "a@h"}, {"name": "b@h"}],
            "local_shutdown": {"trigger_on": "none"},
            "redundancy_groups": [{
                "name": "", "ups_sources": ["a@h", "b@h"], "min_healthy": 1,
                "remote_servers": [{"name": "r", "host": "h", "user": "u",
                                    "enabled": True}]}]})
        text = "\n".join(cc.power_loss_plan(cfg))
        assert "monitoring / remote-only" in text
        assert "advisory: redundancy group '(unnamed)'" in text
        assert "Redundancy group (unnamed)" in text
        assert "fewer than 1 of 2 UPS" in text
        assert "notify only" in text
        assert "Note:" in text


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRender:
    def report(self):
        return cc.CheckReport(path="/x.yaml", findings=[
            cc.Finding("ok", "file", "fine"),
            cc.Finding("error", "ups", "broken", "fix it"),
            cc.Finding("warning", "safety", "hmm"),
            cc.Finding("info", "remote", "fyi"),
        ], plan=["UPS x", "  1. step"])

    def test_plain(self, env):
        text = cc.format_report(self.report())
        assert "File: /x.yaml" in text
        assert "✗ ERROR broken" in text and "-> fix it" in text
        assert "What happens on power loss" in text
        assert "1 error(s), 1 warning(s), 1 check(s) passed" in text
        assert "\033[" not in text

    def test_color_quiet(self, env):
        text = cc.format_report(self.report(), color=True, verbose=False)
        assert "\033[31m" in text and "fine" not in text

    def test_summary_colors(self, env):
        warn = cc.format_report(cc.CheckReport(findings=[
            cc.Finding("warning", "file", "w")]), color=True)
        assert "\033[33mSummary" in warn and "refuse" not in warn
        ok = cc.format_report(cc.CheckReport(), color=True)
        assert "\033[32mSummary" in ok and "File:" not in ok

    def test_use_color(self, monkeypatch):
        tty = MagicMock()
        tty.isatty.return_value = True
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert cc.use_color(tty)
        monkeypatch.setenv("NO_COLOR", "1")
        assert not cc.use_color(tty)
        monkeypatch.delenv("NO_COLOR")
        assert not cc.use_color(object())
        assert cc.use_color() in (True, False)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

class TestCli:
    def test_config_path_arg(self, tmp_path, capsys):
        assert cli._config_path_arg(argparse.Namespace(config="/x"),
                                    must_exist=True) == "/x"
        existing = tmp_path / "config.yaml"
        existing.write_text("")
        with patch.object(cli.ConfigLoader, "DEFAULT_CONFIG_PATHS",
                          [tmp_path / "nope.yaml", existing]):
            assert cli._config_path_arg(argparse.Namespace(),
                                        must_exist=True) == str(existing)
        with patch.object(cli.ConfigLoader, "DEFAULT_CONFIG_PATHS",
                          [tmp_path / "a.yaml", tmp_path / "b.yaml"]):
            assert cli._config_path_arg(argparse.Namespace(config=None),
                                        must_exist=True) is None
            assert "no config file found" in capsys.readouterr().err
            assert cli._config_path_arg(argparse.Namespace(config=None),
                                        must_exist=False) == str(tmp_path / "a.yaml")

    def test_cmd_config_check(self, tmp_path, env, capsys):
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump({"ups": {"name": "u@h"}}))
        args = argparse.Namespace(config=str(p), offline=True, quiet=False)
        with pytest.raises(SystemExit) as exc:
            cli._cmd_config_check(args)
        assert exc.value.code == 0
        assert "configuration check" in capsys.readouterr().out
        args = argparse.Namespace(config=str(tmp_path / "missing.yaml"),
                                  offline=True, quiet=True)
        with pytest.raises(SystemExit) as exc:
            cli._cmd_config_check(args)
        assert exc.value.code == 1

    def test_cmd_config_check_live_flag(self, tmp_path, env):
        p = tmp_path / "c.yaml"
        p.write_text("{}")
        with patch.object(cc, "check_file",
                          return_value=cc.CheckReport()) as cf, \
                pytest.raises(SystemExit):
            cli._cmd_config_check(argparse.Namespace(config=str(p),
                                                     offline=False, quiet=True))
        assert cf.call_args.kwargs["probes"] is True

    def test_cmd_config_check_no_default(self, tmp_path):
        with patch.object(cli.ConfigLoader, "DEFAULT_CONFIG_PATHS",
                          [tmp_path / "a.yaml"]), pytest.raises(SystemExit) as exc:
            cli._cmd_config_check(argparse.Namespace(config=None, offline=True,
                                                     quiet=False))
        assert exc.value.code == 1

    @staticmethod
    def _tty(monkeypatch, value=True):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: value, raising=False)

    def test_edit_requires_tty(self, monkeypatch, tmp_path, capsys):
        self._tty(monkeypatch, False)
        with pytest.raises(SystemExit) as exc:
            cli._cmd_config_edit(argparse.Namespace(config=str(tmp_path / "c.yaml")))
        assert exc.value.code == 2
        assert "interactive terminal" in capsys.readouterr().err

    def test_edit_import_error(self, monkeypatch, tmp_path, capsys):
        self._tty(monkeypatch)
        with patch.dict(sys.modules, {"eneru.config_doc": None}), \
                pytest.raises(SystemExit) as exc:
            cli._cmd_config_edit(argparse.Namespace(config=str(tmp_path / "c.yaml")))
        assert exc.value.code == 1
        assert "ruamel.yaml" in capsys.readouterr().err

    def test_edit_load_failure(self, monkeypatch, tmp_path, capsys):
        self._tty(monkeypatch)
        p = tmp_path / "c.yaml"
        p.write_text("- not\n- a mapping\n")
        with pytest.raises(SystemExit) as exc:
            cli._cmd_config_edit(argparse.Namespace(config=str(p)))
        assert exc.value.code == 1
        assert "cannot open" in capsys.readouterr().err

    @pytest.mark.parametrize("flags,expected", [
        ({}, None), ({"basic": True}, "basic"), ({"advanced": True}, "advanced"),
    ])
    def test_edit_new_file_seeded(self, monkeypatch, tmp_path, flags, expected):
        self._tty(monkeypatch)
        from eneru import config_tui
        seen = {}

        def fake_run(doc, mode):
            seen["doc"], seen["mode"] = doc, mode
            return 0
        monkeypatch.setattr(config_tui, "run_editor", fake_run)
        args = argparse.Namespace(config=str(tmp_path / "new.yaml"), **flags)
        with pytest.raises(SystemExit) as exc:
            cli._cmd_config_edit(args)
        assert exc.value.code == 0
        assert seen["mode"] == expected
        assert seen["doc"].get(("behavior", "dry_run")) is True

    def test_edit_existing_not_seeded(self, monkeypatch, tmp_path):
        self._tty(monkeypatch)
        from eneru import config_tui
        p = tmp_path / "c.yaml"
        p.write_text("ups:\n  name: a@b\n")
        seen = {}
        monkeypatch.setattr(config_tui, "run_editor",
                            lambda doc, mode: seen.setdefault("doc", doc) and 0)
        with pytest.raises(SystemExit):
            cli._cmd_config_edit(argparse.Namespace(config=str(p)))
        assert not seen["doc"].has(("behavior",))

    def test_argparse_wiring(self, monkeypatch, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("{}")
        called = {}
        monkeypatch.setattr(cli, "_cmd_config_check",
                            lambda a: called.setdefault("check", a))
        monkeypatch.setattr(cli, "_cmd_config_edit",
                            lambda a: called.setdefault("edit", a))
        monkeypatch.setattr(sys, "argv", ["eneru", "config", "-c", str(p),
                                          "check", "--offline"])
        cli.main()
        assert called["check"].config == str(p)
        assert called["check"].offline is True
        monkeypatch.setattr(sys, "argv", ["eneru", "config", "--advanced"])
        cli.main()
        assert called["edit"].advanced is True and called["edit"].config is None


# ---------------------------------------------------------------------------
# Code-review fixes: sudo args, per-mount checks, compose fallback, clean()
# ---------------------------------------------------------------------------

def _run_sh(script, env=None):
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True,
                          text=True, env=env)


class TestSudoArgs:
    def test_sudo_allowed_pins_arguments(self):
        chk = cc._sudo_allowed("umount", ["-l", "/mnt/a b"])
        assert chk.script == "sudo -n -l umount -l '/mnt/a b'"
        assert chk.kind == "sudo" and chk.binary == "umount"
        assert "'umount -l /mnt/a b'" in chk.label
        assert cc._sudo_allowed("sync").script == "sudo -n -l sync"

    def test_final_command_passes_its_args(self):
        checks, _ = cc.command_checks("sudo -i synoshutdown -s", False,
                                      final=True, user="admin")
        assert [c.script for c in checks if c.kind == "sudo"] == [
            "sudo -n -l synoshutdown -s"]

    def test_use_sudo_final_passes_args(self):
        checks, _ = cc.command_checks("shutdown -h now", True, final=True,
                                      user="bob")
        assert checks[-1].script == "sudo -n -l shutdown -h now"

    def test_custom_explicit_sudo_passes_args(self):
        checks, notes = cc.command_checks("sudo -n systemctl stop app", True)
        assert checks[-1].script == "sudo -n -l systemctl stop app"
        assert not [n for n in notes if "first command" in n]


class TestUnmountChecks:
    MOUNTS = [{"path": "/mnt/a b", "options": "-l -f"}, "/mnt/x",
              {"path": ""}, {"path": "  "}, 5, {"path": "/mnt/y", "options": None}]

    def test_per_mount_with_sudo(self):
        checks = cc.action_checks("unmount_filesystems", True, mounts=self.MOUNTS)
        assert [(c.kind, c.script.startswith("sudo") and c.script) for c in checks] == [
            ("exists", False),
            ("run", False), ("sudo", "sudo -n -l umount -l -f '/mnt/a b'"),
            ("run", False), ("sudo", "sudo -n -l umount /mnt/x"),
            ("run", False), ("sudo", "sudo -n -l umount /mnt/y"),
        ]

    def test_per_mount_without_sudo(self):
        checks = cc.action_checks("unmount_filesystems", False, mounts=self.MOUNTS)
        assert [c.kind for c in checks] == ["exists", "run", "run", "run"]

    def test_mount_paths_skips_blank_and_non_str(self):
        assert cc._mount_paths(self.MOUNTS) == ["/mnt/a b", "/mnt/x", "/mnt/y"]
        assert cc._mount_paths(None) == []

    def test_regular_remote_without_mounts_warns(self, env):
        srv = server(pre_shutdown_commands=[
            RemoteCommandConfig(action="unmount_filesystems")])
        _, notes = cc.remote_checks(Config(), srv)
        assert any("no `mounts` listed" in n for n in notes)
        out, _ = TestProbeRemote().run_probe(env, srv)
        assert [f for f in out if f.level == "warning"
                and "no `mounts` listed" in f.message]

    def test_regular_remote_with_mounts_no_note(self):
        srv = server(pre_shutdown_commands=[RemoteCommandConfig(
            action="unmount_filesystems", mounts=[{"path": "/mnt/a", "options": ""}])])
        checks, notes = cc.remote_checks(Config(), srv)
        assert not [n for n in notes if "mounts" in n]
        assert any(c.label == "/mnt/a is mounted" for c in checks)

    def _loopback_cfg(self, enabled):
        cfg = build({"ups": {"name": "u@h"}, "filesystems": {"unmount": {
            "enabled": enabled, "mounts": ["/mnt/local"]}}})
        srv = server(is_host_loopback=True, pre_shutdown_commands=[
            RemoteCommandConfig(action="unmount_filesystems")])
        return cfg, srv

    def test_loopback_uses_owner_mounts_only_when_enabled(self, env):
        cfg, srv = self._loopback_cfg(True)
        checks, notes = cc.remote_checks(cfg, srv)
        assert any(c.label == "/mnt/local is mounted" for c in checks)
        assert not notes or not [n for n in notes if "mounts" in n]
        cfg, srv = self._loopback_cfg(False)
        checks, _ = cc.remote_checks(cfg, srv)
        assert not any("is mounted" in c.label for c in checks)

    def test_loopback_without_owner(self, env):
        cfg = build(multi({"name": "a@h"}, {"name": "b@h"}))
        srv = server(is_host_loopback=True)
        assert cc._remote_owner_mounts(cfg, srv) == []


class TestProcMounts:
    def test_escape(self):
        assert cc._proc_mounts_escape("/a b\\c\td\ne") == \
            "/a\\040b\\134c\\011d\\012e"

    @pytest.mark.skipif(not os.path.exists("/proc/mounts"), reason="Linux only")
    def test_mounted_script_against_proc_mounts(self):
        assert _run_sh(cc._mounted("/proc").script).returncode == 0
        assert _run_sh(cc._mounted("/definitely not mounted").script).returncode != 0

    def test_mounted_matches_escaped_space(self, tmp_path):
        fake = tmp_path / "mounts"
        fake.write_text("nas:/x /mnt/my\\040share nfs rw 0 0\n")
        script = cc._mounted("/mnt/my share").script.replace(
            "/proc/mounts", str(fake))
        assert _run_sh(script).returncode == 0
        script = cc._mounted("/mnt/my").script.replace("/proc/mounts", str(fake))
        assert _run_sh(script).returncode != 0


class TestComposeFallback:
    def _shims(self, tmp_path, docker_ok, podman):
        d = tmp_path / "bin"
        d.mkdir()
        if docker_ok is not None:
            (d / "docker").write_text(
                "#!/bin/sh\n"
                + ("echo 'Docker Compose version v2'\nexit 0\n" if docker_ok
                   else "exit 1\n"))
        if podman:
            (d / "podman").write_text("#!/bin/sh\necho 'podman-compose 1.0'\n")
        for f in d.iterdir():
            f.chmod(0o755)
        return {"PATH": str(d)}

    def _compose_check(self):
        checks = cc.action_checks("stop_compose", False, path="/x.yml")
        return [c for c in checks if c.label == "compose works"][0]

    def test_docker_plugin_preferred(self, tmp_path):
        r = _run_sh(self._compose_check().script, self._shims(tmp_path, True, True))
        assert r.returncode == 0 and "Docker Compose" in r.stdout

    def test_falls_back_to_podman_compose(self, tmp_path):
        r = _run_sh(self._compose_check().script, self._shims(tmp_path, False, True))
        assert r.returncode == 0 and "podman-compose" in r.stdout

    def test_neither(self, tmp_path):
        r = _run_sh(self._compose_check().script, self._shims(tmp_path, None, False))
        assert r.returncode == 127 and "no docker compose" in r.stdout

    def test_use_sudo_prefixes_compose(self):
        checks = cc.action_checks("stop_compose", True, path="/x.yml")
        script = [c for c in checks if c.label == "compose works"][0].script
        assert "sudo -n docker compose" in script and "sudo -n podman compose" in script


class TestClean:
    def test_strips_escapes_and_controls(self):
        assert cc.clean("\x1b[31mred\x1b[0m\x07bell\ttab \x1b]x") == "red bell tab x"
        assert cc.clean(5) == "5"

    def test_remote_findings_are_cleaned(self, env):
        out, _ = TestProbeRemote().run_probe(
            env, server(), reach=(False, "\x1b[2Jpwned\x07", 5))
        assert "\x1b" not in joined(out) and "\x07" not in joined(out)
        assert "pwned" in joined(out)

    def test_nut_findings_are_cleaned(self, env):
        def run(cmd, timeout=30, env_overrides=None, **_):
            return 1, "", "\x1b[31mconn refused\x1b[0m"
        config = build({"ups": {"name": "ups@h"}})
        with patch.object(cc, "_run", run):
            out = cc.probe_ups(config, config.ups_groups[0])
        assert "\x1b" not in out[0].message and "conn refused" in out[0].message


class TestProbeLocalPrivileges:
    def _cfg(self, tmp_path):
        return build({"ups": {"name": "u@h"},
                      "virtual_machines": {"enabled": True},
                      "containers": {"enabled": True, "runtime": "docker",
                                     "compose_files": [str(tmp_path / "nope.yml")]}})

    def _run(self, cmd, timeout=30, **_):
        return {"virsh": (1, "", "\x1b[1mdenied"),
                "docker": (0, "", "")}[cmd[0]]

    def test_non_root_downgrades_to_warning(self, env, tmp_path):
        env.euid = 1000
        with patch.object(cc, "_run", side_effect=self._run):
            out = cc.probe_local(self._cfg(tmp_path))
        virsh = [f for f in out if "virsh list failed" in f.message][0]
        compose = [f for f in out if "compose file not found" in f.message][0]
        for f in (virsh, compose):
            assert f.level == "warning" and "checked as non-root" in f.message
        assert "\x1b" not in virsh.message
        # compose version succeeded -> no "not available" warning
        assert not [f for f in out if "is not available" in f.message]

    def test_root_keeps_errors(self, env, tmp_path):
        env.euid = 0
        with patch.object(cc, "_run", side_effect=self._run):
            out = cc.probe_local(self._cfg(tmp_path))
        virsh = [f for f in out if "virsh list failed" in f.message][0]
        compose = [f for f in out if "compose file not found" in f.message][0]
        assert virsh.level == compose.level == "error"
        assert "non-root" not in virsh.message


class TestExtraBranches:
    def test_runtime_not_numeric_and_anonymous_list_fails(self, env):
        vars_ = UPSC_OK.replace("battery.runtime: 1800", "battery.runtime: n/a")
        config = build({"ups": {"name": "ups@h"}})
        with patch.object(cc, "_run", fake_run(vars_=vars_)), \
                patch("eneru.nut_control.list_commands",
                      return_value=(False, [], "denied")):
            out = cc.probe_ups(config, config.ups_groups[0])
        text = joined(out)
        assert "at or below" not in text and "anonymously" not in text

    def test_budget_within_threshold(self, env):
        cfg = build({"ups": {"name": "u@h"},
                     "triggers": {"critical_runtime_threshold": 100000},
                     "virtual_machines": {"enabled": True, "max_wait": 30}})
        assert cc._budget_findings(cfg) == []

    def test_dependencies_found_runtime_and_nonlocal(self, env):
        cfg = build(multi({"name": "a@h", "is_local": True,
                           "containers": {"enabled": True}},
                          {"name": "b@h"}))
        assert not [f for f in cc._dependency_findings(cfg)
                    if "no container runtime" in f.message]


def test_probe_local_containers_without_runtime(env, tmp_path):
    env.bins = {"upsc"}
    cfg = build({"ups": {"name": "u@h"},
                 "containers": {"enabled": True, "runtime": "docker"}})
    with patch.object(cc, "_run", side_effect=AssertionError("no calls")):
        assert cc.probe_local(cfg) == []


def test_probe_local_runtime_without_compose_files(env):
    cfg = build({"ups": {"name": "u@h"},
                 "containers": {"enabled": True, "runtime": "docker"}})
    with patch.object(cc, "_run", return_value=(0, "", "")) as run:
        out = cc.probe_local(cfg)
    assert [c.args[0] for c in run.call_args_list] == [["docker", "ps", "-q"]]
    assert msgs(out) == ["docker ps works"]


class TestCubicRound:
    def test_budget_covers_redundancy_groups(self, env):
        data = multi({"name": "A@h"}, {"name": "B@h"}, redundancy_groups=[{
            "name": "rack", "ups_sources": ["A@h", "B@h"], "min_healthy": 1,
            "triggers": {"critical_runtime_threshold": 10},
            "remote_servers": [{"name": "n1", "enabled": True, "host": "x",
                                "user": "root", "command_timeout": 120}]}])
        out = cc._budget_findings(build(data))
        assert any("redundancy group 'rack'" in f.message for f in out)

    def test_rootless_check_proves_sudo_u_podman(self):
        checks = cc.action_checks("stop_containers_rootless", False)
        script = checks[-1].script
        assert "sudo -n -u \"$u\" podman ps -q" in script
        assert "$1+0 >= 1000" in script

    def test_rootless_script_runs_locally(self, tmp_path):
        # loginctl lists one regular user; sudo refuses -> the check fails.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "loginctl").write_text("#!/bin/sh\necho '1000 alice'\n")
        (bin_dir / "sudo").write_text("#!/bin/sh\nexit 1\n")
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        script = cc.action_checks("stop_containers_rootless", False)[-1].script
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                           env=env)
        assert r.returncode != 0 and "refused for alice" in r.stdout
        (bin_dir / "sudo").write_text("#!/bin/sh\nexit 0\n")
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                           env=env)
        assert r.returncode == 0

    def test_incomplete_nut_login_warns(self, env):
        from eneru.config import NutControlConfig
        config = build({"ups": {"name": "ups@h"}})
        group = config.ups_groups[0]
        config.nut_control = NutControlConfig(username="mon", password="")

        def run(cmd, timeout=10, env_overrides=None):
            if cmd[:2] == ["upsc", "-l"]:
                return 0, "ups\n", ""
            return 0, "ups.status: OL\nbattery.charge: 100\nbattery.runtime: 900\n", ""
        with patch.object(cc, "_run", side_effect=run), \
                patch("eneru.nut_control.list_commands", return_value=(True, ["a"], "")):
            out = cc.probe_ups(config, group)
        assert any("NUT login is incomplete" in f.message and f.level == "warning"
                   for f in out)


class TestQuoteAwareCommands:
    def test_quoted_operators_stay_in_the_argument(self):
        assert cc.command_binary("sudo -n sh -c 'a; b'") == ("sh", True, ["-c", "a; b"])
        checks, notes = cc.command_checks("sudo -n sh -c 'systemctl stop a; systemctl stop b'", True)
        assert [c.kind for c in checks] == ["exists", "sudo"]
        assert not [n for n in notes if "first command" in n]

    def test_unquoted_operator_ends_the_first_command(self):
        assert cc.first_command_tokens("a b && c") == (["a", "b"], True)
        assert cc.first_command_tokens("a 'b|c'") == (["a", "b|c"], False)
        assert cc.first_command_tokens("echo 'open") == (None, False)


class TestLoopbackKeyIsFatal:
    def test_missing_default_loopback_key_is_an_error_like_eneru_run(self, env):
        # A legacy single-UPS config is always local; in a container without
        # the default loopback key `eneru run` exits, so the check must say
        # ERROR (it used to downgrade the synthesis message to WARN).
        env.runtime = "container (Docker)"
        env.euid = 10001
        config = build({"ups": {"name": "ups@h"}})
        # Synthesis probes the default key with Path.stat(); fail exactly
        # that path (host-independent), leave every other stat() alone.
        real_stat = Path.stat
        key = cli._LOOPBACK_DEFAULT_SSH_KEY_PATH

        def fake_stat(self, *a, **k):
            if str(self) == key:
                raise FileNotFoundError(2, "No such file", key)
            return real_stat(self, *a, **k)
        with patch.object(Path, "stat", fake_stat):
            out = cc.static_findings(config, {"ups": {"name": "ups@h"}})
        hits = [f for f in out if "SSH key for the host-loopback delegate" in f.message]
        assert hits and all(f.level == "error" for f in hits)
        assert "refuses to start" in hits[0].hint


class TestSecondReviewRound:
    def test_sudo_env_assignments_are_skipped(self):
        assert cc.command_binary("sudo -n LANG=C FOO=1 shutdown -h now") == \
            ("shutdown", True, ["-h", "now"])
        assert cc.command_binary("sudo LANG=C") == (None, True, [])

    def test_sudo_run_as_target_is_kept_in_the_probe(self):
        checks, _ = cc.command_checks("sudo -u deploy -g ops tool --x", False)
        assert checks[-1].script == "sudo -n -u deploy -g ops -l tool --x"
        checks, _ = cc.command_checks("sudo --user=deploy tool", False)
        assert checks[-1].script == "sudo -n --user=deploy -l tool"
        assert cc.sudo_target_opts("tool") == []
        assert cc.sudo_target_opts("") == []
        assert cc.sudo_target_opts("A=1 sudo -u x t") == ["-u", "x"]

    def test_plan_lines_are_sanitised_in_the_report(self):
        r = cc.CheckReport(plan=["cmd \x1b[2Jevil\x07"])
        out = cc.format_report(r)
        assert "\x1b" not in out and "\x07" not in out and "evil" in out

    def test_bcrypt_needed_when_auth_auto_enables(self, env, tmp_path):
        # Mirrors the daemon (eneru.auth.auth_is_active): unset auth turns on
        # only once the auth DB holds a user; an empty DB keeps it off.
        from eneru.auth import AuthStore
        db = tmp_path / "auth.db"
        data = {"ups": {"name": "u@h"},
                "api": {"enabled": True, "auth": {"db_path": str(db)}}}
        config = build(data)
        assert not config.api.auth.enabled_explicitly_set  # tristate: auto
        store = AuthStore(str(db))
        real_import = __import__

        def no_bcrypt(name, *a, **k):
            if name == "bcrypt":
                raise ImportError("no bcrypt")
            return real_import(name, *a, **k)
        with patch("builtins.__import__", side_effect=no_bcrypt):
            out = cc._optional_module_findings(config)
        assert not any("bcrypt" in f.message for f in out)  # DB but no user
        store.create_user("admin", "a-long-enough-password")
        with patch("builtins.__import__", side_effect=no_bcrypt):
            out = cc._optional_module_findings(config)
        assert any("bcrypt" in f.message for f in out)

    def test_load_raw_reads_utf8_regardless_of_locale(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text('notifications:\n  title: "\U0001F3E2 Lab"\n', encoding="utf-8")
        real_open = open

        def latin1_default(path, mode="r", *a, encoding=None, **k):
            return real_open(path, mode, *a, encoding=encoding or "latin-1", **k)
        with patch("builtins.open", side_effect=latin1_default):
            data, _ = cc.load_raw(str(p))
        assert data["notifications"]["title"] == "\U0001F3E2 Lab"


class TestSudoOptionForms:
    @pytest.mark.parametrize("cmd,binary,target", [
        ("sudo -udeploy tool a", "tool", ["-u", "deploy"]),
        ("sudo -nu deploy tool a", "tool", ["-u", "deploy"]),
        ("sudo --user deploy tool a", "tool", ["--user", "deploy"]),
        ("sudo --group=ops tool a", "tool", ["--group=ops"]),
        ("sudo -R /x tool a", "tool", []),
        ("sudo -gops -n tool a", "tool", ["-g", "ops"]),
        ("sudo -nk tool a", "tool", []),
    ])
    def test_forms(self, cmd, binary, target):
        assert cc.command_binary(cmd)[0] == binary
        assert cc.sudo_target_opts(cmd) == target

    def test_env_assignment_under_sudo_is_noted(self):
        _, notes = cc.command_checks("sudo -n LANG=C tool", False)
        assert any("environment variables" in n for n in notes)
        _, notes = cc.command_checks("sudo -n tool", False)
        assert not any("environment variables" in n for n in notes)


def test_path_findings_skip_wrongly_typed_paths(env):
    # Regression: `statistics.db_directory: 123` crashed the whole check as root.
    config = build({"ups": {"name": "u@h"}})
    config.statistics.db_directory = 123
    config.logging.file = ["not", "a", "path"]
    assert isinstance(cc._path_findings(config), list)  # no TypeError
