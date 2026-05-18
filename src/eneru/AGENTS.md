# `src/eneru/` — package layout for agents

This file gives session-local context when working inside the Eneru package.
The root `/root/Workspace/Eneru/AGENTS.md` still applies — including git,
test, and AI-review workflow rules. This file is **just the module map and the
mixin pattern**, so a session can navigate without loading whole files first.

## Module map

| File / package | Owns |
|---|---|
| `monitor.py` | `UPSGroupMonitor` core: init, dependency checks, UPS polling (`_get_ups_var`, `_get_all_ups_data`), state save, log/notify primitives (`_log_message`, `_send_notification`, `_log_power_event`), shutdown orchestration (`_execute_shutdown_sequence`, `_trigger_immediate_shutdown`, `_cleanup_and_exit`), `_main_loop`, `_handle_on_battery` / `_handle_on_line` / `_handle_connection_failure`, plus the module-level `compute_effective_order`. |
| `multi_ups.py` | `MultiUPSCoordinator` — thread-per-group orchestration, shared logger / notification worker, defense-in-depth local-shutdown lock + filesystem flag. |
| `shutdown/vms.py` | `VMShutdownMixin` — libvirt VMs: graceful `virsh shutdown` with force-destroy fallback after `max_wait`. |
| `shutdown/containers.py` | `ContainerShutdownMixin` — Docker/Podman runtime detection (`_detect_container_runtime`, `_check_compose_available`), compose stack shutdown, remaining-container shutdown, optional rootless Podman per non-system user. Owns `self._container_runtime` and `self._compose_available` (set in `_check_dependencies`). |
| `shutdown/filesystems.py` | `FilesystemShutdownMixin` — `os.sync()` with controller-cache flush sleep, then per-mount `umount` with timeout / busy-mount handling. |
| `shutdown/remote.py` | `RemoteShutdownMixin` — SSH-based remote-server orchestration: phase batching by `shutdown_order`, parallel-thread phase, deadline-based join, per-server pre-shutdown commands (`REMOTE_ACTIONS` templates + custom commands), final shutdown command. |
| `health/voltage.py` | `VoltageMonitorMixin` — dynamic threshold init from `input.voltage.nominal` / `input.transfer.*`, per-state transitions (`_check_voltage_issues`, `_check_avr_status`, `_check_bypass_status`, `_check_overload_status`). |
| `health/battery.py` | `BatteryMonitorMixin` — rolling battery-history file (`self._battery_history_path`), depletion-rate calculation, sustained-reading anomaly confirmation across 3 polls (filters APC/CyberPower/UniFi firmware jitter). |
| `config.py` | All 16 config dataclasses + `ConfigLoader` (YAML parse, env override, validation). |
| `state.py` | `MonitorState` dataclass — runtime state attached to a `UPSGroupMonitor` instance. |
| `logger.py` | `UPSLogger` + `TimezoneFormatter`. |
| `notifications.py` | `NotificationWorker` (Apprise-backed, queued, retry-aware). |
| `utils.py` | `run_command`, `command_exists`, `is_numeric`, `format_seconds`. |
| `actions.py` | `REMOTE_ACTIONS` — predefined SSH command templates (`shutdown`, `stop_compose`, etc.). |
| `cli.py` | argparse + subcommand dispatch (`run`, `validate`, `version`, `test-notifications`, `monitor`). |
| `tui.py` | curses-based dashboard, `--once` plain-text variant. |

## The mixin pattern (where to add a new shutdown phase)

Every shutdown / health phase is a **mixin class**, not a free function.
Method bodies live in the mixin file; `UPSGroupMonitor` inherits them.

```python
# src/eneru/shutdown/<new_phase>.py

class NewPhaseMixin:
    """Mixin: <one-line description>."""

    def _shutdown_<thing>(self):
        if not self.config.<feature>.enabled:
            return
        self._log_message("...")
        ...
```

Then in `monitor.py`:

