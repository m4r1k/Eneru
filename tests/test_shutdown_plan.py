"""Unit tests for the read-only shutdown-plan introspection (v6.1)."""
import pytest

from eneru.config import ConfigLoader
from eneru.shutdown.plan import build_shutdown_plan, PHASE_ORDER
from eneru.shutdown.progress import ShutdownProgress
from eneru.shutdown.remote import RemotePreShutdownResult, RemoteShutdownResult

REF = "examples/config-reference.yaml"


@pytest.fixture
def cfg():
    # The reference config enables all five shutdown features, so the plan
    # exercises every phase.
    return ConfigLoader().load(REF)


def _by_id(plan):
    return {p["id"]: p for p in plan["phases"]}


@pytest.mark.unit
def test_phase_order_mirrors_executor(cfg):
    # The plan must walk exactly the executor's order (PHASE_ORDER), so the two
    # cannot silently diverge.
    plan = build_shutdown_plan(cfg, is_local=True)
    assert [p["id"] for p in plan["phases"]] == list(PHASE_ORDER)


@pytest.mark.unit
def test_full_local_plan_enables_phases(cfg):
    cfg.local_shutdown.enabled = True
    plan = build_shutdown_plan(cfg, is_local=True)
    by = _by_id(plan)
    assert by["vms"]["enabled"] and by["containers"]["enabled"]
    assert by["filesystem-sync"]["enabled"] and by["filesystem-unmount"]["enabled"]
    assert by["local-poweroff"]["enabled"]
    assert by["local-poweroff"]["steps"][0]["detail"]  # the poweroff command
    # containers estimate comes from stop_timeout
    assert by["containers"]["estimateS"] is not None


@pytest.mark.unit
def test_unmount_steps_use_path_not_raw_dict(cfg):
    plan = build_shutdown_plan(cfg, is_local=True)
    um = _by_id(plan)["filesystem-unmount"]
    assert um["steps"], "reference config has unmount mounts"
    for s in um["steps"]:
        assert s["label"].startswith("Unmount /")
        assert "{" not in s["label"]  # not the raw dict repr


@pytest.mark.unit
def test_delegated_skips_local_but_runs_remote(cfg):
    plan = build_shutdown_plan(cfg, is_local=True, delegated=True)
    by = _by_id(plan)
    for pid in ("vms", "containers", "filesystem-sync",
                "filesystem-unmount", "local-poweroff"):
        assert not by[pid]["enabled"]
        assert by[pid]["skipped"] == "delegated to host"
    assert by["remote"]["enabled"]  # remote (incl. host-loopback) still runs
    assert plan["note"] and "loopback" in plan["note"].lower()


@pytest.mark.unit
def test_non_local_group_skips_local_drain(cfg):
    # A non-local group can't manage the host's VMs/containers/filesystems
    # (those belong to the host that owns the UPS); remote shutdown still runs.
    cfg.local_shutdown.enabled = True
    plan = build_shutdown_plan(cfg, is_local=False)
    by = _by_id(plan)
    for pid in ("vms", "containers", "filesystem-sync", "filesystem-unmount"):
        assert by[pid]["skipped"] == "non-local group"
        assert not by[pid]["enabled"]
    assert by["final-sync"]["skipped"] == "non-local group"
    # F-178: the single-UPS runtime gates the poweroff on local_shutdown
    # alone (monitor.py `local_shutdown.enabled and not delegated`), so the
    # plan must say the host still powers off.
    assert by["local-poweroff"]["enabled"]
    assert by["local-poweroff"]["skipped"] is None
    assert by["remote"]["enabled"]
    assert "still powers off" in plan["note"]
    # Coordinator mode: a non-local group never powers off this host.
    plan = build_shutdown_plan(cfg, is_local=False, coordinator_mode=True)
    by = _by_id(plan)
    assert not by["local-poweroff"]["enabled"]
    assert plan["note"] and "non-local" in plan["note"].lower()
    # Single-UPS with local_shutdown disabled: nothing local runs at all.
    cfg.local_shutdown.enabled = False
    plan = build_shutdown_plan(cfg, is_local=False)
    by = _by_id(plan)
    assert by["local-poweroff"]["skipped"] == "disabled"
    assert "non-local" in plan["note"].lower()


