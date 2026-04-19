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
