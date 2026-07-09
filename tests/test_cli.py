"""Tests for CLI argument handling and validation commands."""

import argparse
import json
import pytest
import runpy
import sys
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

from eneru import (
    main, ConfigLoader, __version__, Config, UPSConfig, UPSGroupConfig, MonitorState,
    APIConfig, BehaviorConfig, LoggingConfig, LocalShutdownConfig, VMConfig, ContainersConfig,
    FilesystemsConfig, UnmountConfig, RedundancyGroupConfig, RemoteServerConfig,
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

    @pytest.mark.unit
    def test_python_m_eneru_entrypoint_calls_cli_main(self):
        """``python -m eneru`` must route through the same CLI main."""
        with patch("eneru.cli.main") as cli_main:
            runpy.run_module("eneru.__main__", run_name="__main__")

        cli_main.assert_called_once_with()


class TestCLIRunOverrides:
    """Run-subcommand config overrides."""

    @pytest.mark.unit
    def test_api_bind_and_port_imply_api_enabled(self):
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            api=APIConfig(enabled=False, bind="127.0.0.1", port=9191),
        )
        args = Namespace(
            dry_run=False,
            api=False,
            api_bind="0.0.0.0",
            api_port=9100,
        )

        _apply_run_overrides(config, args)

        assert config.api.enabled is True
        assert config.api.bind == "0.0.0.0"
        assert config.api.port == 9100

    @pytest.mark.unit
    def test_non_root_remote_only_config_allowed(self):
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"),
                is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )

        assert _root_required_reasons(config) == []

    @pytest.mark.unit
    def test_non_root_local_config_reports_root_reasons(self):
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host", display_name="Rack"),
                is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )

        reasons = _root_required_reasons(config)

        assert "UPS group 'Rack' is marked is_local" in reasons
        assert "local_shutdown can power off the Eneru host" in reasons

    @pytest.mark.unit
    def test_api_flag_alone_implies_enabled(self):
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            api=APIConfig(enabled=False, bind="127.0.0.1", port=9191),
        )
        args = Namespace(dry_run=False, api=True, api_bind=None, api_port=None)

        _apply_run_overrides(config, args)

        assert config.api.enabled is True
        # bind and port unchanged when not provided
        assert config.api.bind == "127.0.0.1"
        assert config.api.port == 9191

    @pytest.mark.unit
    def test_api_bind_alone_implies_enabled(self):
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            api=APIConfig(enabled=False, bind="127.0.0.1", port=9191),
        )
        args = Namespace(dry_run=False, api=False, api_bind="0.0.0.0", api_port=None)

        _apply_run_overrides(config, args)

        assert config.api.enabled is True
        assert config.api.bind == "0.0.0.0"
        assert config.api.port == 9191

    @pytest.mark.unit
    def test_api_port_alone_implies_enabled(self):
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            api=APIConfig(enabled=False, bind="127.0.0.1", port=9191),
        )
        args = Namespace(dry_run=False, api=False, api_bind=None, api_port=9100)

        _apply_run_overrides(config, args)

        assert config.api.enabled is True
        assert config.api.bind == "127.0.0.1"
        assert config.api.port == 9100

    @pytest.mark.unit
    def test_cli_overrides_yaml_api_disabled(self):
        """CLI flags must flip api.enabled True even when YAML had it False.

        This is the container-healthcheck story: image users should not need
        to author or mount a YAML to enable /health.
        """
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            api=APIConfig(enabled=False, bind="127.0.0.1", port=9191),
        )
        args = Namespace(dry_run=False, api=True, api_bind="0.0.0.0", api_port=9191)

        _apply_run_overrides(config, args)

        assert config.api.enabled is True

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_port", ["0", "-1", "65536", "70000", "abc"])
    def test_api_port_argparse_rejects_out_of_range(self, bad_port, capsys):
        """argparse-time validation: --api-port must be 1..65535 integer.

        Catching this at parse time gives a clear error before any config or
        privilege check fires; previously type=int let -1 / 70000 through.
        """
        with patch.object(sys, "argv", ["eneru", "run", "--api-port", bad_port]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        # argparse always exits 2 on parse-time failures.
        assert exc_info.value.code == 2

    @pytest.mark.unit
    def test_port_int_validator_accepts_boundaries(self):
        from eneru.cli import _port_int

        assert _port_int("1") == 1
        assert _port_int("65535") == 65535
        assert _port_int("9191") == 9191


class TestPrivilegeChecks:
    """Root-vs-non-root startup gating."""

    @pytest.mark.unit
    def test_exit_on_privilege_errors_passes_for_root(self):
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )

        with patch("eneru.cli.os.geteuid", return_value=0, create=True):
            _exit_on_privilege_errors(config)  # Must not raise

    @pytest.mark.unit
    def test_exit_on_privilege_errors_passes_when_geteuid_absent(self):
        """Non-Unix platforms (no os.geteuid) skip the check."""
        from eneru.cli import _exit_on_privilege_errors
        import eneru.cli

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )

        # Replace os.geteuid with a sentinel to simulate a platform that
        # doesn't expose it; getattr in cli.py returns None and short-circuits.
        original_geteuid = getattr(eneru.cli.os, "geteuid", None)
        try:
            if original_geteuid is not None:
                del eneru.cli.os.geteuid
            _exit_on_privilege_errors(config)  # Must not raise
        finally:
            if original_geteuid is not None:
                eneru.cli.os.geteuid = original_geteuid

    @pytest.mark.unit
    def test_exit_on_privilege_errors_exits_for_non_root_with_local_features(self, capsys):
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host", display_name="Rack"),
                is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc_info:
                _exit_on_privilege_errors(config)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "must run as root" in out
        assert "is marked is_local" in out

    @pytest.mark.unit
    def test_exit_on_privilege_errors_passes_for_non_root_remote_only(self):
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True):
            _exit_on_privilege_errors(config)  # Must not raise

    @pytest.mark.unit
    def test_eneru_skip_privilege_check_bypass_warns_but_does_not_exit(self, capsys):
        """ENERU_SKIP_PRIVILEGE_CHECK=1 downgrades the fatal check to a warning.

        Used by the E2E workflow so eneru runs as the unprivileged runner
        user (no sudo, no file-ownership churn between tests).
        """
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host", display_name="Rack"),
                is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": "1"}, clear=False):
            _exit_on_privilege_errors(config)  # Must NOT raise

        err = capsys.readouterr().err
        assert "ENERU_SKIP_PRIVILEGE_CHECK" in err
        assert "is marked is_local" in err

    @pytest.mark.unit
    def test_eneru_skip_privilege_check_accepts_true_value(self):
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": "true"}, clear=False):
            _exit_on_privilege_errors(config)  # Must not raise

    @pytest.mark.unit
    def test_eneru_skip_privilege_check_unset_still_exits(self):
        """Empty/unset env var must NOT bypass the check."""
        from eneru.cli import _exit_on_privilege_errors

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": ""}, clear=False):
            with pytest.raises(SystemExit) as exc_info:
                _exit_on_privilege_errors(config)
            assert exc_info.value.code == 1

    @pytest.mark.unit
    @pytest.mark.parametrize("group_kwargs,expected_substring", [
        ({"is_local": True}, "is marked is_local"),
        ({"virtual_machines": VMConfig(enabled=True)}, "virtual_machines enabled"),
        ({"containers": ContainersConfig(enabled=True)}, "containers enabled"),
        ({"filesystems": FilesystemsConfig(unmount=UnmountConfig(enabled=True))},
         "filesystem unmount enabled"),
    ])
    def test_root_required_reasons_per_ups_group_category(self, group_kwargs, expected_substring):
        """Each local feature on a UPS group independently triggers a root-required reason."""
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host", display_name="Rack"),
                **group_kwargs,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        reasons = _root_required_reasons(config)
        assert any(expected_substring in r for r in reasons), reasons

    @pytest.mark.unit
    @pytest.mark.parametrize("group_kwargs,expected_substring", [
        ({"is_local": True}, "is marked is_local"),
        ({"virtual_machines": VMConfig(enabled=True)}, "virtual_machines enabled"),
        ({"containers": ContainersConfig(enabled=True)}, "containers enabled"),
        ({"filesystems": FilesystemsConfig(unmount=UnmountConfig(enabled=True))},
         "filesystem unmount enabled"),
    ])
    def test_root_required_reasons_per_redundancy_group_category(self, group_kwargs, expected_substring):
        """Each local feature on a redundancy group independently triggers a reason."""
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
                **group_kwargs,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        reasons = _root_required_reasons(config)
        assert any(expected_substring in r for r in reasons), reasons

    @pytest.mark.unit
    def test_local_shutdown_dormant_does_not_require_root(self):
        """trigger_on='none' + all-remote groups means local_shutdown never fires.

        Lock this in so a future "simplification" of the privilege check
        doesn't accidentally start requiring root for a dormant configuration.
        """
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="none"),
        )
        reasons = _root_required_reasons(config)
        assert reasons == []

    @pytest.mark.unit
    def test_no_ups_groups_flags_implicit_single_ups_local_mode(self):
        """Empty ups_groups (legacy single-UPS via top-level `ups:` mapping that
        wasn't promoted to a group) is treated as local-host mode."""
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[],  # No groups at all
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        reasons = _root_required_reasons(config)
        assert any("implicit single-UPS local-host mode" in r for r in reasons)

    @pytest.mark.unit
    def test_local_shutdown_with_local_owner_requires_root_even_if_trigger_on_none(self):
        """trigger_on='none' is the multi-UPS knob; with a local-owner group the
        local UPS can still drive the host to shutdown when it goes critical."""
        from eneru.cli import _root_required_reasons

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="none"),
        )
        reasons = _root_required_reasons(config)
        assert any("local_shutdown" in r for r in reasons)


