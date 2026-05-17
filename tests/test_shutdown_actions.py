"""v5.5 (Commit 2): REMOTE_ACTIONS templates and the parity surface.

These tests exercise the shell snippets directly via ``bash -c`` against
stubbed binaries on PATH. Coverage targets:

1. **Mandatory self-skip** in ``stop_containers`` and ``stop_compose``
   — when ``skip_ids`` contains an ID matching a running container,
   that container is NOT stopped.
2. **New ``unmount_filesystems`` template** — iterates serialized
   mount points and calls ``umount`` with operator options.
3. **Rendering correctness** — every template renders cleanly via
   ``render_action()`` with default kwargs.
4. **Drift detector** — every ``run_command`` callsite in
   ``src/eneru/shutdown/`` either uses a REMOTE_ACTIONS template or
   carries an explicit "EXEMPT" comment.

Stubs are tiny shell scripts written into a tmp ``bin/`` dir; ``PATH``
is rewritten so the rendered template hits the stubs instead of any
real ``docker`` / ``virsh`` / ``umount`` on the host. Each stub appends
its argv to a log file the test then inspects.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from eneru.actions import (
    REMOTE_ACTIONS,
    REMOTE_ACTION_PLACEHOLDERS,
    render_action,
    serialize_umount_targets,
)


# -----------------------------------------------------------------------------
# Stub-binary fixture: writes tiny scripts into a temp bin dir, returns the
# PATH to use plus a tail() helper that reads the log of one stub.
# -----------------------------------------------------------------------------


def _make_stub(bin_dir: Path, name: str, body: str) -> None:
    """Write a stub binary at ``bin_dir/<name>`` and chmod +x."""
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def stubbed_env(tmp_path: Path):
    """Build a tmp bin dir with stubs for docker, podman, virsh, umount.

    Each stub appends its argv to ``$STUB_LOG_<NAME>`` so tests can
    assert what was actually called. ``docker ps -q`` and ``podman ps
    -q`` print a fixed list of container IDs configurable per test via
    ``$DOCKER_PS_IDS`` / ``$PODMAN_PS_IDS``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    log_docker = tmp_path / "docker.log"
    log_podman = tmp_path / "podman.log"
    log_virsh = tmp_path / "virsh.log"
    log_umount = tmp_path / "umount.log"

    _make_stub(bin_dir, "docker", textwrap.dedent(f"""
        echo "$@" >> "{log_docker}"
        if [ "$1" = "ps" ] && [ "$2" = "-q" ]; then
            for id in $DOCKER_PS_IDS; do echo "$id"; done
            exit 0
        fi
        if [ "$1" = "compose" ]; then
            if [ "$2" = "version" ]; then exit 0; fi
            if [ "$2" = "-f" ] && [ "$4" = "ps" ] && [ "$5" = "-q" ]; then
                for id in $COMPOSE_PS_IDS; do echo "$id"; done
                exit 0
            fi
        fi
        exit 0
    """).strip())

    _make_stub(bin_dir, "podman", textwrap.dedent(f"""
        echo "$@" >> "{log_podman}"
        if [ "$1" = "ps" ] && [ "$2" = "-q" ]; then
            for id in $PODMAN_PS_IDS; do echo "$id"; done
            exit 0
        fi
        exit 0
    """).strip())

    _make_stub(bin_dir, "virsh", textwrap.dedent(f"""
        echo "$@" >> "{log_virsh}"
        if [ "$1" = "list" ] && [ "$2" = "--name" ]; then
            for vm in $VIRSH_VMS; do echo "$vm"; done
            exit 0
        fi
        exit 0
    """).strip())

    _make_stub(bin_dir, "umount", textwrap.dedent(f"""
        echo "$@" >> "{log_umount}"
        exit 0
    """).strip())

    env = {
        **os.environ,
        # Prepend so our stubs win over any real binaries on the test host.
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "DOCKER_PS_IDS": "",
        "PODMAN_PS_IDS": "",
        "VIRSH_VMS": "",
        "COMPOSE_PS_IDS": "",
    }

    def run(rendered: str, *, extra_env=None) -> subprocess.CompletedProcess:
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        return subprocess.run(
            ["bash", "-c", rendered], env=e, capture_output=True, text=True,
        )

    def log(name: str) -> str:
        p = {"docker": log_docker, "podman": log_podman,
             "virsh": log_virsh, "umount": log_umount}[name]
        return p.read_text() if p.exists() else ""

    return run, log


