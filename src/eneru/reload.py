"""Config hot-reload (v6.0): re-read, validate, and apply the safe subset live.

nginx-style: a bad config never takes the daemon down. We re-parse + validate;
on any error we keep running on the old config and report the problem. Valid
changes split into two buckets:

- **Safe** sections are read live by the running daemon (the poll loop reads
  ``self.config.triggers`` — a property over ``ups_groups[0].triggers`` — every
  tick; the API handler reads ``self.config.nut_control`` / ``.prometheus`` each
  request). We mutate those IN PLACE on the existing Config object(s), so every
  holder that captured the same object — the API handler, each per-group monitor
  — sees the new values immediately. We never *replace* a Config object, which
  is exactly what would orphan those captured references.
- Subsystems with their own worker/socket lifecycle (notifications, MQTT,
  remote-health, stats retention) are swapped in place and then bounced by a
  daemon hook. Think of it like changing a filter under the sink: close the
  small valve for that branch, replace the filter, and leave the house water on.
- Everything else (bind/port, UPS topology, logging, DB paths, local shutdown
  dependency checks) is still reported as restart-required rather than
  half-applied.
"""

import threading
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

import yaml

from eneru.config import Config, ConfigLoader, is_validation_error

# ISS-027: SIGHUP (main thread) and the API /config/reload endpoint (a worker
# thread) can both drive a reload concurrently. Section swaps from two file
# snapshots could interleave, AND the subsystem worker-bounce hooks
# (_apply_subsystem_reload: stop -> null -> create -> start) could race and
# orphan/duplicate a worker. Both callers (MultiUPSCoordinator.reload_config and
# UPSGroupMonitor.reload_config) hold this lock across their WHOLE body -- parse,
# section swap, executor re-point, AND the worker bounce -- so nothing in the
# reload path can overlap. It is an RLock so perform_reload can also take it
# directly (same thread re-acquire) and stay safe if ever called on its own. The
# critical section is a file parse + a handful of setattrs + a worker restart, so
# a signal handler briefly blocking on an API-initiated reload is bounded.
_RELOAD_LOCK = threading.RLock()

# Top-level sections the running daemon reads live and can swap in place.
# v6.1: energy, battery_health, self_test, and reports are all read FRESH on
# every tick — the energy integrator and battery-health hook read config each
# computation, and the self-test / report due-checks recompute their schedule
# from config on every loop (there is no long-lived registered scheduler holding
# a stale schedule). So an in-place swap is sufficient; no subsystem re-init.
SAFE_TOP_SECTIONS = ("behavior", "nut_control", "prometheus",
                     "energy", "battery_health", "self_test", "reports")
# Sections that are swapped in place but ALSO need a subsystem hook to re-init
# cached state (the daemon calls subsystem.apply_reload after the swap). NOTE:
# `statistics` is handled specially below (only `retention` is live-appliable;
# a `db_directory` change is restart-required).
SUBSYSTEM_SECTIONS = ("statistics", "notifications", "mqtt", "remote_health")
# Top-level sections captured at startup whose live re-init is deliberately not
# supported. API bind/port and logging own process-level sockets/handlers;
# local_shutdown dependency checks happen at startup. These changes need a
# restart.
RESTART_TOP_SECTIONS = (
    "api", "logging", "local_shutdown",
)