class TestRuntimeContextDetection:
    """`eneru validate` reports the runtime context (container/systemd/bare).

    F-049: _detect_runtime_context is lru_cache-memoized; the autouse
    _reset_runtime_context_cache fixture in conftest.py clears it around every
    test so these per-scenario /proc + env fakes never see a stale cached label.
    """

    @pytest.mark.unit
    def test_dockerenv_marker_detected(self):
        from eneru.cli import _detect_runtime_context

        def fake_exists(self):
            return str(self) == "/.dockerenv"

        with patch("pathlib.Path.exists", new=fake_exists):
            assert _detect_runtime_context() == "container (Docker)"

    @pytest.mark.unit
    def test_podman_containerenv_marker_detected(self):
        from eneru.cli import _detect_runtime_context

        def fake_exists(self):
            return str(self) == "/run/.containerenv"

        with patch("pathlib.Path.exists", new=fake_exists):
            assert _detect_runtime_context() == "container (Podman)"

    @pytest.mark.unit
    def test_container_env_var_detected_known_runtime_capitalized(self):
        from eneru.cli import _detect_runtime_context

        with patch("pathlib.Path.exists", return_value=False), \
             patch.dict("os.environ", {"container": "podman"}, clear=False):
            assert _detect_runtime_context() == "container (Podman)"

    @pytest.mark.unit
    def test_container_env_var_detected_unknown_runtime_passthrough(self):
        from eneru.cli import _detect_runtime_context

        with patch("pathlib.Path.exists", return_value=False), \
             patch.dict("os.environ", {"container": "lxc"}, clear=False):
            assert _detect_runtime_context() == "container (lxc)"

    @pytest.mark.unit
    def test_systemd_invocation_id_detected(self):
        from eneru.cli import _detect_runtime_context

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", side_effect=OSError), \
             patch.dict("os.environ", {"INVOCATION_ID": "abc123", "container": ""}, clear=True):
            assert _detect_runtime_context() == "systemd service"

    @pytest.mark.unit
    def test_bare_process_when_no_signals(self):
        from eneru.cli import _detect_runtime_context

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", side_effect=OSError), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_runtime_context() == "bare process"

    @pytest.mark.unit
    def test_container_takes_precedence_over_systemd(self):
        """A systemd unit running inside a container should be reported as container."""
        from eneru.cli import _detect_runtime_context

        def fake_exists(self):
            return str(self) == "/.dockerenv"

        with patch("pathlib.Path.exists", new=fake_exists), \
             patch.dict("os.environ", {"INVOCATION_ID": "abc"}, clear=False):
            assert _detect_runtime_context() == "container (Docker)"

    @pytest.mark.unit
    def test_cgroup_marker_falls_through_to_generic_container(self):
        """Old Docker on cgroup v1 (no /.dockerenv but cgroup path mentions docker)."""
        from eneru.cli import _detect_runtime_context

        def fake_read(self, *args, **kwargs):
            if str(self) == "/proc/1/cgroup":
                return "12:cpu:/docker/abc123\n"
            raise OSError

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", new=fake_read), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_runtime_context() == "container"

    @pytest.mark.unit
    def test_mountinfo_with_docker_path_detects_container(self):
        """Modern Docker on cgroup v2 + cgroupns: cgroup is collapsed but
        mountinfo still has /docker/containers/<id> bind-mount paths."""
        from eneru.cli import _detect_runtime_context

        def fake_read(self, *args, **kwargs):
            if str(self) == "/proc/1/cgroup":
                return "0::/\n"
            if str(self) == "/proc/self/mountinfo":
                return ("123 122 0:9 /docker/containers/abc123/hostname "
                        "/etc/hostname rw - tmpfs tmpfs rw\n")
            raise OSError

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", new=fake_read), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_runtime_context() == "container"

    @pytest.mark.unit
    def test_systemd_via_proc_1_comm_and_journal_stream(self):
        """When INVOCATION_ID isn't set, fall back to PID 1 comm + JOURNAL_STREAM."""
        from eneru.cli import _detect_runtime_context

        def fake_read(self, *args, **kwargs):
            if str(self) == "/proc/1/comm":
                return "systemd\n"
            raise OSError

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", new=fake_read), \
             patch.dict("os.environ", {"JOURNAL_STREAM": "8:12345"}, clear=True):
            assert _detect_runtime_context() == "systemd service"


class TestKubernetesRuntimeDetection:
    """v5.5: K8s pods are a distinct runtime profile from generic Docker/Podman."""

    @pytest.mark.unit
    def test_kubernetes_service_host_env_var(self):
        """The canonical kubelet signal — set for every pod by default."""
        from eneru.cli import _detect_kubernetes, _detect_runtime_context

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", side_effect=OSError), \
             patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, clear=True):
            assert _detect_kubernetes() is True
            assert _detect_runtime_context() == "container (Kubernetes)"

    @pytest.mark.unit
    def test_kubernetes_service_account_token_mount(self):
        """Pods with hardened env still expose the SA token mount by default."""
        from eneru.cli import _detect_kubernetes, _detect_runtime_context

        def fake_exists(self):
            return str(self) == "/var/run/secrets/kubernetes.io/serviceaccount/token"

        with patch("pathlib.Path.exists", new=fake_exists), \
             patch("pathlib.Path.read_text", side_effect=OSError), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_kubernetes() is True
            assert _detect_runtime_context() == "container (Kubernetes)"

    @pytest.mark.unit
    def test_kubernetes_kubepods_cgroup(self):
        """Legacy cgroup v1 path that surfaces kubelet pod hierarchy."""
        from eneru.cli import _detect_kubernetes, _detect_runtime_context

        def fake_read(self, *args, **kwargs):
            if str(self) == "/proc/1/cgroup":
                return "12:cpu:/kubepods/burstable/pod-abc/container-def\n"
            raise OSError

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", new=fake_read), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_kubernetes() is True
            assert _detect_runtime_context() == "container (Kubernetes)"

    @pytest.mark.unit
    def test_kubernetes_wins_over_dockerenv(self):
        """Some CNI plugins create /.dockerenv inside K8s pods. K8s must win."""
        from eneru.cli import _detect_runtime_context

        def fake_exists(self):
            return str(self) == "/.dockerenv"

        with patch("pathlib.Path.exists", new=fake_exists), \
             patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, clear=False):
            assert _detect_runtime_context() == "container (Kubernetes)"

    @pytest.mark.unit
    def test_no_kubernetes_signals_does_not_classify_as_k8s(self):
        from eneru.cli import _detect_kubernetes

        with patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.read_text", side_effect=OSError), \
             patch.dict("os.environ", {}, clear=True):
            assert _detect_kubernetes() is False


