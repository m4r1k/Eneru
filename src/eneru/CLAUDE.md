# `src/eneru/` — package layout for Claude Code

This file gives session-local context when working inside the Eneru package.
The root `/root/Workspace/Eneru/CLAUDE.md` still applies — this one is **just
the module map and the mixin pattern**, so a session can navigate without
loading whole files first.

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
   convention (root `CLAUDE.md`): every feature ships with both synthetic
   tests in `tests/` AND an E2E step.

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

- Emoji semantics in log messages are documented in the root `CLAUDE.md`
  ("Code Style" section). Use them — they're not decoration; they're scanner
  hints during incident review.
- The single-file `monitor.py` god-object was decomposed in v5.1; if you find
  yourself wanting to add a 200-line method here, it almost certainly belongs
  in (or as) a mixin under `shutdown/` or `health/`.
- Stateful attributes that a mixin depends on are set in
  `UPSGroupMonitor.__init__` or in `_check_dependencies`. Don't init mixin
  state inside the mixin's own methods — keep init centralized.