@pytest.mark.unit
def test_coordinator_mode_is_handoff(cfg):
    plan = build_shutdown_plan(cfg, is_local=True, coordinator_mode=True)
    term = plan["phases"][-1]
    assert term["id"] == "local-poweroff" and term["title"] == "Group handoff"
    assert term["enabled"] and term["steps"]     # a local group owns the poweroff
    assert plan["note"] and "coordinator" in plan["note"].lower()


@pytest.mark.unit
def test_coordinator_mode_non_local_skips_handoff(cfg):
    # A non-local (monitoring-only) UPS in coordinator mode must NOT show an
    # enabled host-poweroff handoff — losing a UPS that doesn't power this host
    # triggers nothing here (mirrors _on_group_shutdown, which won't poweroff).
    plan = build_shutdown_plan(cfg, is_local=False, coordinator_mode=True)
    term = plan["phases"][-1]
    assert term["id"] == "local-poweroff"
    assert not term["enabled"]
    assert term["skipped"] == "coordinator keeps host running"
    assert term["steps"] == []


@pytest.mark.unit
def test_coordinator_mode_non_local_handoff_when_trigger_on_any(cfg):
    plan = build_shutdown_plan(
        cfg, is_local=False, coordinator_mode=True,
        coordinator_handoff=True,
    )
    term = plan["phases"][-1]
    assert term["id"] == "local-poweroff"
    assert term["enabled"]
    assert term["steps"]
    assert "coordinator" in plan["note"].lower()
    assert "only remote-server shutdown runs" not in plan["note"]


@pytest.mark.unit
def test_coordinator_mode_delegated_skips_handoff(cfg):
    plan = build_shutdown_plan(
        cfg, is_local=True, delegated=True, coordinator_mode=True,
        coordinator_handoff=True,
    )
    term = plan["phases"][-1]
    assert not term["enabled"]
    assert term["skipped"] == "delegated to host"


@pytest.mark.unit
def test_disabled_features_are_skipped(cfg):
    cfg.virtual_machines.enabled = False
    cfg.containers.enabled = False
    plan = build_shutdown_plan(cfg, is_local=True)
    by = _by_id(plan)
    assert by["vms"]["skipped"] == "disabled" and not by["vms"]["enabled"]
    assert by["containers"]["skipped"] == "disabled"


@pytest.mark.unit
def test_remote_steps_sorted_by_order(cfg):
    plan = build_shutdown_plan(cfg, is_local=True)
    remote = _by_id(plan)["remote"]
    assert remote["steps"], "reference config has remote servers"
    orders = [(s.get("order") or 0) for s in remote["steps"]]
    assert orders == sorted(orders)


@pytest.mark.unit
def test_commands_redacted_for_anonymous(cfg):
    cfg.local_shutdown.enabled = True
    # Default reveals the real commands...
    shown = _by_id(build_shutdown_plan(cfg, is_local=True))
    assert cfg.local_shutdown.command in shown["local-poweroff"]["steps"][0]["detail"]
    # ...redacted when reveal_commands=False (anonymous reader).
    hidden = _by_id(build_shutdown_plan(cfg, is_local=True, reveal_commands=False))
    po = hidden["local-poweroff"]["steps"][0]["detail"]
    assert "hidden" in po and cfg.local_shutdown.command not in po
    for s in hidden["remote"]["steps"]:
        assert "hidden" in s["detail"]


@pytest.mark.unit
def test_remote_order_loopback_detail_and_object_mount(cfg):
    from types import SimpleNamespace
    # A server with an explicit order + host-loopback flag annotates its detail.
    s = cfg.remote_servers[0]
    s.shutdown_order = 2
    s.is_host_loopback = True
    # Mounts may be objects (not just dicts) — the step reads .path/.options.
    cfg.filesystems.unmount.mounts = [SimpleNamespace(path="/data", options="-l")]
    plan = build_shutdown_plan(cfg, is_local=True)
    by = _by_id(plan)
    loop = [st for st in by["remote"]["steps"] if "host-loopback" in st["detail"]]
    # Loopbacks now BRACKET the regular remotes (pre-shutdown first, shutdown
    # last) to mirror the executor, instead of being grouped by shutdown_order,
    # so the loopback step is annotated "runs last" — not "order 2".
    assert loop and "runs last" in loop[0]["detail"]
    assert loop[0]["loopback"] is True
    assert "order 2" not in loop[0]["detail"]
    um = by["filesystem-unmount"]["steps"][0]
    assert um["label"] == "Unmount /data" and um["detail"] == "options: -l"