class TestFindHostLoopback:
    """v5.5: _find_host_loopback locates the single is_host_loopback entry."""

    def _config_with_loopback(self, tmp_path, *, is_loopback=True, in_group=False):
        config_file = tmp_path / "config.yaml"
        if in_group:
            body = (
                "ups:\n"
                "  - name: 'TestUPS@localhost'\n"
                "    is_local: true\n"
                "    remote_servers:\n"
                "      - name: host-loopback\n"
                "        enabled: true\n"
                "        host: 127.0.0.1\n"
                "        user: root\n"
                f"        is_host_loopback: {'true' if is_loopback else 'false'}\n"
                "  - name: 'TestUPS2@localhost'\n"
                "    remote_servers:\n"
                "      - name: nas\n"
                "        enabled: true\n"
                "        host: 10.0.0.5\n"
                "        user: root\n"
            )
        else:
            body = (
                "ups:\n"
                "  name: 'TestUPS@localhost'\n"
                "remote_servers:\n"
                "  - name: host-loopback\n"
                "    enabled: true\n"
                "    host: 127.0.0.1\n"
                "    user: root\n"
                f"    is_host_loopback: {'true' if is_loopback else 'false'}\n"
            )
        config_file.write_text(body)
        return ConfigLoader.load(str(config_file))

    @pytest.mark.unit
    def test_find_host_loopback_top_level(self, tmp_path):
        from eneru.cli import _find_host_loopback

        config = self._config_with_loopback(tmp_path)
        result = _find_host_loopback(config)
        assert result is not None
        _owner, server = result
        assert server.is_host_loopback is True
        assert server.host == "127.0.0.1"

    @pytest.mark.unit
    def test_find_host_loopback_in_multi_ups_group(self, tmp_path):
        from eneru.cli import _find_host_loopback

        config = self._config_with_loopback(tmp_path, in_group=True)
        result = _find_host_loopback(config)
        assert result is not None
        _owner, server = result
        assert server.name == "host-loopback"

    @pytest.mark.unit
    def test_find_host_loopback_returns_none_when_none_flagged(self, tmp_path):
        from eneru.cli import _find_host_loopback

        config = self._config_with_loopback(tmp_path, is_loopback=False)
        assert _find_host_loopback(config) is None

    @pytest.mark.unit
    def test_find_host_loopback_ignores_disabled_entries(self, tmp_path):
        from eneru.cli import _find_host_loopback

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[RemoteServerConfig(
                    name="disabled-loopback",
                    enabled=False,
                    host="127.0.0.1",
                    user="root",
                    is_host_loopback=True,
                )],
            )],
        )

        assert _find_host_loopback(config) is None

    @pytest.mark.unit
    def test_uses_loopback_delegate_requires_container_local_and_enabled(self):
        # F-057: the predicate now lives in eneru.runtime; patch it there.
        from eneru.runtime import _uses_loopback_delegate

        group = UPSGroupConfig(
            ups=UPSConfig(name="UPS@host"),
            is_local=True,
            remote_servers=[RemoteServerConfig(
                name="host-loopback",
                enabled=True,
                host="127.0.0.1",
                user="root",
                is_host_loopback=True,
            )],
        )
        config = Config(ups_groups=[group])

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            assert _uses_loopback_delegate(config, group) is True

        group.remote_servers[0].enabled = False
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            assert _uses_loopback_delegate(config, group) is False

        group.remote_servers[0].enabled = True
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"):
            assert _uses_loopback_delegate(config, group) is False


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
    def test_validate_config_redacts_schemeless_notification_url(
            self, tmp_path, capsys):
        """A scheme-less URL must not leak any of its characters.

        The old display fell back to ``url[:20]...`` for URLs without
        ``://``, printing up to 20 raw characters of what may be a
        malformed-but-secret webhook reference. It now goes through
        redact_apprise_url like every other notification URL surface.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"

notifications:
  urls:
    - "hooks.example.com/services/SECRETTOKENPART"
""")

        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with pytest.raises(SystemExit):
                    main()

        captured = capsys.readouterr()
        assert "unknown://***" in captured.out
        assert "SECRETTOKENPART" not in captured.out
        assert "hooks.example.com" not in captured.out

    @pytest.mark.unit
    def test_validate_config_nonexistent_file(self, tmp_path, capsys):
        """F-003: `validate -c <missing>` must fail loud, non-zero.

        The old behavior warned and validated the all-default
        (shutdown-armed) config, exiting 0 — so a fat-fingered `--config`
        path looked "valid" while the daemon would boot on a config the
        operator never wrote. An explicit path that doesn't exist is now a
        hard error: non-zero exit and a "config file not found" message that
        names the typo'd path. Uses tmp_path so the path is deterministically
        absent across environments.
        """
        typo_path = str(tmp_path / "missing-config.yaml")
        # Sanity: ensure we're not racing a pre-existing file in tmp_path.
        assert not (tmp_path / "missing-config.yaml").exists()
        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", typo_path,
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code != 0

        # The message is carried on the SystemExit (ConfigLoader.load raises it);
        # capsys may or may not have flushed it depending on the exit path, so
        # assert on the exception which is always present.
        combined = str(exc_info.value.code) + capsys.readouterr().out
        assert "config file not found" in combined.lower()
        assert typo_path in combined

    @pytest.mark.unit
    def test_run_nonexistent_config_exits_nonzero(self, capsys):
        """F-003: `run -c <missing>` also exits non-zero rather than arming
        poweroff on the all-default config."""
        with patch.object(sys, "argv", [
            "eneru", "run", "-c", "/no/such/eneru-config.yaml",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        combined = str(exc_info.value.code) + capsys.readouterr().out
        assert "config file not found" in combined.lower()

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

    @pytest.mark.unit
    def test_validate_redundancy_groups_section(self, tmp_path, capsys):
        """Validate prints the redundancy_groups summary block — sources,
        quorum settings, remote_servers, and local_resources tags."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
    display_name: "UPS A"
  - name: "UPS-B"
    display_name: "UPS B"
redundancy_groups:
  - name: "rack-a"
    is_local: true
    ups_sources: ["UPS-A", "UPS-B"]
    min_healthy: 1
    degraded_counts_as: healthy
    unknown_counts_as: degraded
    virtual_machines:
      enabled: true
    containers:
      enabled: true
    remote_servers:
      - name: "node1"
        enabled: true
        host: "node1.lan"
        user: "ops"
local_shutdown:
  enabled: false
  trigger_on: none
""")

        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(config_file)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        out = capsys.readouterr().out
        assert "Redundancy groups (1)" in out
        assert "rack-a" in out
        assert "[is_local]" in out
        assert "Sources (2): UPS-A, UPS-B" in out
        assert "min_healthy=1" in out
        assert "degraded→healthy" in out
        assert "unknown→degraded" in out
        assert "Remote servers (1): node1" in out
        # Local-resources tags fire only because is_local is true
        assert "Local resources:" in out
        assert "VMs" in out
        assert "containers" in out

    @pytest.mark.unit
    def test_validate_shutdown_sequence_multi_phase_remote(self, tmp_path, capsys):
        """When remote_servers use shutdown_order phases, the sequence
        block prints "Remote servers (N, M phases):" and labels each
        phase with its order key."""
        config_file = tmp_path / "config.yaml"
        # In legacy single-UPS layout, remote_servers is a top-level key
        # (sibling to `ups:`), not nested under it.
        config_file.write_text("""
ups:
  name: "UPS@localhost"
remote_servers:
  - name: "early"
    enabled: true
    host: "h1.lan"
    user: "u"
    shutdown_order: 1
  - name: "mid"
    enabled: true
    host: "h2.lan"
    user: "u"
    shutdown_order: 2
  - name: "late"
    enabled: true
    host: "h3.lan"
    user: "u"
    shutdown_order: 3
""")

        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(config_file)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        out = capsys.readouterr().out
        # Three explicit phases, three distinct order keys
        assert "Remote servers (3, 3 phases):" in out
        assert "Phase 1 (order=1): early" in out
        assert "Phase 2 (order=2): mid" in out
        assert "Phase 3 (order=3): late" in out

    @pytest.mark.unit
    def test_validate_shutdown_sequence_legacy_parallel_sequential(self, tmp_path, capsys):
        """Legacy `parallel: true/false` flag (without shutdown_order)
        groups remotes into a sequential phase (negative key) and a
        parallel phase, labelled accordingly."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@localhost"
remote_servers:
  - name: "first"
    enabled: true
    host: "h1.lan"
    user: "u"
    parallel: false
  - name: "second"
    enabled: true
    host: "h2.lan"
    user: "u"
    parallel: false
  - name: "third"
    enabled: true
    host: "h3.lan"
    user: "u"
    parallel: true
""")

        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(config_file)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        out = capsys.readouterr().out
        # Legacy mode labels by execution style, not phase number
        assert "Sequential:" in out
        assert "Parallel:" in out

    @pytest.mark.unit
    def test_validate_handles_unparseable_yaml(self, tmp_path, capsys):
        """If the config file isn't a YAML mapping at the root, validate
        exits 1 and surfaces the parse error inline (covers the
        ConfigValidationLoadError catch in _cmd_validate)."""
        config_file = tmp_path / "config.yaml"
        # Top-level list isn't a valid Eneru config root.
        config_file.write_text("- not\n- a\n- mapping\n")

        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(config_file)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

        out = capsys.readouterr().out
        # The validate command prints "Configuration is INVALID" on any
        # exit_code != 0 path.
        assert "INVALID" in out