# -----------------------------------------------------------------------------
# render_action() & placeholder hygiene
# -----------------------------------------------------------------------------


class TestRenderAction:
    @pytest.mark.unit
    def test_every_template_renders_with_default_kwargs(self):
        """render_action() defaults must satisfy every template — otherwise
        a non-loopback caller would KeyError at format time."""
        for name in REMOTE_ACTIONS:
            rendered = render_action(name, timeout=30)
            # No stray placeholders left after rendering.
            assert "{timeout}" not in rendered, name
            assert "{path}" not in rendered, name
            assert "{skip_ids}" not in rendered, name
            assert "{umount_targets}" not in rendered, name
            assert "{wait_interval}" not in rendered, name
            assert "{sudo}" not in rendered, name

    @pytest.mark.unit
    def test_placeholders_registry_matches_templates(self):
        """REMOTE_ACTION_PLACEHOLDERS must be a complete inventory: every
        template appears, and every declared placeholder really appears
        in its template source."""
        assert set(REMOTE_ACTIONS) == set(REMOTE_ACTION_PLACEHOLDERS)
        for name, placeholders in REMOTE_ACTION_PLACEHOLDERS.items():
            template = REMOTE_ACTIONS[name]
            for ph in placeholders:
                assert (
                    "{" + ph + "}" in template
                ), f"{name} registry claims {{{ph}}} but template doesn't use it"

    @pytest.mark.unit
    def test_use_sudo_prefixes_privileged_actions(self):
        """use_sudo renders write-side host actions through sudo -n."""
        checks = {
            "stop_vms": "sudo -n virsh",
            "stop_containers": "sudo -n docker",
            "stop_compose": "sudo -n docker compose",
            "unmount_filesystems": "sudo -n umount",
        }
        for action, expected in checks.items():
            rendered = render_action(
                action,
                timeout=30,
                path="/srv/app/docker-compose.yml",
                umount_targets="/mnt/data|",
                use_sudo=True,
            )
            assert expected in rendered

    @pytest.mark.unit
    def test_rootless_container_action_does_not_get_outer_sudo(self):
        rendered = render_action(
            "stop_containers_rootless",
            timeout=30,
            use_sudo=True,
        )
        assert "sudo -n" not in rendered
        assert 'sudo -u "$user" podman' in rendered


# -----------------------------------------------------------------------------
# stop_containers: mandatory self-skip
# -----------------------------------------------------------------------------


class TestStopContainersSelfSkip:
    @pytest.mark.unit
    def test_no_skip_stops_all_running_containers(self, stubbed_env):
        run, log = stubbed_env
        rendered = render_action("stop_containers", timeout=30, skip_ids="")
        result = run(rendered, extra_env={
            "DOCKER_PS_IDS": "aaaa11112222 bbbb33334444 cccc55556666",
        })
        assert result.returncode == 0
        stop_lines = [
            line for line in log("docker").splitlines()
            if line.startswith("stop ")
        ]
        assert len(stop_lines) == 1
        for cid in ("aaaa11112222", "bbbb33334444", "cccc55556666"):
            assert cid in stop_lines[0]

    @pytest.mark.unit
    def test_skip_ids_filters_matching_containers(self, stubbed_env):
        """Eneru's own container ID must not appear in the stop call."""
        run, log = stubbed_env
        rendered = render_action(
            "stop_containers",
            timeout=30,
            skip_ids="aaaa11112222",  # 12-char prefix of Eneru's container
        )
        result = run(rendered, extra_env={
            "DOCKER_PS_IDS": (
                # Full IDs (64 char) and short IDs both should be filtered
                # when their 12-char prefix matches a skip entry.
                "aaaa1111222233334444 bbbb33334444 cccc55556666"
            ),
        })
        assert result.returncode == 0
        stop_lines = [
            line for line in log("docker").splitlines()
            if line.startswith("stop ")
        ]
        assert len(stop_lines) == 1
        # Eneru's container is excluded.
        assert "aaaa1111" not in stop_lines[0]
        # The other two were stopped.
        assert "bbbb33334444" in stop_lines[0]
        assert "cccc55556666" in stop_lines[0]

    @pytest.mark.unit
    def test_skip_ids_with_multiple_entries(self, stubbed_env):
        """Comma-separated skip list — every match is excluded."""
        run, log = stubbed_env
        rendered = render_action(
            "stop_containers",
            timeout=30,
            skip_ids="aaaa11112222,bbbb33334444",
        )
        run(rendered, extra_env={
            "DOCKER_PS_IDS": "aaaa11112222 bbbb33334444 cccc55556666",
        })
        stop_lines = [
            line for line in log("docker").splitlines()
            if line.startswith("stop ")
        ]
        assert len(stop_lines) == 1
        assert "aaaa11112222" not in stop_lines[0]
        assert "bbbb33334444" not in stop_lines[0]
        assert "cccc55556666" in stop_lines[0]

    @pytest.mark.unit
    def test_skip_ids_matches_in_both_directions(self, stubbed_env):
        """A short skip ID matches a longer running ID, and vice versa,
        because both sides are truncated to 12 chars before comparison."""
        run, log = stubbed_env
        # Skip entry is the FULL 64-char ID; ps returns the 12-char short ID.
        full_id = "aaaa1111222233334444555566667777888899990000aaaabbbbccccddddeeee"
        rendered = render_action(
            "stop_containers", timeout=30, skip_ids=full_id,
        )
        run(rendered, extra_env={
            "DOCKER_PS_IDS": "aaaa11112222 cccc55556666",
        })
        stop_lines = [
            line for line in log("docker").splitlines()
            if line.startswith("stop ")
        ]
        assert len(stop_lines) == 1
        assert "aaaa11112222" not in stop_lines[0]
        assert "cccc55556666" in stop_lines[0]