```python
from eneru.shutdown.new_phase import NewPhaseMixin

class UPSGroupMonitor(
    VMShutdownMixin,
    ContainerShutdownMixin,
    FilesystemShutdownMixin,
    RemoteShutdownMixin,
    NewPhaseMixin,        # add here
    VoltageMonitorMixin,
    BatteryMonitorMixin,
):
    ...
    def _execute_shutdown_sequence(self):
        ...
        self._shutdown_<thing>()        # call where it belongs in the order
        ...
```

What mixins can rely on (all set up by `monitor.py`):

- `self.config`, `self.state`, `self.logger`
- `self._log_message`, `self._send_notification`, `self._log_power_event`
- `self._shutdown_flag_path`, `self._battery_history_path`, `self._state_file_path`
- `self._notification_worker`, `self._stop_event`, `self._coordinator_mode`

What mixins MUST do when adding a new module file:

1. **Add a `contents:` entry in `nfpm.yaml`.** The deb/rpm builds enumerate
   each `.py` explicitly — they do NOT glob. Pip CI passes silently when a
   module is missing because `pyproject.toml` autodiscovers, so the gap only
   surfaces at install time on Debian/Ubuntu/RHEL. PR #23 already burned us
   here once; don't repeat.
2. **Add a step to `.github/workflows/e2e.yml`** that exercises the feature
   end-to-end against the docker-compose environment in `tests/e2e/`. Project
   convention (root `AGENTS.md`): every feature ships with both synthetic
   tests in `tests/` AND an E2E step.

## Loopback ordering (v5.5+)

Eneru's pre-v5.5 shutdown sequence had a fixed three-act order: **(1)
drain local state** (stop VMs/containers, sync, unmount filesystems),
**(2) shut down peer remotes** (NAS, secondary hosts), **(3) poweroff
the eneru host**. Local drain before peers because a local app or NFS
mount might depend on a peer being alive. Host poweroff last because
eneru itself is running on it.

v5.5 added container-native local-host ownership through a
`is_host_loopback: true` SSH delegate. The local drain (act 1) and the
host poweroff (act 3) both happen via SSH to `127.0.0.1` because
nothing privileged can run inside the non-root container. Mechanically,
both got bundled into a single `remote_servers` entry — and that broke
the ordering invariant, because the remote loop sorts by one
`shutdown_order` integer per entry and "first AND last" can't be one
integer. v5.5.0-rc7 shipped with the loopback at `shutdown_order=999`,
which put the whole bundle AFTER configured peer remotes. Real test
2026-05-18: NAS shutdown sent first, NFS unmount of NAS mounts hung
32s after the NAS was already powering off. Held back 5.5.0 stable.

**The fix lives in `src/eneru/shutdown/remote.py`,
`RemoteShutdownMixin._shutdown_remote_servers`.** The runtime now
partitions enabled remotes into `loopbacks` and `regulars`, then runs:

1. **Phase A** — every loopback's `pre_shutdown_commands`. Synchronous
   (loopbacks are usually one, sometimes a few in K8s; parallelism
   doesn't pay).
2. **Phase B** — regulars, grouped by `shutdown_order` and run in
   parallel within a phase. **This is the v5.4 code path, unchanged.**
3. **Phase C** — every loopback's `shutdown_command`.

Each loopback's `RemoteShutdownResult` is pre-allocated before Phase A
and updated in both A and C, so the summary log still reports one
success/fail row per server.

**Invariants the runtime guarantees:**

- A loopback's pre-actions always run **before** any non-loopback remote.
- A loopback's shutdown command always runs **after** every non-loopback
  remote.
- `shutdown_order` on a loopback entry is **ignored** at execution time.
  We keep the field schema-valid for backward compatibility with explicit
  YAML, but the auto-synthesizer no longer sets it (see
  `_synthesize_loopback_if_needed` in `cli.py`).
- Remote-only configs (no `is_host_loopback: true` anywhere) take the
  Phase B path only and are bit-for-bit equivalent to v5.4 behavior.

**When you touch this code:**

- Keep Phase B independent of loopback state — if you find yourself
  threading `is_host_loopback` checks through `compute_effective_order`
  or `_shutdown_servers_parallel`, you're undoing the partition.
- If you add a new field that mutates the loopback's execution
  semantics (custom `shutdown_command_per_phase`, deadlines, etc.),
  update this section and add a regression test covering the
  `[loopback, regular@-1]` shape — that's the exact configuration the
  rc7 bug hit.
- The `print_shutdown_sequence` tree in `cli.py` shows local-delegated
  step 1, remotes step 2, host poweroff step 3. After this fix the
  print output and the runtime execution finally agree; if you change
  one, change the other.

## Stats schema evolution (when to add a column)

The SQLite stats DB (`src/eneru/stats.py`) ships a `SCHEMA_VERSION`
integer + a migration block in `_init_schema`. Bump and migrate as the
daemon grows new persistent state.

**When to add a DB column** (vs. keeping state in-memory only):

- The data has long-term analytical value (capacity planning, debugging
  power events months later, cross-correlation with hardware behavior).
- It's queryable per row — a user with `sqlite3 /var/lib/eneru/*.db`
  should be able to ask a useful question with it.
- Its grain matches an existing table (per-poll → `samples`, per-event
  → `events`, per-aggregation-window → `agg_5min` / `agg_hourly`).

**When NOT to add a column:**

- Pure runtime state (locks, pending timers, in-flight buffers) — keep
  in `MonitorState` or local variables.
- Configuration that's already in `config.yaml` — don't denormalize.
- Anything written every poll that's a derivative of existing columns
  (compute it on read instead).

**Migration pattern** (real examples: v1→v2 added 4 raw NUT metrics +
`output_voltage_avg`; v2→v3 added `events.notification_sent`):

```python
SCHEMA_VERSION = 3   # bump

def _migrate_schema(self) -> None:
    cur = self._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if cur is None:
        return  # brand-new DB; CREATE TABLE already includes everything
    current = int(cur[0]) if cur[0] else 1

    if current < 2:
        # v1 -> v2 deltas (additive ALTERs only).
        for col in ("battery_voltage REAL", "ups_temperature REAL", ...):
            self._safe_alter("samples", col)
        for table in ("agg_5min", "agg_hourly"):
            for col in ("output_voltage_avg REAL", ...):
                self._safe_alter(table, col)

    if current < 3:
        # v2 -> v3 deltas. Append-only -- never edit the v1->v2 block.
        self._safe_alter("events",
                         "notification_sent INTEGER DEFAULT 1")

def _safe_alter(self, table: str, column_def: str) -> None:
    """Idempotent ALTER TABLE ... ADD COLUMN."""
    try:
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError:
        pass  # column already exists -- benign on retries
```

**Rules:**

1. **Migrations are append-only.** Never modify a previous version's
   block. If you got the v1→v2 migration wrong, fix it forward in v2→v3.
2. Every `ALTER TABLE` goes through `_safe_alter` — the daemon must
   tolerate running against a partially-migrated DB.
3. `meta.schema_version` is updated last, so a crash mid-migration is
   replayed safely on next start.
4. `_init_schema` is called inside `open()`. Failure raises and the
   `monitor.py` call site swallows + logs once + sets stats to no-op
   (per the failure-isolation contract). A migration must never take
   down the daemon's safety-critical path.
5. **New event types do NOT need a schema bump** — `events.event_type`
   is `TEXT`. Bump only when adding columns or tables.
6. Update `docs/statistics.md` when bumping: extend the schema block,
   bump the storage-volume number if material, add the new column to
   any sqlite3 query examples.
7. Add tests in `tests/test_stats.py`'s `TestSchemaMigration` class:
   one that proves the new column is added, one that proves it's
   idempotent on repeated open, one that proves existing rows are
   preserved.

## Conventions specific to this package

- Emoji semantics in log messages are documented in the root `AGENTS.md`
  ("Code Style" section). Use them — they're not decoration; they're scanner
  hints during incident review.
- The single-file `monitor.py` god-object was decomposed in v5.1; if you find
  yourself wanting to add a 200-line method here, it almost certainly belongs
  in (or as) a mixin under `shutdown/` or `health/`.
- Stateful attributes that a mixin depends on are set in
  `UPSGroupMonitor.__init__` or in `_check_dependencies`. Don't init mixin
  state inside the mixin's own methods — keep init centralized.