class TestCmdRunRouting:
    """`eneru run` routes to UPSGroupMonitor for single-UPS or
    MultiUPSCoordinator for multi-UPS / redundancy configs. Locks
    that the right entry point is picked from the parsed config."""

    @pytest.mark.unit
    def test_multi_ups_routes_to_coordinator(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
local_shutdown:
  enabled: false
  trigger_on: none
""")
        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file),
                                        "--exit-after-shutdown"]), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": "1"}, clear=False), \
             patch("eneru.cli.MultiUPSCoordinator") as coord_cls, \
             patch("eneru.cli.UPSGroupMonitor") as mon_cls:
            coord_cls.return_value = MagicMock()
            main()
        coord_cls.assert_called_once()
        mon_cls.assert_not_called()

    @pytest.mark.unit
    def test_redundancy_groups_route_to_coordinator(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
redundancy_groups:
  - name: "rack"
    ups_sources: ["UPS-A", "UPS-B"]
local_shutdown:
  enabled: false
  trigger_on: none
""")
        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file),
                                        "--exit-after-shutdown"]), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": "1"}, clear=False), \
             patch("eneru.cli.MultiUPSCoordinator") as coord_cls, \
             patch("eneru.cli.UPSGroupMonitor") as mon_cls:
            coord_cls.return_value = MagicMock()
            main()
        coord_cls.assert_called_once()
        mon_cls.assert_not_called()

    @pytest.mark.unit
    def test_single_ups_routes_to_monitor(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@localhost"
local_shutdown:
  enabled: false
  trigger_on: none
""")
        with patch.object(sys, "argv", ["eneru", "run", "-c", str(config_file),
                                        "--exit-after-shutdown"]), \
             patch.dict("os.environ", {"ENERU_SKIP_PRIVILEGE_CHECK": "1"}, clear=False), \
             patch("eneru.cli.UPSGroupMonitor") as mon_cls, \
             patch("eneru.cli.MultiUPSCoordinator") as coord_cls:
            mon_cls.return_value = MagicMock()
            main()
        mon_cls.assert_called_once()
        coord_cls.assert_not_called()


class TestIterRemoteServerOwners:
    """`_iter_remote_server_owners` yields (label, name, server) for
    every remote server in the config — including redundancy_groups,
    which use a `redundancy:<name>` label prefix."""

    @pytest.mark.unit
    def test_yields_redundancy_group_owners_with_label_prefix(self):
        from eneru.cli import _iter_remote_server_owners
        from eneru import RedundancyGroupConfig, RemoteServerConfig

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                remote_servers=[
                    RemoteServerConfig(name="ups-target", enabled=True,
                                       host="h1", user="u"),
                ],
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack",
                remote_servers=[
                    RemoteServerConfig(name="rg-target", enabled=True,
                                       host="h2", user="u"),
                ],
            )],
        )
        rows = list(_iter_remote_server_owners(config))
        # First row from ups_groups, second from redundancy_groups
        assert len(rows) == 2
        assert rows[0][0] == "UPS@host"  # owner_label = ups.label
        assert rows[1][0] == "redundancy:rack"
        assert rows[1][2].name == "rg-target"


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

    @pytest.mark.unit
    def test_test_notifications_invalid_url_is_redacted(self, tmp_path, capsys):
        """ISS-034: an invalid URL is printed scheme-only — no token leak."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@localhost"
notifications:
  urls:
    - "discord://id/SUPERSECRETTOKEN"
""")
        mock_apprise = MagicMock()
        inst = MagicMock()
        mock_apprise.Apprise.return_value = inst
        inst.add.return_value = False
        mock_apprise.NotifyType.INFO = "info"
        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
        ]):
            with patch("eneru.cli.APPRISE_AVAILABLE", True):
                with patch.dict(sys.modules, {"apprise": mock_apprise}):
                    with patch("eneru.cli.apprise", mock_apprise):
                        with pytest.raises(SystemExit):
                            main()
        out = capsys.readouterr().out
        assert "discord://***" in out
        assert "SUPERSECRETTOKEN" not in out


class TestCLIMainExceptionGuard:
    """ISS-035: main() turns unexpected errors into a one-line message + exit 1
    (no traceback), and Ctrl-C into exit 130."""

    @pytest.mark.unit
    def test_unexpected_exception_becomes_exit_1(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "c.yaml"
        cfg.write_text('ups:\n  name: "U@localhost"\n')
        monkeypatch.setattr(
            "eneru.cli._cmd_validate",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(cfg)]):
            with pytest.raises(SystemExit) as ei:
                main()
        assert ei.value.code == 1
        assert "Error: boom" in capsys.readouterr().err

    @pytest.mark.unit
    def test_keyboard_interrupt_becomes_exit_130(self, tmp_path, monkeypatch):
        cfg = tmp_path / "c.yaml"
        cfg.write_text('ups:\n  name: "U@localhost"\n')
        monkeypatch.setattr(
            "eneru.cli._cmd_validate",
            MagicMock(side_effect=KeyboardInterrupt()),
        )
        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(cfg)]):
            with pytest.raises(SystemExit) as ei:
                main()
        assert ei.value.code == 130

    @pytest.mark.unit
    def test_eneru_debug_reraises_traceback(self, tmp_path, monkeypatch):
        """ISS-035: ENERU_DEBUG=1 re-raises the original exception for diagnosis."""
        cfg = tmp_path / "c.yaml"
        cfg.write_text('ups:\n  name: "U@localhost"\n')
        monkeypatch.setenv("ENERU_DEBUG", "1")
        monkeypatch.setattr(
            "eneru.cli._cmd_validate",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        with patch.object(sys, "argv", ["eneru", "validate", "-c", str(cfg)]):
            with pytest.raises(RuntimeError, match="boom"):
                main()

    @pytest.mark.unit
    def test_self_test_token_flags_deprecation_warning(self, capsys):
        """ISS-033: --token/--api-key emit a deprecation warning steering to env."""
        import argparse
        from eneru.cli import _self_test_token
        assert _self_test_token(argparse.Namespace(token="abc", api_key=None)) == "abc"
        err = capsys.readouterr().err.lower()
        assert "deprecated" in err and "eneru_api_token" in err

    @pytest.mark.unit
    def test_apikey_create_warns_when_auth_inactive(self, tmp_path, capsys):
        """ISS-031: creating a key while auth is inactive warns it grants nothing."""
        cfg = tmp_path / "c.yaml"
        db = tmp_path / "auth.db"
        cfg.write_text(
            'ups:\n  name: "U@localhost"\n'
            f'api:\n  auth:\n    db_path: "{db}"\n'
        )
        with patch.object(sys, "argv", [
            "eneru", "apikey", "create", "--label", "ci", "-c", str(cfg),
        ]):
            main()
        out = capsys.readouterr().out
        assert "not active" in out.lower()


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
        # KIND column is present and tags ups groups correctly.
        assert "ups" in out
        # Disabled targets show '—' for ORDER, not a numeric position
        # in a rotation they don't participate in.
        dev_line = next(line for line in out.splitlines() if "dev-box" in line)
        assert "—" in dev_line
        assert "no" in dev_line

    @pytest.mark.unit
    def test_remote_list_group_column_matches_what_shutdown_group_accepts(
        self, tmp_path, capsys,
    ):
        # Regression guard: the GROUP column value must be exactly what
        # `eneru shutdown group --group <X>` accepts (no parentheticals,
        # no `redundancy:` prefix). The CLI resolver looks at name/label
        # for ups groups and name for redundancy groups.
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: UPS-A\n"
            "    display_name: rack-a\n"
            "    remote_servers:\n"
            "      - name: nas\n"
            "        enabled: true\n"
            "        host: nas.local\n"
            "        user: root\n"
            "redundancy_groups:\n"
            "  - name: rack-pair\n"
            "    ups_sources: [UPS-A]\n"
            "    min_healthy: 1\n"
            "    remote_servers:\n"
            "      - name: vault\n"
            "        enabled: true\n"
            "        host: vault.local\n"
            "        user: root\n"
        )
        with patch.object(sys, "argv", [
            "eneru", "remote", "list", "-c", str(config_file),
        ]):
            main()
        out = capsys.readouterr().out
        # No legacy `name (label)` parenthetical or `redundancy:` prefix.
        assert "(rack-a)" not in out
        assert "redundancy:" not in out
        # The bare group names that --group accepts ARE present.
        nas_line = next(line for line in out.splitlines() if "nas " in line)
        vault_line = next(line for line in out.splitlines() if "vault" in line)
        assert "UPS-A" in nas_line
        assert "rack-pair" in vault_line

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
    def test_remote_list_includes_remote_health_sidecar(self, tmp_path, capsys):
        """`remote list` should show last known health without probing SSH."""
        from eneru.remote_health import remote_health_sidecar_path
        from eneru.status import state_file_path_for_group

        state_file = tmp_path / "state.json"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: UPS-A\n"
            "  display_name: rack-a\n"
            "remote_servers:\n"
            "  - name: Synology NAS\n"
            "    enabled: true\n"
            "    host: nas.local\n"
            "    user: admin\n"
            "logging:\n"
            f"  state_file: '{state_file}'\n"
            "remote_health:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        sidecar = remote_health_sidecar_path(
            state_file_path_for_group(config, config.ups_groups[0])
        )
        sidecar.write_text(json.dumps({
            "group": "rack-a",
            "generated_at": 1,
            "servers": [{
                "group": "rack-a",
                "server": "Synology NAS",
                "host": "nas.local",
                "user": "admin",
                "status": "HEALTHY",
            }],
        }))

        with patch.object(sys, "argv", [
            "eneru", "remote", "list", "-c", str(config_file),
        ]):
            main()

        out = capsys.readouterr().out
        assert "HEALTH" in out
        assert "HEALTHY" in next(
            line for line in out.splitlines() if "Synology NAS" in line
        )

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


class TestRemoteHealthIndex:
    """Unit coverage for ``_remote_health_index`` -- the sidecar-rows ->
    lookup-table helper behind the HEALTH column of ``eneru remote list``."""

    @pytest.mark.unit
    def test_indexes_row_by_both_server_and_host(self):
        from eneru.cli import _remote_health_index

        idx = _remote_health_index([
            {"group": "rack-a", "server": "nas", "host": "nas.local",
             "status": "HEALTHY"},
        ])
        assert idx[("rack-a", "nas")] == "HEALTHY"
        assert idx[("rack-a", "nas.local")] == "HEALTHY"

    @pytest.mark.unit
    def test_server_only_and_host_only_rows(self):
        """A row may carry only a server name or only a host; each indexes the
        key it has and skips the empty one (exercises both the ``if server``
        and ``if host`` false branches)."""
        from eneru.cli import _remote_health_index

        idx = _remote_health_index([
            {"group": "g", "server": "srv-only", "status": "DEGRADED"},
            {"group": "g", "host": "host-only.lan", "status": "UNREACHABLE"},
        ])
        assert idx == {
            ("g", "srv-only"): "DEGRADED",
            ("g", "host-only.lan"): "UNREACHABLE",
        }

    @pytest.mark.unit
    def test_missing_status_defaults_to_unknown(self):
        from eneru.cli import _remote_health_index

        idx = _remote_health_index([{"group": "g", "server": "s"}])
        assert idx[("g", "s")] == "UNKNOWN"

    @pytest.mark.unit
    def test_non_dict_rows_are_skipped(self):
        """A corrupted or version-skewed sidecar write can leave non-mapping
        items in the ``servers`` list. They must be skipped, not crash
        ``remote list`` with an AttributeError on ``row.get``."""
        from eneru.cli import _remote_health_index

        idx = _remote_health_index([
            "bogus", 123, None, ["x"],
            {"group": "g", "server": "good", "status": "HEALTHY"},
        ])
        assert idx == {("g", "good"): "HEALTHY"}

    @pytest.mark.unit
    def test_empty_and_none_inputs(self):
        from eneru.cli import _remote_health_index

        assert _remote_health_index([]) == {}
        assert _remote_health_index(None) == {}


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
    def test_real_shutdown_group_uses_strict_loopback_key_check(self, tmp_path, capsys):
        """Real manual shutdown must fail fast when a synthesized loopback key is absent."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  display_name: rack-a\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli._LOOPBACK_DEFAULT_SSH_KEY_PATH",
                   str(tmp_path / "missing-id-loopback")), \
             patch.object(sys, "argv", [
                "eneru", "shutdown", "group",
                "-c", str(config_file), "--group", "rack-a",
                "--i-really-want-to-proceed-with-group-shutdown",
             ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1
        assert "default SSH key for the host-loopback delegate is missing" in (
            capsys.readouterr().err
        )

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

    def _is_local_redundancy_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: UPS-A\n"
            "    display_name: rack-a\n"
            "  - name: UPS-B\n"
            "    display_name: rack-b\n"
            "redundancy_groups:\n"
            "  - name: local-pair\n"
            "    ups_sources: [UPS-A, UPS-B]\n"
            "    min_healthy: 1\n"
            "    is_local: true\n"
            "    remote_servers:\n"
            "      - name: nas\n"
            "        enabled: true\n"
            "        host: nas.local\n"
            "        user: root\n"
        )
        return config_file

    @pytest.mark.unit
    def test_is_local_redundancy_confirm_warns_about_destructive_mixins(
        self, tmp_path, capsys,
    ):
        # is_local redundancy + confirm + real execution: the executor
        # still drains VMs/containers/filesystems before the suppressed
        # poweroff. The CLI must explicitly warn about that, not just say
        # "no local poweroff" (which understates the blast radius).
        config_file = self._is_local_redundancy_config(tmp_path)
        with patch("eneru.cli.RedundancyGroupExecutor") as mock_executor_cls:
            mock_executor_cls.return_value = MagicMock()
            with patch.object(sys, "argv", [
                "eneru", "shutdown", "group",
                "-c", str(config_file), "--group", "local-pair",
                "--i-really-want-to-proceed-with-group-shutdown",
            ]):
                main()
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "local VMs/containers" in out
        assert "unmount configured filesystems" in out

    @pytest.mark.unit
    def test_is_local_redundancy_dry_run_keeps_softer_disclaimer(
        self, tmp_path, capsys,
    ):
        # In dry-run mode no real damage can land, so the softer
        # "rehearsal does not fire local poweroff" line is appropriate.
        config_file = self._is_local_redundancy_config(tmp_path)
        with patch("eneru.cli.RedundancyGroupExecutor") as mock_executor_cls:
            mock_executor_cls.return_value = MagicMock()
            with patch.object(sys, "argv", [
                "eneru", "shutdown", "group",
                "-c", str(config_file), "--group", "local-pair", "--dry-run",
            ]):
                main()
        out = capsys.readouterr().out
        assert "WARNING" not in out
        assert "does not fire local poweroff" in out


# --- v5.5: container loopback delegation — privilege check + synthesis ---


class TestPrivilegeChecksV5_5:
    """v5.5 added the container-loopback pass-through to _exit_on_privilege_errors."""

    def _container_config_with_loopback(self, tmp_path):
        """Single-UPS legacy config with local capabilities + an explicit loopback."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        return ConfigLoader.load(str(config_file))

    @pytest.mark.unit
    def test_container_with_loopback_passes_for_non_root(self, tmp_path, capsys):
        """Docker/Podman + loopback + non-root → silent pass.

        The privilege check returns without raising AND without printing a
        banner. (rc6: removed the "delegated to root@127.0.0.1 via SSH"
        line — root vs non-root container is cosmetic in v5.5 since both
        paths SSH-delegate through the loopback, so the banner was just
        noise on every restart.)
        """
        from eneru.cli import _exit_on_privilege_errors

        config = self._container_config_with_loopback(tmp_path)
        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _exit_on_privilege_errors(config)  # Must not raise

        # Silent — no banner.
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_kubernetes_with_capabilities_passes_with_warning(self, tmp_path, capsys):
        """K8s + local capabilities + non-root → start with WARNING (no loopback required)."""
        from eneru.cli import _exit_on_privilege_errors

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Kubernetes)"):
            _exit_on_privilege_errors(config)  # Must not raise

        err = capsys.readouterr().err
        assert "container (Kubernetes)" in err
        assert "/ready will report 503" in err

    @pytest.mark.unit
    def test_docker_without_loopback_still_exits_for_non_root(self, tmp_path, capsys):
        """Container runtime + local caps + NO loopback + non-root → exit with hint."""
        from eneru.cli import _exit_on_privilege_errors

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))

        with patch("eneru.cli.os.geteuid", return_value=1000, create=True), \
             patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            with pytest.raises(SystemExit) as exc_info:
                _exit_on_privilege_errors(config)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "loopback SSH delegate" in out
        assert "config-container-local.yaml" in out


class TestSynthesizeLoopback:
    """v5.5: auto-enable loopback for Docker/Podman + local capabilities."""

    def _local_config_no_loopback(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        return ConfigLoader.load(str(config_file))

    @pytest.mark.unit
    def test_synthesizes_loopback_on_docker_with_local_caps(self, tmp_path):
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config = self._local_config_no_loopback(tmp_path)
        assert _find_host_loopback(config) is None  # baseline

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"):  # ssh key present
            _synthesize_loopback_if_needed(config)

        found = _find_host_loopback(config)
        assert found is not None
        _owner, server = found
        assert server.host == "127.0.0.1"
        assert server.user == "root"
        assert server.shutdown_command == "shutdown -h now"
        assert server.ssh_key_path == "/var/lib/eneru/ssh/id_loopback"
        # v5.5: shutdown_order intentionally unset on the synthesized
        # loopback. The runtime brackets is_host_loopback delegates
        # around the regular remotes regardless of this field; the
        # ordering invariant is enforced by RemoteShutdownMixin
        # (see TestLoopbackShutdownOrdering in test_remote_commands.py).
        assert server.shutdown_order is None

    @pytest.mark.unit
    def test_skipped_on_kubernetes(self, tmp_path):
        """K8s is the remote-only profile — never auto-enable."""
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config = self._local_config_no_loopback(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Kubernetes)"), \
             patch("eneru.cli.Path.exists", return_value=True):
            _synthesize_loopback_if_needed(config)
        assert _find_host_loopback(config) is None

    @pytest.mark.unit
    def test_skipped_on_bare_metal(self, tmp_path):
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config = self._local_config_no_loopback(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"), \
             patch("eneru.cli.Path.exists", return_value=True):
            _synthesize_loopback_if_needed(config)
        assert _find_host_loopback(config) is None

    @pytest.mark.unit
    def test_skipped_when_no_local_capabilities(self, tmp_path):
        """Remote-only container with no local actions → no synthesis."""
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  - name: 'TestUPS@nut'\n"  # not strictly valid YAML; use list form below
        )
        # simpler: build the config with no is_local + no local capabilities
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.exists", return_value=True):
            _synthesize_loopback_if_needed(config)
        assert _find_host_loopback(config) is None

    @pytest.mark.unit
    def test_skipped_when_explicit_loopback_present(self, tmp_path):
        """User-configured loopback wins; synthesis must not double-inject."""
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: my-loopback\n"
            "    enabled: true\n"
            "    host: 172.17.0.1\n"
            "    user: deploy\n"
            "    shutdown_command: 'sudo shutdown -h now'\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        before = _find_host_loopback(config)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"):
            _synthesize_loopback_if_needed(config)
        after = _find_host_loopback(config)
        # Same server entry (no new one synthesized)
        assert before[1] is after[1]
        assert after[1].user == "deploy"

    @pytest.mark.unit
    def test_errors_if_default_ssh_key_missing(self, tmp_path, capsys):
        """Synthesis fires but the default key path doesn't exist → exit 1 with hint."""
        from eneru.cli import _synthesize_loopback_if_needed

        config = self._local_config_no_loopback(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat", side_effect=FileNotFoundError):
            with pytest.raises(SystemExit) as exc_info:
                _synthesize_loopback_if_needed(config)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "default SSH key for the host-loopback delegate is missing" in err
        assert "/var/lib/eneru/ssh/id_loopback" in err

    @pytest.mark.unit
    def test_unreadable_ssh_key_path_distinguishes_perm_error(self, tmp_path, capsys):
        """Path.stat() raises PermissionError when the parent dir isn't
        readable — common when operators bind-mount /root/.ssh/ (0700)
        into the container running as eneru (uid 10001). Treat as
        'not usable' with a perm-specific actionable error rather than
        an uncaught traceback."""
        from eneru.cli import _synthesize_loopback_if_needed

        config = self._local_config_no_loopback(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat",
                   side_effect=PermissionError("Permission denied")):
            with pytest.raises(SystemExit) as exc_info:
                _synthesize_loopback_if_needed(config)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not readable by the container user" in err
        assert "uid 10001 (eneru)" in err
        assert "PermissionError" in err

    @pytest.mark.unit
    def test_unreadable_ssh_key_warns_in_non_strict_mode(self, tmp_path, capsys):
        """validate / dry-run rehearsals get a WARNING and proceed so the
        user can still inspect the delegated sequence even before fixing
        the perm issue."""
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config = self._local_config_no_loopback(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat",
                   side_effect=PermissionError("Permission denied")):
            _synthesize_loopback_if_needed(config, strict_key_check=False)
        # Synthesis still happens — loopback entry was injected.
        assert _find_host_loopback(config) is not None
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "not readable by the container user" in err


class TestKubernetesLocalMisuseWarning:
    """v5.5: K8s + local capabilities → startup WARNING (not error)."""

    @pytest.mark.unit
    def test_warning_fires_for_k8s_with_local_caps(self, tmp_path, capsys):
        from eneru.cli import _warn_on_kubernetes_local_misuse

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Kubernetes)"):
            _warn_on_kubernetes_local_misuse(config)
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "Kubernetes" in err
        assert "install-comparison.md" in err

    @pytest.mark.unit
    def test_silent_on_docker(self, tmp_path, capsys):
        from eneru.cli import _warn_on_kubernetes_local_misuse

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _warn_on_kubernetes_local_misuse(config)
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_silent_on_k8s_without_local_caps(self, tmp_path, capsys):
        """Remote-only K8s deployment is the recommended path — no warning."""
        from eneru.cli import _warn_on_kubernetes_local_misuse

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Kubernetes)"):
            _warn_on_kubernetes_local_misuse(config)
        assert capsys.readouterr().err == ""