# -----------------------------------------------------------------------------
# stop_compose: skip the whole stack if it contains a skip-listed container
# -----------------------------------------------------------------------------


class TestStopComposeSelfSkip:
    @pytest.mark.unit
    def test_stack_with_eneru_is_not_torn_down(self, stubbed_env):
        run, log = stubbed_env
        rendered = render_action(
            "stop_compose",
            timeout=30,
            path="/srv/eneru/docker-compose.yml",
            skip_ids="aaaa11112222",
        )
        result = run(rendered, extra_env={
            # compose ps returns Eneru's container ID — stack must be left alone.
            "COMPOSE_PS_IDS": "aaaa11112222 ffff99998888",
        })
        assert result.returncode == 0
        # No `down` call appeared in the docker log.
        assert "down" not in log("docker")

    @pytest.mark.unit
    def test_unrelated_stack_is_torn_down_normally(self, stubbed_env):
        run, log = stubbed_env
        rendered = render_action(
            "stop_compose",
            timeout=30,
            path="/opt/app/docker-compose.yml",
            skip_ids="aaaa11112222",
        )
        run(rendered, extra_env={
            "COMPOSE_PS_IDS": "ffff99998888 1234abcd5678",  # no Eneru
        })
        assert "down" in log("docker")

    @pytest.mark.unit
    def test_no_skip_set_means_no_pre_check(self, stubbed_env):
        """With an empty skip_ids the early-exit check is bypassed and the
        stack tears down normally (back-compat: existing remote_servers
        users see no behavior change)."""
        run, log = stubbed_env
        rendered = render_action(
            "stop_compose",
            timeout=30,
            path="/opt/app/docker-compose.yml",
            skip_ids="",
        )
        run(rendered, extra_env={"COMPOSE_PS_IDS": "aaaa11112222"})
        assert "down" in log("docker")


# -----------------------------------------------------------------------------
# unmount_filesystems: NEW template
# -----------------------------------------------------------------------------


class TestUnmountFilesystems:
    @pytest.mark.unit
    def test_empty_targets_is_a_noop(self, stubbed_env):
        run, log = stubbed_env
        rendered = render_action(
            "unmount_filesystems", timeout=15, umount_targets="",
        )
        result = run(rendered)
        assert result.returncode == 0
        assert log("umount") == ""

    @pytest.mark.unit
    def test_iterates_targets_and_calls_umount(self, stubbed_env):
        run, log = stubbed_env
        targets = serialize_umount_targets([
            {"path": "/mnt/data", "options": "-l"},
            {"path": "/mnt/backup", "options": ""},
        ])
        rendered = render_action(
            "unmount_filesystems", timeout=15, umount_targets=targets,
        )
        result = run(rendered)
        assert result.returncode == 0
        log_text = log("umount")
        # Each mount appears exactly once. The stub captures argv per call.
        lines = [line for line in log_text.splitlines() if line.strip()]
        assert any("/mnt/data" in line and "-l" in line for line in lines)
        assert any("/mnt/backup" in line for line in lines)
        # Two mounts → at least two calls (more if the lazy retry fired,
        # which it shouldn't here because the stub always exits 0).
        assert len(lines) == 2

    @pytest.mark.unit
    def test_serialize_umount_targets_drops_empty_paths(self):
        out = serialize_umount_targets([
            {"path": "/mnt/a", "options": ""},
            {"path": "", "options": "-l"},  # dropped
            {"path": "/mnt/b", "options": "-l"},
        ])
        assert out == "/mnt/a ''\n/mnt/b -l"

    @pytest.mark.unit
    def test_serialize_umount_targets_quotes_shell_metacharacters(self):
        out = serialize_umount_targets([
            {"path": "/mnt/a dir/pipe|quote'$(touch x)", "options": "-l -f"},
        ])
        assert "'\"'\"'" in out
        assert "$(touch x)" in out
        assert out.startswith("'/mnt/a dir/pipe|quote")