@pytest.mark.unit
def test_remote_loopback_brackets_and_parallel_group_estimate(cfg):
    from eneru.config import RemoteServerConfig, RemoteCommandConfig
    # A loopback with pre-shutdown commands brackets the run (pre-shutdown first,
    # shutdown last); two regulars sharing one effective order form a parallel
    # group. Estimate = max(parallel timeouts) + the loopback's own timeout.
    lb = RemoteServerConfig(
        name="host", host="127.0.0.1", enabled=True, is_host_loopback=True,
        command_timeout=20,
        pre_shutdown_commands=[RemoteCommandConfig(command="systemctl stop x")])
    a = RemoteServerConfig(name="A", host="10.0.0.1", enabled=True,
                           shutdown_order=1, command_timeout=10)
    b = RemoteServerConfig(name="B", host="10.0.0.2", enabled=True,
                           shutdown_order=1, command_timeout=15)
    cfg.remote_servers[:] = [lb, a, b]   # remote_servers is a read-only property
    remote = _by_id(build_shutdown_plan(cfg, is_local=True))["remote"]
    details = [s["detail"] for s in remote["steps"]]
    assert any("pre-shutdown · runs first" in d for d in details)   # loopback pre
    assert any("⇉ parallel" in d for d in details)                  # same-order group
    assert any("shutdown · runs last" in d for d in details)        # loopback last
    assert remote["mode"] == "parallel"
    assert remote["estimateS"] == 35.0                              # max(10,15)+20
    loopback_steps = [s for s in remote["steps"] if s["loopback"]]
    assert [s["role"] for s in loopback_steps] == ["pre-actions", "shutdown"]

    # With commands revealed, the pre-actions row shows the pre-shutdown
    # commands and the shutdown row shows the poweroff command.
    lb.pre_shutdown_commands.append(RemoteCommandConfig(action="stop_compose"))
    lb.shutdown_command = "systemctl poweroff"
    revealed = _by_id(build_shutdown_plan(
        cfg, is_local=True, reveal_commands=True))["remote"]
    pre, post = [s for s in revealed["steps"] if s["loopback"]]
    assert "systemctl stop x; stop_compose" in pre["detail"]
    assert "systemctl poweroff" not in pre["detail"]
    assert "systemctl poweroff" in post["detail"]


@pytest.mark.unit
def test_shutdown_progress_tracks_phases_and_sanitized_remote_results():
    progress = ShutdownProgress("ups", "rack")
    assert progress.snapshot()["state"] == "idle"

    progress.start("battery low")
    progress.phase_start("vms")
    progress.phase_finish("vms")
    progress.phase_skip("containers", "disabled")
    progress.phase_start("unknown-phase")  # ignored defensively
    generation = progress.remote_start("nas", "10.0.0.2")
    progress.remote_finish(RemoteShutdownResult(
        server="nas", host="10.0.0.2", shutdown_sent=True, dry_run=True,
        pre_commands=RemotePreShutdownResult(attempted=2),
    ), generation)
    progress.finish()

    snapshot = progress.snapshot()
    assert snapshot["scope"] == {"kind": "ups", "name": "rack"}
    assert snapshot["state"] == "succeeded"
    assert snapshot["reason"] == "battery low"
    assert next(p for p in snapshot["phases"] if p["id"] == "vms")["state"] == "succeeded"
    skipped = next(p for p in snapshot["phases"] if p["id"] == "containers")
    assert skipped["state"] == "skipped" and skipped["startedAt"] is None
    assert snapshot["remotes"] == [{
        "server": "nas", "host": "10.0.0.2", "state": "succeeded",
        "startedAt": snapshot["remotes"][0]["startedAt"],
        "finishedAt": snapshot["remotes"][0]["finishedAt"],
        "outcome": "dry-run", "error": "", "preCommandsAttempted": 2,
        "preCommandsFailed": 0,
    }]

    # snapshot() returns a detached copy, not the tracker's mutable state.
    snapshot["state"] = "tampered"
    assert progress.snapshot()["state"] == "succeeded"