# --- Coverage uplift: branches missed by the existing suite ---


class TestApplyRunOverridesDryRun:
    """Cover the dry_run=True branch in `_apply_run_overrides` (line 67)."""

    @pytest.mark.unit
    def test_dry_run_flag_flips_config_to_true(self):
        from argparse import Namespace
        from eneru.cli import _apply_run_overrides

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            behavior=BehaviorConfig(dry_run=False),
        )
        args = Namespace(dry_run=True, api=False, api_bind=None, api_port=None)

        _apply_run_overrides(config, args)

        assert config.behavior.dry_run is True


class TestLoopbackHelpersRedundancyPaths:
    """Cover redundancy_groups branches in the loopback helpers
    (`_find_host_loopback`, `_has_explicit_loopback_opt_out`,
    `_local_owner_group`, `_uses_loopback_delegate`)."""

    @pytest.mark.unit
    def test_find_host_loopback_in_redundancy_group(self):
        from eneru.cli import _find_host_loopback
        from eneru import RedundancyGroupConfig

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
                remote_servers=[RemoteServerConfig(
                    name="host-loopback",
                    enabled=True,
                    host="127.0.0.1",
                    user="root",
                    is_host_loopback=True,
                )],
            )],
        )
        found = _find_host_loopback(config)
        assert found is not None
        owner_label, server = found
        assert owner_label == "rack-a"
        assert server.is_host_loopback is True

    @pytest.mark.unit
    def test_find_host_loopback_redundancy_unnamed_group_label(self):
        from eneru.cli import _find_host_loopback
        from eneru import RedundancyGroupConfig

        rg = RedundancyGroupConfig(
            remote_servers=[RemoteServerConfig(
                name="lb", enabled=True, host="127.0.0.1", user="root",
                is_host_loopback=True,
            )],
        )
        # Force-clear the name to exercise the "(unnamed)" fallback.
        rg.name = ""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
            )],
            redundancy_groups=[rg],
        )
        found = _find_host_loopback(config)
        assert found is not None
        owner_label, _ = found
        assert owner_label == "(unnamed)"

    @pytest.mark.unit
    def test_has_explicit_opt_out_in_ups_group(self):
        from eneru.cli import _has_explicit_loopback_opt_out

        server = RemoteServerConfig(
            name="not-loopback", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=False,
        )
        # Mark the field as explicitly set in YAML.
        server._is_host_loopback_explicit = True
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[server],
            )],
        )
        assert _has_explicit_loopback_opt_out(config) is True

    @pytest.mark.unit
    def test_has_explicit_opt_out_in_redundancy_group(self):
        from eneru.cli import _has_explicit_loopback_opt_out
        from eneru import RedundancyGroupConfig

        server = RemoteServerConfig(
            name="not-loopback", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=False,
        )
        server._is_host_loopback_explicit = True
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
                remote_servers=[server],
            )],
        )
        assert _has_explicit_loopback_opt_out(config) is True

    @pytest.mark.unit
    def test_local_owner_group_finds_redundancy_is_local(self):
        from eneru.cli import _local_owner_group
        from eneru import RedundancyGroupConfig

        rg = RedundancyGroupConfig(name="rack-a", is_local=True)
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
            )],
            redundancy_groups=[rg],
        )
        assert _local_owner_group(config) is rg

    @pytest.mark.unit
    def test_uses_loopback_delegate_early_return_when_explicit_group_passed(self):
        """Passing `group=` skips the `_local_owner_group` lookup (line 259)."""
        # F-057: predicate + helpers now live in eneru.runtime; patch them there.
        from eneru.runtime import _uses_loopback_delegate

        group = UPSGroupConfig(
            ups=UPSConfig(name="UPS@host"),
            is_local=True,
            remote_servers=[RemoteServerConfig(
                name="host-loopback", enabled=True, host="127.0.0.1",
                user="root", is_host_loopback=True,
            )],
        )
        config = Config(ups_groups=[group])

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.runtime._local_owner_group") as mock_owner:
            # Passing group explicitly means _local_owner_group must NOT
            # be consulted — that's the whole point of the bypass.
            assert _uses_loopback_delegate(config, group=group) is True
            mock_owner.assert_not_called()