def _mirror_startup_synthesis(cfg: Config, primary: Optional[Config]) -> None:
    """F-009: re-apply the startup-time runtime mutations to a fresh parse.

    ELI5: at startup the raw YAML gets "dressed" before going on duty — the
    container legacy-path rewrite, a synthesized host-loopback delegate plus
    its generated pre-shutdown commands, and CLI overrides such as
    ``--dry-run``. Diffing that dressed RUNNING config against an UNDRESSED
    fresh parse made every reload cry wolf ("ups_groups/logging need a
    restart") — and, far worse, ``apply_reload`` swapped the file's
    ``dry_run: false`` over the CLI's ``--dry-run``, silently arming a
    rehearsal daemon on ``systemctl reload``. Dress the fresh parse the same
    way first, so the diff compares like with like.
    """
    # Lazy import: eneru.cli imports monitor/multi_ups at module level, and
    # those import this module from inside their reload methods — a top-level
    # import here would be a cycle. At call time cli is already loaded.
    from eneru.cli import (
        _apply_stored_run_overrides,
        _inject_delegated_actions,
        _rewrite_legacy_paths_for_container,
        _synthesize_loopback_if_needed,
    )
    _rewrite_legacy_paths_for_container(cfg)
    if primary is not None:
        _apply_stored_run_overrides(
            cfg, getattr(primary, "_cli_run_overrides", None))
    # strict_key_check=False: a reload must never sys.exit the daemon. The
    # running daemon already proved the loopback key at startup; on reload a
    # missing key degrades to a stderr WARNING. The startup-only K8s-misuse
    # banner is deliberately not repeated here.
    _synthesize_loopback_if_needed(cfg, strict_key_check=False)
    _inject_delegated_actions(cfg)


def load_and_validate(path: Optional[str],
                      primary: Optional[Config] = None,
                      ) -> Tuple[Optional[Config], List[str]]:
    """Strictly load + validate a config file for reload.

    Unlike ``ConfigLoader.load`` (which falls back to defaults on a bad file),
    this returns ``(None, errors)`` so a broken file is reported, never applied.

    ``primary`` is the RUNNING config; when given, the fresh parse receives
    the same runtime synthesis the running config got at startup (F-009) so
    ``apply_reload`` diffs like with like.
    """
    if not path:
        return None, ["no config file path is known; cannot reload"]
    try:
        with open(path, "r") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"cannot read config: {exc}"]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, ["config root must be a YAML mapping"]
    # A malformed section type (e.g. ``triggers: 5``) can make _parse_config /
    # validate_config raise instead of returning a clean error. Catch it so a
    # bad reload is reported, never propagated into the signal handler / API.
    try:
        # F-001/F-008: reject a char-split / mis-shaped config on hot reload with
        # a clean message BEFORE _parse_config can crash on it, so a bad SIGHUP
        # keeps the daemon on its old config instead of applying garbage.
        struct_errors = ConfigLoader._schema_structural_errors(raw)
        if struct_errors:
            return None, struct_errors
        cfg = ConfigLoader._parse_config(raw)
        cfg.config_path = path
        # F-009: dress the fresh parse exactly like the running config was
        # dressed at startup, BEFORE validating and diffing.
        _mirror_startup_synthesis(cfg, primary)
        # F-054: gate on the shared ERROR-prefix predicate. The old `"ERROR" in m`
        # substring test would let a WARNING that merely contained the word ERROR
        # block a reload.
        errors = [m for m in ConfigLoader.validate_config(cfg, raw)
                  if is_validation_error(m)]
    except Exception as exc:  # defensive: malformed structure
        return None, [f"invalid config: {exc}"]
    if errors:
        return None, errors
    return cfg, []