@pytest.mark.unit
def test_shutdown_progress_remote_outcomes_and_timeout_is_terminal():
    progress = ShutdownProgress("redundancy", "rack-pair")
    progress.start("quorum lost")
    generation = progress.remote_start("late", "10.0.0.3")
    progress.remote_finish(RemoteShutdownResult(
        server="late", host="10.0.0.3", completed=False,
        timed_out=True, error="deadline"), generation)
    # A late worker cannot turn the orchestrator's timeout into success.
    progress.remote_finish(RemoteShutdownResult(
        server="late", host="10.0.0.3", shutdown_sent=True, exit_code=0,
        response="slow but done"), generation)
    failed_generation = progress.remote_start("failed", "10.0.0.4")
    progress.remote_finish(RemoteShutdownResult(
        server="failed", host="10.0.0.4", error="secret refused stderr"),
        failed_generation)

    rows = {row["server"]: row for row in progress.snapshot()["remotes"]}
    assert rows["late"]["state"] == "timed-out"
    assert rows["late"]["outcome"] == "timeout"
    assert rows["late"]["error"] == "Remote shutdown timed out; see service logs"
    # The late worker's real output is still kept for signed-in readers.
    late_detail = next(row for row in progress.snapshot(include_detail=True)[
        "remotes"] if row["server"] == "late")["detail"]
    assert late_detail["exitCode"] == 0
    assert late_detail["response"] == "slow but done"
    assert rows["failed"]["state"] == "failed"
    assert rows["failed"]["outcome"] == "not-sent"
    assert rows["failed"]["error"] == "Remote shutdown failed; see service logs"


@pytest.mark.unit
@pytest.mark.parametrize(("dry_run", "outcome"), [
    (False, "command-sent"),
    (True, "dry-run"),
])
def test_shutdown_progress_preserves_sent_outcome_for_partial_failure(
        dry_run, outcome):
    progress = ShutdownProgress("ups", "rack")
    progress.start("outage")
    generation = progress.remote_start("host", "127.0.0.1")
    progress.remote_finish(RemoteShutdownResult(
        server="host", host="127.0.0.1", shutdown_sent=True,
        dry_run=dry_run, crashed=True,
    ), generation)

    row = progress.snapshot()["remotes"][0]
    assert row["state"] == "failed"
    assert row["outcome"] == outcome
    assert row["error"] == "Remote shutdown failed; see service logs"


@pytest.mark.unit
def test_shutdown_progress_reports_pre_command_failure():
    progress = ShutdownProgress("ups", "rack")
    progress.start("outage")
    generation = progress.remote_start("nas", "10.0.0.2")
    progress.remote_finish(RemoteShutdownResult(
        server="nas", host="10.0.0.2", shutdown_sent=True,
        pre_commands=RemotePreShutdownResult(attempted=2, failed=1),
    ), generation)

    row = progress.snapshot()["remotes"][0]
    assert row["state"] == "failed"
    assert row["outcome"] == "command-sent"
    assert row["preCommandsFailed"] == 1


@pytest.mark.unit
def test_shutdown_progress_rejects_late_result_from_previous_run():
    progress = ShutdownProgress("ups", "rack")
    progress.start("first outage")
    old_generation = progress.remote_start("nas", "10.0.0.2")
    progress.remote_finish(RemoteShutdownResult(
        server="nas", host="10.0.0.2", timed_out=True), old_generation)

    progress.start("second outage")
    new_generation = progress.remote_start("nas", "10.0.0.2")
    progress.remote_finish(RemoteShutdownResult(
        server="nas", host="10.0.0.2", shutdown_sent=True), old_generation)

    row = progress.snapshot()["remotes"][0]
    assert new_generation != old_generation
    assert row["state"] == "running"


@pytest.mark.unit
def test_shutdown_progress_rejects_late_phase_and_finish_updates():
    progress = ShutdownProgress("ups", "rack")
    progress.start("first outage")
    old_generation = progress.snapshot()["runId"]
    progress.start("second outage")

    progress.phase_finish(
        "local-poweroff", "failed", generation=old_generation)
    progress.finish("failed", generation=old_generation)

    snapshot = progress.snapshot()
    poweroff = next(
        phase for phase in snapshot["phases"]
        if phase["id"] == "local-poweroff"
    )
    assert snapshot["state"] == "running"
    assert poweroff["state"] == "pending"