class TestSynthesizeLoopbackDefensiveReturn:
    """Cover the defensive `return` at line 341 — `owner is None` but
    `config.ups_groups` is non-empty (no group marked is_local)."""

    @pytest.mark.unit
    def test_no_owner_with_existing_groups_returns_without_synth(self):
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        # ups_groups present but none is_local; redundancy group requires
        # local capabilities so synthesis would otherwise proceed.
        from eneru import RedundancyGroupConfig

        rg = RedundancyGroupConfig(
            name="rack-a",
            # NOT is_local; capability comes from a separate path.
        )
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
                virtual_machines=VMConfig(enabled=True),
            )],
            redundancy_groups=[rg],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        # Sanity: local capabilities are declared so we get past the
        # `_local_capabilities_required` guard.
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"):
            _synthesize_loopback_if_needed(config)
        # No owner was identified → defensive return path; no loopback
        # was injected.
        assert _find_host_loopback(config) is None


class TestSynthesizeLoopbackImplicitMode:
    """Cover line 428 — synthesized loopback attaches to `config.remote_servers`
    in implicit single-UPS mode (no ups_groups, no redundancy_groups)."""

    @pytest.mark.unit
    def test_implicit_mode_attaches_to_top_level_remote_servers(self, capsys):
        from eneru.cli import _synthesize_loopback_if_needed

        # Implicit single-UPS local-host mode → no ups_groups at all.
        # `_local_owner_group` returns None for this shape, and synthesis
        # falls through the `owner is None and config.ups_groups` guard
        # (ups_groups is empty), hitting the top-level append branch at
        # line 428.
        config = Config(
            ups_groups=[],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )
        assert _local_owner_group_module_safe(config) is None
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"):
            _synthesize_loopback_if_needed(config)
        # The synthesis path printed its banner — proves we reached the
        # `config.remote_servers.append(synthesized)` line (line 428)
        # before the banner at lines 430-435.
        err = capsys.readouterr().err
        assert "auto-enabled host-loopback delegate" in err


def _local_owner_group_module_safe(config):
    """Helper that mirrors `_local_owner_group` so we can sanity-check
    setup without importing through unittest.mock interference."""
    from eneru.cli import _local_owner_group
    return _local_owner_group(config)


class TestExitOnMissingLoopbackContract:
    """Cover lines 443-459 — container + local caps + no loopback exits 1."""

    @pytest.mark.unit
    def test_container_local_caps_without_loopback_exits(self, capsys):
        from eneru.cli import _exit_on_missing_loopback_contract

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            with pytest.raises(SystemExit) as exc_info:
                _exit_on_missing_loopback_contract(config)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "no enabled is_host_loopback delegate is configured" in err

    @pytest.mark.unit
    def test_bare_metal_is_noop(self):
        from eneru.cli import _exit_on_missing_loopback_contract

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"):
            _exit_on_missing_loopback_contract(config)  # Must not raise

    @pytest.mark.unit
    def test_kubernetes_is_noop(self):
        from eneru.cli import _exit_on_missing_loopback_contract

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Kubernetes)"):
            _exit_on_missing_loopback_contract(config)  # Must not raise

    @pytest.mark.unit
    def test_container_without_local_caps_is_noop(self):
        from eneru.cli import _exit_on_missing_loopback_contract

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@nut"), is_local=False,
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _exit_on_missing_loopback_contract(config)  # Must not raise

    @pytest.mark.unit
    def test_existing_loopback_is_noop(self):
        from eneru.cli import _exit_on_missing_loopback_contract

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[RemoteServerConfig(
                    name="host-loopback", enabled=True, host="127.0.0.1",
                    user="root", is_host_loopback=True,
                )],
            )],
            local_shutdown=LocalShutdownConfig(enabled=True),
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _exit_on_missing_loopback_contract(config)  # Must not raise


class TestInjectDelegatedActions:
    """Cover `_inject_delegated_actions` branches: owner=None early return
    (line 491), include_user_containers add (line 511), no-generated-actions
    early return (line 521), and the generation/prepend path."""

    @pytest.mark.unit
    def test_returns_when_not_a_container_runtime(self):
        from eneru.cli import _inject_delegated_actions

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[RemoteServerConfig(
                    name="host-loopback", enabled=True, host="127.0.0.1",
                    user="root", is_host_loopback=True,
                )],
            )],
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"):
            _inject_delegated_actions(config)
        # Nothing was generated.
        assert config.ups_groups[0].remote_servers[0].pre_shutdown_commands == []

    @pytest.mark.unit
    def test_returns_when_no_loopback_present(self):
        from eneru.cli import _inject_delegated_actions

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=True,
            )],
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)  # Must not raise

    @pytest.mark.unit
    def test_returns_when_no_local_owner_group(self):
        """Loopback exists but no group is is_local → owner is None → return."""
        from eneru.cli import _inject_delegated_actions

        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1",
            user="root", is_host_loopback=True,
        )
        # is_local=False everywhere, but the loopback IS present — exercises
        # the owner=None early return at line 491.
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"), is_local=False,
                remote_servers=[loopback],
            )],
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)
        assert loopback.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_no_local_capabilities_returns_without_prepending(self):
        """Loopback + is_local but zero capability flags → no generated
        actions → line 521 early return; pre_shutdown_commands stays empty."""
        from eneru.cli import _inject_delegated_actions

        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1",
            user="root", is_host_loopback=True,
        )
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=False),
                containers=ContainersConfig(enabled=False),
                filesystems=FilesystemsConfig(
                    sync_enabled=False,
                    unmount=UnmountConfig(enabled=False),
                ),
                remote_servers=[loopback],
            )],
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)
        assert loopback.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_include_user_containers_adds_rootless_action(self):
        """Cover line 511 — `include_user_containers` adds the rootless
        delegated action."""
        from eneru.cli import _inject_delegated_actions

        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1",
            user="root", is_host_loopback=True,
        )
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                containers=ContainersConfig(
                    enabled=True,
                    shutdown_all_remaining_containers=False,
                    include_user_containers=True,
                ),
                remote_servers=[loopback],
            )],
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)
        actions = [c.action for c in loopback.pre_shutdown_commands]
        assert "stop_containers_rootless" in actions

    @pytest.mark.unit
    def test_multi_ups_local_sections_generate_full_loopback_action_set(self, tmp_path):
        """The loopback E2E config uses the multi-UPS list shape, so local
        resources must live under the local ``ups`` entry."""
        from eneru.cli import _inject_delegated_actions

        config_file = tmp_path / "loopback.yaml"
        config_file.write_text(
            "ups:\n"
            "  - name: TestUPS@nut-server\n"
            "    is_local: true\n"
            "    remote_servers:\n"
            "      - name: host-loopback\n"
            "        enabled: true\n"
            "        host: ssh-target\n"
            "        user: root\n"
            "        is_host_loopback: true\n"
            "    virtual_machines:\n"
            "      enabled: true\n"
            "      max_wait: 2\n"
            "    containers:\n"
            "      enabled: true\n"
            "      runtime: docker\n"
            "      shutdown_all_remaining_containers: true\n"
            "      include_user_containers: true\n"
            "      compose_files:\n"
            "        - path: /opt/e2e/docker-compose.yml\n"
            "          stop_timeout: 2\n"
            "    filesystems:\n"
            "      sync_enabled: true\n"
            "      unmount:\n"
            "        enabled: true\n"
            "        mounts:\n"
            "          - path: /mnt/e2e-loopback\n"
            "            options: -l\n"
        )
        config = ConfigLoader.load(str(config_file))

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        actions = [
            c.action
            for c in config.ups_groups[0].remote_servers[0].pre_shutdown_commands
        ]
        assert actions == [
            "stop_vms",
            "stop_compose",
            "stop_containers",
            "stop_containers_rootless",
            "sync",
            "unmount_filesystems",
        ]


