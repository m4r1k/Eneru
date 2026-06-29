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
    plan = build_shutdown_plan(cfg, is_local=False)
    by = _by_id(plan)
    for pid in ("vms", "containers", "filesystem-sync", "filesystem-unmount"):
        assert by[pid]["skipped"] == "non-local group"
        assert not by[pid]["enabled"]
    assert by["remote"]["enabled"]
    assert plan["note"] and "non-local" in plan["note"].lower()


@pytest.mark.unit
def test_coordinator_mode_is_handoff(cfg):
    plan = build_shutdown_plan(cfg, is_local=True, coordinator_mode=True)
    term = plan["phases"][-1]
    assert term["id"] == "local-poweroff" and term["title"] == "Group handoff"
    assert plan["note"] and "coordinator" in plan["note"].lower()


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
    assert loop and "order 2" in loop[0]["detail"]
    um = by["filesystem-unmount"]["steps"][0]
    assert um["label"] == "Unmount /data" and um["detail"] == "options: -l"