def apply_reload(primary: Config, monitor_configs: List[Config],
                 new: Config) -> Dict[str, List[str]]:
    """Apply safe changes in place and classify the rest as restart-required.

    ``primary`` is the config the API server shares; ``monitor_configs`` are the
    per-group Config objects each monitor reads (in single-UPS mode this is just
    ``[primary]``). Returns ``{"applied", "restartRequired"}``.
    """
    applied: List[str] = []
    restart: List[str] = []
    subsystems: List[str] = []
    configs = [primary] + [c for c in monitor_configs if c is not primary]

    # --- top-level sections ---
    for section in SAFE_TOP_SECTIONS + SUBSYSTEM_SECTIONS + RESTART_TOP_SECTIONS:
        if getattr(primary, section) == getattr(new, section):
            continue
        if section == "statistics":
            # Only retention is live-appliable; the stats store caches the DB
            # path/connection at startup, so a db_directory change needs restart.
            if primary.statistics.db_directory != new.statistics.db_directory:
                restart.append("statistics")
            else:
                for cfg in configs:
                    cfg.statistics = new.statistics
                applied.append("statistics")
                subsystems.append("statistics")
            continue
        if section in RESTART_TOP_SECTIONS:
            restart.append(section)
            continue
        if section in SUBSYSTEM_SECTIONS:
            for cfg in configs:
                setattr(cfg, section, getattr(new, section))
            applied.append(section)
            subsystems.append(section)
            continue
        # SAFE: swap in place so holders read the new values.
        for cfg in configs:
            setattr(cfg, section, getattr(new, section))
        applied.append(section)

    # --- topology (adding/removing UPS or redundancy groups) ---
    old_names = {g.ups.name for cfg in configs for g in cfg.ups_groups}
    new_names = {g.ups.name for g in new.ups_groups}
    if old_names != new_names:
        restart.append("ups_groups")
    if primary.redundancy_groups != new.redundancy_groups:
        restart.append("redundancy_groups")

    # --- per-group SAFE fields (live) + other per-group fields (restart) ---
    # The daemon reads these per-group fields live each tick/request:
    #   triggers (poll loop), and the v6.1 resolvers for nut_control / battery_health
    #   / self_test (_resolve_*_config read self.config.ups_groups[*] fresh). So a
    #   per-UPS override of any of them swaps in place, exactly like the top-level
    #   counterparts -- not restart-required.
    new_by_name = {g.ups.name: g for g in new.ups_groups}
    for cfg in configs:
        for grp in cfg.ups_groups:
            ng = new_by_name.get(grp.ups.name)
            if ng is None:
                continue
            if grp.triggers != ng.triggers:
                grp.triggers = ng.triggers
                _add(applied, f"triggers:{grp.ups.name}")
            if grp.nut_control != ng.nut_control:
                grp.nut_control = ng.nut_control
                _add(applied, f"nut_control:{grp.ups.name}")
            if grp.battery_health != ng.battery_health:
                grp.battery_health = ng.battery_health
                _add(applied, f"battery_health:{grp.ups.name}")
            if grp.self_test != ng.self_test:
                grp.self_test = ng.self_test
                _add(applied, f"self_test:{grp.ups.name}")
            # Anything else changed on the group (VMs, containers, remote
            # servers, ...) is captured by the shutdown path at run time and is
            # reported as restart-required.
            if replace(grp, triggers=ng.triggers, nut_control=ng.nut_control,
                       battery_health=ng.battery_health,
                       self_test=ng.self_test) != ng:
                _add(restart, f"ups_groups:{grp.ups.name}")

    return {"applied": applied, "restartRequired": restart,
            "subsystems": subsystems}


def perform_reload(primary: Config, monitor_configs: List[Config],
                   path: Optional[str]) -> Dict:
    """Load+validate the file and apply the safe subset. Never raises on a bad
    config — returns a report the caller can log or serialize.

    ISS-027: takes ``_RELOAD_LOCK`` (an RLock) so a direct call is serialized;
    the reload_config callers also hold the same lock across their full body
    (including the post-reload worker bounce), so the whole reload path is
    mutually exclusive. The signal handler may briefly block on an in-flight API
    reload (bounded by the reload duration)."""
    with _RELOAD_LOCK:
        new, errors = load_and_validate(path, primary)
        if new is None:
            return {"reloaded": False, "applied": [], "restartRequired": [],
                    "subsystems": [], "errors": errors}
        report = apply_reload(primary, monitor_configs, new)
        report["reloaded"] = True
        report["errors"] = []
        return report


def format_report(report: Dict) -> List[str]:
    """Render a reload report into log lines (shared by monitor + coordinator)."""
    if not report.get("reloaded"):
        lines = ["⚠️  Config reload failed; keeping running config:"]
        lines += [f"   {e}" for e in report.get("errors", [])]
        return lines
    applied = report.get("applied") or []
    restart = report.get("restartRequired") or []
    lines = []
    if applied:
        lines.append(f"✅  Config reloaded; applied live: {', '.join(applied)}")
    if restart:
        lines.append(f"ℹ️  Config changes that need a restart: {', '.join(restart)}")
    if not applied and not restart:
        lines.append("ℹ️  Config reload: no changes detected")
    return lines


def _add(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)