class TestPrintShutdownSequenceDelegated:
    """Cover the delegated-loopback summary lines in `_print_shutdown_sequence`
    (760-777, 795, 815) via `eneru validate`."""

    @pytest.mark.unit
    def test_validate_shows_delegated_summary_and_host_poweroff(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "containers:\n"
            "  enabled: true\n"
            "filesystems:\n"
            "  sync_enabled: true\n"
            "  unmount:\n"
            "    enabled: true\n"
            "    mounts:\n"
            "      - /mnt/data\n"
            "remote_servers:\n"
            "  - name: nas-a\n"
            "    enabled: true\n"
            "    host: nas-a.lan\n"
            "    user: root\n"
            "  - name: nas-b\n"
            "    enabled: true\n"
            "    host: nas-b.lan\n"
            "    user: root\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
            "    shutdown_order: 999\n"
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"), \
             patch.object(sys, "argv", ["eneru", "validate", "-c", str(config_file)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Line 773-776: delegated summary names every local action that
        # was delegated, including the unmount(N) suffix.
        assert "Local actions delegated via loopback SSH" in out
        assert "VMs" in out
        assert "containers" in out
        assert "sync" in out
        assert "unmount(1)" in out
        # Line 815: delegated host poweroff line.
        assert "host poweroff delegated via loopback SSH" in out

    @pytest.mark.unit
    def test_validate_multi_server_single_phase_line(self, tmp_path, capsys):
        """Cover line 795 — multiple remote servers, all in the same phase."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "remote_servers:\n"
            "  - name: nas-a\n"
            "    enabled: true\n"
            "    host: nas-a.lan\n"
            "    user: root\n"
            "  - name: nas-b\n"
            "    enabled: true\n"
            "    host: nas-b.lan\n"
            "    user: root\n"
            "  - name: nas-c\n"
            "    enabled: true\n"
            "    host: nas-c.lan\n"
            "    user: root\n"
        )
        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", str(config_file),
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Line 795 — multi-server, single phase.
        assert "Remote servers (3):" in out


class TestCLIPartitioningOfLoopback:
    """v5.5: CLI inspection paths (validate, remote list, shutdown remote)
    must partition is_host_loopback delegates out of compute_effective_order
    so their output matches the runtime bracketing (Phase A / regulars /
    Phase C). Without the partition, `eneru validate` lists a phantom
    `Remote server: host-loopback` step and `eneru remote list` prints a
    misleading numeric ORDER for the loopback row.
    """

    def _docker_config_with_loopback_and_nas(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: NAS\n"
            "    enabled: true\n"
            "    host: 10.0.0.10\n"
            "    user: root\n"
            "    shutdown_order: 5\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        return config_file

    @pytest.mark.unit
    def test_validate_no_phantom_remote_loopback_row(self, tmp_path, capsys):
        """`eneru validate` must NOT print `Remote server: host-loopback`
        — the loopback is surfaced via the delegated-actions step and the
        host-poweroff step, never as a peer-remote phase."""
        config_file = self._docker_config_with_loopback_and_nas(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"), \
             patch.object(sys, "argv", [
                "eneru", "validate", "-c", str(config_file),
             ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Delegated-actions step + NAS-only remote row + host-poweroff step.
        assert "Local actions delegated via loopback SSH" in out
        assert "Remote server: NAS" in out
        # Phantom must be gone — never as a peer remote.
        assert "Remote server: host-loopback" not in out
        assert "Remote servers (2)" not in out
        # Final host-poweroff line still delegated.
        assert "host poweroff delegated via loopback SSH" in out

    @pytest.mark.unit
    def test_validate_loopback_only_no_remote_row(self, tmp_path, capsys):
        """Loopback-only setup (no peer remotes) must not print a `(no
        remote servers)` line either — the loopback covers Phase A and C."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"), \
             patch.object(sys, "argv", [
                "eneru", "validate", "-c", str(config_file),
             ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Local actions delegated via loopback SSH" in out
        assert "host poweroff delegated via loopback SSH" in out
        # No misleading peer-remote row or empty-list placeholder.
        assert "Remote server" not in out  # neither singular nor "Remote servers"
        assert "(no remote servers)" not in out

    @pytest.mark.unit
    def test_remote_list_loopback_row_shows_loopback_order(self, tmp_path, capsys):
        """`eneru remote list` must show the loopback's ORDER as `loopback`,
        not a numeric phase that the runtime ignores."""
        config_file = self._docker_config_with_loopback_and_nas(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"), \
             patch.object(sys, "argv", [
                "eneru", "remote", "list", "-c", str(config_file),
             ]):
            # `eneru remote list` returns normally on success (no sys.exit).
            main()
        out = capsys.readouterr().out
        # Loopback row exists with ORDER=loopback.
        assert "host-loopback" in out
        assert "loopback" in out
        # NAS row keeps its numeric order.
        assert "NAS" in out


class TestRemoteListAppliesRuntimePrep:
    """v5.5: `_cmd_remote_list` must call `_prepare_runtime_config` so
    an auto-synthesized loopback delegate appears in the printed table.
    Without prep, `eneru remote list` silently disagrees with what the
    daemon and `eneru validate` would see — the loopback row is
    missing entirely. Sibling fix to `_cmd_shutdown_remote`."""

    @pytest.mark.unit
    def test_remote_list_runs_prepare_runtime_config(self, tmp_path):
        from eneru.cli import _cmd_remote_list

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: NAS\n"
            "    enabled: true\n"
            "    host: 10.0.0.10\n"
            "    user: root\n"
        )
        args = argparse.Namespace(config=str(config_file))

        with patch("eneru.cli._prepare_runtime_config") as mock_prep, \
             patch("eneru.cli._exit_on_config_errors"):
            try:
                _cmd_remote_list(args)
            except SystemExit as exc:
                assert exc.code in (None, 0), f"unexpected SystemExit code: {exc.code}"

        mock_prep.assert_called_once()
        # Read-only inspection — non-strict so a missing key warns
        # instead of hard-erroring.
        _args, kwargs = mock_prep.call_args
        assert kwargs.get("strict_key_check") is False


class TestShutdownRemoteAppliesRuntimePrep:
    """v5.5: `_cmd_shutdown_remote` (the manual one-server drill) must
    call `_prepare_runtime_config` so an explicit OR synthesized
    is_host_loopback target picks up the auto-synthesized delegate plus
    the generated VM/container/sync/unmount pre-actions. Without prep
    the drill silently runs only the user-typed entry and misses the
    generated work — defeating the purpose of a drill."""

    @pytest.mark.unit
    def test_shutdown_remote_drill_runs_prepare_runtime_config(self, tmp_path):
        from eneru.cli import _cmd_shutdown_remote

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "  is_local: true\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: NAS\n"
            "    enabled: true\n"
            "    host: 10.0.0.10\n"
            "    user: root\n"
        )
        args = argparse.Namespace(
            config=str(config_file),
            server="NAS",
            group=None,
            dry_run=True,
            confirm=False,
            connectivity_check=False,
            host_keys=None,
            log_file=None,
        )

        with patch("eneru.cli._prepare_runtime_config") as mock_prep, \
             patch("eneru.cli._exit_on_config_errors"), \
             patch("eneru.cli.UPSGroupMonitor") as mock_monitor_cls:
            mock_monitor = mock_monitor_cls.return_value
            mock_monitor._shutdown_remote_server.return_value = MagicMock(
                success=True, shutdown_sent=True, error="",
            )
            try:
                _cmd_shutdown_remote(args)
            except SystemExit as exc:
                # Drill must exit cleanly (0) or via the dry-run early
                # return (None). Swallowing every code would mask a
                # regression where the command exits 1 or 2 right
                # after the prep call — `assert_called_once` would
                # still pass and the test would lie.
                assert exc.code in (None, 0), f"unexpected SystemExit code: {exc.code}"

        mock_prep.assert_called_once()
        # Dry-run drills use strict_key_check=False so a missing key
        # warns instead of hard-erroring (operator is diagnosing).
        _args, kwargs = mock_prep.call_args
        assert kwargs.get("strict_key_check") is False


class TestValidateNotificationFormatting:
    """Cover the notification display branches — Title:(none) and
    avatar_url. The URL-without-scheme case now redacts via
    redact_apprise_url; see
    test_validate_config_redacts_schemeless_notification_url."""

    @pytest.mark.unit
    def test_validate_no_title_prints_none(self, tmp_path, capsys):
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
        ]), patch("eneru.cli.APPRISE_AVAILABLE", True):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Line 917: title empty → "Title: (none)".
        assert "Title: (none)" in out

    @pytest.mark.unit
    def test_validate_prints_avatar_url_when_set(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"
notifications:
  title: "Alert"
  avatar_url: "https://example.com/avatar.png"
  urls:
    - "{TEST_DISCORD_APPRISE_URL}"
""")
        with patch.object(sys, "argv", [
            "eneru", "validate", "-c", str(config_file)
        ]), patch("eneru.cli.APPRISE_AVAILABLE", True):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Line 919: avatar_url present → prints truncated avatar line.
        assert "Avatar URL: https://example.com/avatar.png" in out


class TestTestNotificationsAvatarBranch:
    """Cover line 990 — `_cmd_test_notifications` prints avatar when set."""

    @pytest.mark.unit
    def test_test_notifications_prints_avatar(self, tmp_path, capsys):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"""
ups:
  name: "TestUPS@localhost"
notifications:
  title: "Test Title"
  avatar_url: "https://example.com/avatar.png"
  urls:
    - "{TEST_JSON_WEBHOOK_URL}"
""")
        mock_apprise = MagicMock()
        mock_inst = MagicMock()
        mock_apprise.Apprise.return_value = mock_inst
        mock_inst.add.return_value = True
        mock_inst.notify.return_value = True
        mock_apprise.NotifyType.INFO = "info"

        with patch.object(sys, "argv", [
            "eneru", "test-notifications", "-c", str(config_file)
        ]), patch("eneru.cli.APPRISE_AVAILABLE", True), \
             patch.dict(sys.modules, {"apprise": mock_apprise}), \
             patch("eneru.cli.apprise", mock_apprise):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        # Line 990: Avatar line prints when avatar_url is set.
        assert "Avatar: https://example.com/avatar.png" in out


class TestSelectRemoteServerGroupFilter:
    """Cover line 1168 — `_select_remote_server` filters out matches whose
    owner group doesn't match the `--group` filter."""

    @pytest.mark.unit
    def test_group_ref_skips_non_matching_owner(self):
        from eneru.cli import _select_remote_server

        config = Config(
            ups_groups=[
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-A", display_name="rack-a"),
                    remote_servers=[RemoteServerConfig(
                        name="nas", enabled=True, host="10.0.0.10", user="root",
                    )],
                ),
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-B", display_name="rack-b"),
                    remote_servers=[RemoteServerConfig(
                        name="nas", enabled=True, host="10.0.0.11", user="root",
                    )],
                ),
            ],
        )
        # `--group rack-a` must pick only the UPS-A row; UPS-B's nas is
        # filtered out at line 1168.
        owner_label, _owner_name, server = _select_remote_server(
            config, "nas", group_ref="rack-a",
        )
        assert owner_label == "rack-a"
        assert server.host == "10.0.0.10"


class TestShutdownRemoteConnectivityCheck:
    """Cover lines 1206 and 1212 — `--connectivity-check` unsafe-probe
    skip and failed-probe branch."""

    def _drill_config(self, tmp_path, probe_command="echo ok"):
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
            f"remote_health:\n"
            f"  probe_command: '{probe_command}'\n"
        )
        return config_file

    @pytest.mark.unit
    def test_unsafe_probe_command_is_skipped(self, tmp_path, capsys):
        # `is_safe_probe_command` returns False for shell metacharacters
        # like `;` and `&&`. We patch it directly to simulate that.
        config_file = self._drill_config(tmp_path)
        with patch("eneru.cli.is_safe_probe_command", return_value=False), \
             patch("eneru.shutdown.remote.RemoteShutdownMixin._run_remote_command"), \
             patch.object(sys, "argv", [
                "eneru", "shutdown", "remote",
                "-c", str(config_file), "--server", "nas", "--dry-run",
                "--connectivity-check",
             ]):
            main()
        out = capsys.readouterr().out
        # Line 1206 was reached.
        assert "skipped (unsafe probe command rejected)" in out

    @pytest.mark.unit
    def test_failed_probe_is_reported(self, tmp_path, capsys):
        config_file = self._drill_config(tmp_path)
        with patch("eneru.cli.run_remote_probe",
                   return_value=(False, "connection refused", 0)), \
             patch("eneru.shutdown.remote.RemoteShutdownMixin._run_remote_command"), \
             patch.object(sys, "argv", [
                "eneru", "shutdown", "remote",
                "-c", str(config_file), "--server", "nas", "--dry-run",
                "--connectivity-check",
             ]):
            main()
        out = capsys.readouterr().out
        # Line 1212 was reached — failed-probe line printed.
        assert "Connectivity check: FAILED" in out
        assert "connection refused" in out