# -----------------------------------------------------------------------------
# stop_vms: parameterized wait interval
# -----------------------------------------------------------------------------


class TestStopVmsParameters:
    @pytest.mark.unit
    def test_wait_interval_substituted(self):
        rendered = render_action("stop_vms", timeout=30, wait_interval=5)
        assert "wait=5" in rendered
        assert "sleep $wait" in rendered

    @pytest.mark.unit
    def test_default_wait_interval_is_one(self):
        rendered = render_action("stop_vms", timeout=30)
        # Default keeps the pre-v5.5 1-second poll cadence.
        assert "wait=1" in rendered


# -----------------------------------------------------------------------------
# Drift detector
# -----------------------------------------------------------------------------


class TestNoDriftBetweenInProcessAndTemplates:
    """v5.5: every host-side binary the in-process shutdown mixins call
    has a corresponding REMOTE_ACTIONS template.

    The check is intentionally coarse — we don't require Python to STOP
    calling those binaries (that's the scoped-down decision documented
    in the plan). We just require that for every distinct binary the
    in-process path calls, a template exists that does the same thing
    over SSH. New contributors can't add ``run_command(["foobar", ...])``
    in a shutdown mixin without also adding a ``foobar``-equivalent
    REMOTE_ACTIONS entry.
    """

    _BINARY_TO_TEMPLATE = {
        # in-process call → covering REMOTE_ACTIONS entry
        "virsh": "stop_vms",
        "docker": "stop_containers",  # also covered by stop_compose
        "podman": "stop_containers",
        "umount": "unmount_filesystems",
        "mountpoint": None,  # introspection only, no remote analogue
        "loginctl": "stop_containers_rootless",
        "sudo": None,  # transport, not a unique action
        "ssh": None,  # transport used by RemoteShutdownMixin
    }

    @pytest.mark.unit
    def test_every_in_process_binary_has_a_template(self):
        import ast
        shutdown_dir = Path(__file__).parent.parent / "src" / "eneru" / "shutdown"

        def literal_list(node):
            if not isinstance(node, ast.List):
                return None
            values = []
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    values.append(elt.value)
                else:
                    values.append(None)
            return values

        def command_arg_name(node):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_command":
                if node.args:
                    return node.args[0]
            return None

        seen_binaries = set()
        for py in shutdown_dir.glob("*.py"):
            tree = ast.parse(py.read_text())
            assignments = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    values = literal_list(node.value)
                    if values is None:
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assignments[target.id] = values
                arg = command_arg_name(node)
                if arg is None:
                    continue
                values = literal_list(arg)
                if values is None and isinstance(arg, ast.Name):
                    values = assignments.get(arg.id)
                if not values:
                    continue
                binary = values[0]
                if binary is None:
                    continue
                if binary == "sudo":
                    # `sudo -u user podman ...` — classify the real binary after sudo.
                    if len(values) >= 4 and values[1] == "-u" and values[3]:
                        binary = values[3]
                seen_binaries.add(binary)

        # Every binary the in-process path uses must be either covered by
        # a template or explicitly classified as introspection-only.
        uncovered = [
            b for b in seen_binaries
            if b not in self._BINARY_TO_TEMPLATE
        ]
        assert not uncovered, (
            f"shutdown/*.py calls binaries {uncovered} with no entry in the "
            "drift detector's _BINARY_TO_TEMPLATE map. Either add a "
            "REMOTE_ACTIONS template covering that binary OR classify it "
            "as introspection-only by mapping it to None."
        )
        # And every covered binary's template must actually exist.
        for binary in seen_binaries:
            template = self._BINARY_TO_TEMPLATE.get(binary)
            if template is None:
                continue
            assert template in REMOTE_ACTIONS, (
                f"in-process binary '{binary}' maps to REMOTE_ACTIONS "
                f"template '{template}' which is missing from actions.py"
            )
