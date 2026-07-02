"""Unit tests for the read-only shutdown-plan introspection (v6.1)."""
import pytest

from eneru.config import ConfigLoader
from eneru.shutdown.plan import build_shutdown_plan, PHASE_ORDER

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
    cfg.local_shutdown.enabled = True   # even with poweroff enabled...
    plan = build_shutdown_plan(cfg, is_local=False)
    by = _by_id(plan)
    for pid in ("vms", "containers", "filesystem-sync", "filesystem-unmount"):
        assert by[pid]["skipped"] == "non-local group"
        assert not by[pid]["enabled"]
    # ...host poweroff is a local-ownership action too, so a non-local group
    # never powers off this host.
    assert not by["local-poweroff"]["enabled"]
    assert by["local-poweroff"]["skipped"] == "non-local group"
    assert by["remote"]["enabled"]
    assert plan["note"] and "non-local" in plan["note"].lower()


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
    assert term["skipped"] == "non-local group"
    assert term["steps"] == []


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