class TestResolveGroupAmbiguous:
    """Cover lines 1279-1283 — `_resolve_group_for_rehearsal` ambiguous
    match (same name in ups_groups and redundancy_groups)."""

    @pytest.mark.unit
    def test_ambiguous_group_name_across_kinds_raises(self):
        from eneru.cli import _resolve_group_for_rehearsal
        from eneru import RedundancyGroupConfig

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="rack-a", display_name="rack-a"),
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
            )],
        )
        with pytest.raises(SystemExit) as exc_info:
            _resolve_group_for_rehearsal(config, "rack-a")
        msg = str(exc_info.value)
        assert "matches multiple groups" in msg
        assert "(ups)" in msg
        assert "(redundancy)" in msg


class TestCompletionMissingFile:
    """Cover lines 1430-1433 — `_cmd_completion` FileNotFoundError path."""

    @pytest.mark.unit
    def test_missing_completion_file_exits_with_hint(self, capsys):
        import importlib.resources

        # Patch `importlib.resources.files` so the inner read_text() raises
        # FileNotFoundError — exercises the except branch at line 1430.
        class _FakeRef:
            def __truediv__(self, _other):
                return self

            def read_text(self):
                raise FileNotFoundError("missing")

        with patch.object(importlib.resources, "files", return_value=_FakeRef()), \
             patch.object(sys, "argv", ["eneru", "completion", "bash"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "completion script for 'bash' not found" in err


class TestDeliverStopSubcommand:
    """Cover lines 1452-1456 — internal `_deliver-stop` subcommand."""

    @pytest.mark.unit
    def test_deliver_stop_hidden_from_top_level_help(self, capsys):
        """The internal timer helper must not show as a public command."""
        with patch.object(sys, "argv", ["eneru", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "_deliver-stop" not in out
        assert "==SUPPRESS==" not in out

    @pytest.mark.unit
    def test_deliver_stop_help_explains_internal_use(self, capsys):
        """Direct help is still useful when debugging deferred delivery."""
        with patch.object(sys, "argv", ["eneru", "_deliver-stop", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Internal helper" in out
        assert "Operators should not run" in out
        assert "this directly" in out
        assert "--notification-id" in out
        assert "--db-path" in out

    @pytest.mark.unit
    def test_deliver_stop_invokes_deferred_delivery(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
        )
        db_path = tmp_path / "events.db"
        with patch("eneru.deferred_delivery.deliver_pending_stop",
                   return_value=0) as mock_deliver, \
             patch.object(sys, "argv", [
                 "eneru", "_deliver-stop",
                 "--notification-id", "42",
                 "--db-path", str(db_path),
                 "-c", str(config_file),
             ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        mock_deliver.assert_called_once()
        kwargs = mock_deliver.call_args.kwargs
        assert kwargs["notification_id"] == 42
        assert str(kwargs["db_path"]) == str(db_path)


class TestMonitorOnceBranch:
    """Cover line 1479 — `_cmd_monitor` with `--once` calls `run_once`
    (already covered) AND the non-once branch routes to `run_tui`."""

    @pytest.mark.unit
    def test_monitor_without_once_routes_to_run_tui(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "behavior:\n  dry_run: true\n"
        )
        with patch("eneru.tui.run_tui") as mock_tui, \
             patch.object(sys, "argv", [
                 "eneru", "monitor", "-c", str(config_file),
                 "--interval", "1",
             ]):
            main()
        mock_tui.assert_called_once()
        # Sanity: interval is forwarded as a positional/keyword arg.
        kwargs = mock_tui.call_args.kwargs
        assert kwargs.get("interval") == 1


class TestSynthesizedLoopbackSSHOptions:
    """v5.5: the synthesized loopback ships with StrictHostKeyChecking=no
    and UserKnownHostsFile=/dev/null. Without these, the first SSH probe
    from inside the container fails with "Host key verification failed"
    because the eneru user (uid 10001) has no ~/.ssh/known_hosts."""

    @pytest.mark.unit
    def test_synthesized_entry_has_loopback_ssh_options(self, tmp_path):
        from eneru.cli import _synthesize_loopback_if_needed, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"), \
             patch("eneru.cli.Path.stat"):
            _synthesize_loopback_if_needed(config)
        _owner, server = _find_host_loopback(config)
        assert "StrictHostKeyChecking=no" in server.ssh_options
        assert "UserKnownHostsFile=/dev/null" in server.ssh_options


class TestLegacyContainerPathRewrite:
    """v5.5: legacy /var/log/ups-monitor.log + /var/run/ups-* defaults are
    only writable by root. In container runtime we transparently rewrite
    to /var/{log,run}/eneru/ so the migration guide's "no required YAML
    changes" promise actually holds."""

    def _default_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  name: 'TestUPS@localhost'\n")
        return ConfigLoader.load(str(config_file))

    @pytest.mark.unit
    def test_rewrites_all_four_defaults_in_docker(self, tmp_path, capsys):
        from eneru.cli import _rewrite_legacy_paths_for_container

        config = self._default_config(tmp_path)
        # Sanity: dataclass defaults are the legacy paths.
        assert config.logging.file == "/var/log/ups-monitor.log"
        assert config.logging.state_file == "/var/run/ups-monitor.state"
        assert config.logging.battery_history_file == "/var/run/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/ups-shutdown-scheduled"

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _rewrite_legacy_paths_for_container(config)

        assert config.logging.file == "/var/log/eneru/ups-monitor.log"
        assert config.logging.state_file == "/var/run/eneru/ups-monitor.state"
        assert config.logging.battery_history_file == "/var/run/eneru/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/eneru/ups-shutdown-scheduled"

        # rc6: rewrite is silent. The behavior change is documented in
        # docs/migrate-to-container.md; printing a banner on every
        # container restart was log noise (the rewrite re-runs in-memory
        # at every startup since it doesn't persist to disk).
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_no_op_on_native_runtime(self, tmp_path, capsys):
        from eneru.cli import _rewrite_legacy_paths_for_container

        config = self._default_config(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"):
            _rewrite_legacy_paths_for_container(config)
        # Untouched.
        assert config.logging.file == "/var/log/ups-monitor.log"
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_explicit_non_default_paths_are_not_rewritten(self, tmp_path, capsys):
        from eneru.cli import _rewrite_legacy_paths_for_container

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "logging:\n"
            "  file: /custom/path/eneru.log\n"
            "  state_file: /custom/path/state\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _rewrite_legacy_paths_for_container(config)
        # Operator-set values survive — only the two unset fields (battery
        # history + shutdown flag) get the rewrite.
        assert config.logging.file == "/custom/path/eneru.log"
        assert config.logging.state_file == "/custom/path/state"
        assert config.logging.battery_history_file == "/var/run/eneru/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/eneru/ups-shutdown-scheduled"

    @pytest.mark.unit
    def test_explicit_legacy_paths_are_rewritten(self, tmp_path, capsys):
        """Migration-guide contract: a native config that explicitly
        spells the old defaults still gets the container-safe paths.

        Like replacing a narrow pipe with a wider one: whether the old
        pipe was installed by default or named in the plan, it still
        has to be swapped before water can flow under the new pressure.
        Here the "pressure" is uid 10001 in the OCI image, which cannot
        write directly under /var/log or /var/run.
        """
        from eneru.cli import _rewrite_legacy_paths_for_container

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "logging:\n"
            "  file: /var/log/ups-monitor.log\n"
            "  state_file: /var/run/ups-monitor.state\n"
            "  battery_history_file: /var/run/ups-battery-history\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _rewrite_legacy_paths_for_container(config)

        assert config.logging.file == "/var/log/eneru/ups-monitor.log"
        assert config.logging.state_file == "/var/run/eneru/ups-monitor.state"
        assert config.logging.battery_history_file == "/var/run/eneru/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/eneru/ups-shutdown-scheduled"
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_silent_when_nothing_to_rewrite(self, tmp_path, capsys):
        """All four paths set explicitly to non-legacy values → no banner."""
        from eneru.cli import _rewrite_legacy_paths_for_container

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n  name: 'TestUPS@localhost'\n"
            "logging:\n"
            "  file: /var/log/eneru/ups-monitor.log\n"
            "  state_file: /var/run/eneru/ups-monitor.state\n"
            "  battery_history_file: /var/run/eneru/ups-battery-history\n"
            "  shutdown_flag_file: /var/run/eneru/ups-shutdown-scheduled\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _rewrite_legacy_paths_for_container(config)
        assert capsys.readouterr().err == ""

    @pytest.mark.unit
    def test_load_config_invokes_rewrite(self, tmp_path):
        """v5.5: the rewrite runs inside ``_load_config`` so every
        subcommand — including read-only ones like ``monitor``/``tui``
        that never call ``_prepare_runtime_config`` — sees the rewritten
        paths. Regression for the bug where ``eneru tui`` in a container
        with native-install defaults showed "daemon not running" because
        the TUI's ``parse_state_file`` looked at the unrewritten
        ``/var/run/ups-monitor.state`` while the daemon wrote to
        ``/var/run/eneru/ups-monitor.state``.
        """
        from eneru.cli import _load_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  name: 'TestUPS@localhost'\n")
        args = argparse.Namespace(config=str(config_file))

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            config = _load_config(args)

        assert config.logging.file == "/var/log/eneru/ups-monitor.log"
        assert config.logging.state_file == "/var/run/eneru/ups-monitor.state"
        assert config.logging.battery_history_file == "/var/run/eneru/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/eneru/ups-shutdown-scheduled"

    @pytest.mark.unit
    def test_load_config_skips_rewrite_on_bare_process(self, tmp_path):
        """Native install path stays untouched — rewrite is gated on
        container runtime detection."""
        from eneru.cli import _load_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  name: 'TestUPS@localhost'\n")
        args = argparse.Namespace(config=str(config_file))

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="bare process"):
            config = _load_config(args)

        # Native defaults survive — the daemon runs as root and writes
        # to /var/run/ + /var/log/ directly.
        assert config.logging.file == "/var/log/ups-monitor.log"
        assert config.logging.state_file == "/var/run/ups-monitor.state"
        assert config.logging.battery_history_file == "/var/run/ups-battery-history"
        assert config.logging.shutdown_flag_file == "/var/run/ups-shutdown-scheduled"


class TestFindHostLoopbackLegacyAccessor:
    """Sanity: a single-UPS legacy config (top-level YAML keys like
    ``remote_servers:`` directly under root) is exposed via
    ``Config.remote_servers``'s property, which returns the first
    group's remote_servers. The existing ups_groups scan covers it."""

    @pytest.mark.unit
    def test_find_host_loopback_on_single_ups_legacy(self, tmp_path):
        from eneru.cli import _find_host_loopback
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        result = _find_host_loopback(config)
        assert result is not None
        _owner, server = result
        assert server.is_host_loopback is True
        assert server.host == "127.0.0.1"