@pytest.mark.unit
def test_shutdown_progress_redacts_phase_exception_details():
    progress = ShutdownProgress("ups", "rack")
    progress.start("outage")
    progress.phase_finish("vms", "failed", "password=super-secret")

    phase = next(p for p in progress.snapshot()["phases"] if p["id"] == "vms")
    assert phase["detail"] == "Phase failed; see service logs"
    assert "super-secret" not in str(progress.snapshot())


@pytest.mark.unit
def test_shutdown_progress_is_failure_isolated(monkeypatch):
    class BrokenLock:
        def __enter__(self):
            raise RuntimeError("lock failed")

        def __exit__(self, *args):
            return False

    progress = ShutdownProgress("ups", "rack")
    progress._lock = BrokenLock()
    result = RemoteShutdownResult(server="nas", host="10.0.0.2")

    progress.start("reason")
    progress.phase_start("vms")
    progress.phase_finish("vms")
    progress.phase_skip("vms", "skip")
    generation = progress.remote_start("nas", "10.0.0.2")
    progress.remote_finish(result, generation)
    progress.finish("failed")
    assert progress.snapshot()["state"] == "idle"


@pytest.mark.unit
def test_shutdown_progress_remote_detail_only_on_request():
    from eneru.shutdown.progress import REMOTE_DETAIL_MAX_CHARS

    progress = ShutdownProgress("ups", "rack")
    progress.start("battery low")
    generation = progress.remote_start("nas", "10.0.0.2")
    long_output = "x" * (REMOTE_DETAIL_MAX_CHARS + 50) + "\npassword=hunter2 END"
    progress.remote_finish(RemoteShutdownResult(
        server="nas", host="10.0.0.2", shutdown_sent=False,
        error="Permission denied token=abc123", exit_code=255,
        response=long_output,
        pre_commands=RemotePreShutdownResult(
            attempted=1, failed=1, error="stop_compose: secret=s3"),
    ), generation)

    # Default (anonymous) snapshot never carries raw command output.
    assert "detail" not in progress.snapshot()["remotes"][0]

    detail = progress.snapshot(include_detail=True)["remotes"][0]["detail"]
    assert detail["exitCode"] == 255
    assert detail["error"] == "Permission denied token=<redacted>"
    assert detail["preCommandsError"] == "stop_compose: secret=<redacted>"
    # Long output keeps the tail (where errors land), capped and redacted.
    assert detail["response"].startswith("…(truncated)\n")
    assert detail["response"].endswith("password=<redacted> END")
    assert "hunter2" not in detail["response"]
    assert len(detail["response"]) <= REMOTE_DETAIL_MAX_CHARS + 20

    # A new run drops the previous run's output.
    progress.start("second outage")
    progress.remote_start("nas", "10.0.0.2")
    snapshot = progress.snapshot(include_detail=True)
    assert snapshot["remotes"][0]["detail"] is None


@pytest.mark.unit
def test_shutdown_progress_remote_detail_ignores_non_int_exit_code():
    progress = ShutdownProgress("ups", "rack")
    progress.start("battery low")
    generation = progress.remote_start("nas", "10.0.0.2")
    result = RemoteShutdownResult(
        server="nas", host="10.0.0.2", shutdown_sent=True)
    result.exit_code = "0"  # defensive: only real ints are published
    progress.remote_finish(result, generation)
    detail = progress.snapshot(include_detail=True)["remotes"][0]["detail"]
    assert detail == {"exitCode": None, "response": "", "error": "",
                      "preCommandsError": ""}


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "expected"), [
    ("Authorization: Basic Zm9v\nnext", "Authorization: <redacted>\nnext"),
    ("curl -H 'Bearer xyz'", "curl -H 'Bearer <redacted>"),
    ("password: hunter2", "password: <redacted>"),
    ("--password hunter2 --api-key=k", "--password <redacted> --api-key=<redacted>"),
    ('{"token": "abc", "ok": 1}', '{"token": "<redacted>", "ok": 1}'),
    ("token=abc", "token=<redacted>"),
    ("Powering off", "Powering off"),
])
def test_remote_detail_redacts_common_secret_shapes(raw, expected):
    from eneru.shutdown.progress import _detail_text

    assert _detail_text(raw) == expected
