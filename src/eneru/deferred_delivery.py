"""Deferred delivery of the lifecycle 'Service Stopped' notification.

v5.2 wanted ONE notification per lifecycle transition. v5.2.0 shipped
the stop synchronously via ``flush(timeout=5)``, which beat the next
daemon's classifier to the punch and produced two messages on every
``systemctl restart``.

v5.2.1's first attempt (defer the stop, let the next daemon's classifier
cancel it) fixed restart but broke ``systemctl stop`` — without a next
daemon, the row sat ``pending`` forever and the user never saw a
notification.

The fix here is option D from the v5.2.1 design discussion: at SIGTERM
time the OLD daemon enqueues the stop row AND schedules a transient
``systemd-run`` timer that re-invokes ``eneru _deliver-stop`` ~15 s
later. The transient timer lives **outside** ``eneru.service``'s cgroup,
so it survives our exit and doesn't gate ``systemctl restart`` on its
own delivery (systemd starts the new daemon as soon as our cgroup
empties — which happens immediately on our exit).

When the timer fires:

- If the new daemon already came up and its classifier cancelled the
  pending row (via ``_emit_lifecycle_startup_notification``'s supersede
  block, fired BEFORE the worker can deliver), the deliver helper sees
  ``status != 'pending'`` and exits silently. The user gets a single
  ``🔄 Restarted`` notification.
- If no replacement came up (``systemctl stop``), the row is still
  ``pending``. The deliver helper opens the per-UPS Apprise instance,
  sends the row, and marks it ``sent``. The user gets a single
  ``🛑 Service Stopped`` notification.

Fallback path: when ``systemd-run`` isn't available (non-systemd
containers, frozen-pristine sandboxes), the helper falls back to
shipping the stop synchronously via the worker's Apprise instance.
This loses the restart-coalescing benefit on those hosts but at least
the user always sees a notification.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
from pathlib import Path
from typing import Callable, Optional


# Window between OUR exit and the deferred-delivery timer firing.
# Chosen to comfortably exceed systemd's `RestartSec=5` (eneru.service)
# plus the new daemon's startup time (~2-5 s through `_initialize`)
# plus a small margin. The new daemon's classifier-supersede pass
# (monitor.py:_emit_lifecycle_startup_notification) is the cancel
# mechanism that fires BEFORE the worker can deliver — as long as the
# daemon comes up inside this window, the cancel beats us to the row
# and the timer fire is a no-op.
DEFAULT_DEFER_SECS = 15


def _eneru_invocation_args() -> Optional[list]:
    """Return ``[interpreter, script]`` args that re-invoke this eneru
    installation in a fresh subprocess. The deb/rpm wrapper at
    ``/opt/ups-monitor/eneru.py`` is preferred when present because it
    sets ``sys.path`` explicitly (no PYTHONPATH dependency); otherwise
    we use ``python -m eneru`` which works for pip / uv-venv installs.
    """
    deb_rpm_wrapper = "/opt/ups-monitor/eneru.py"
    if os.path.exists(deb_rpm_wrapper):
        return [sys.executable, deb_rpm_wrapper]
    return [sys.executable, "-m", "eneru"]


def schedule_deferred_stop_or_eager_send(
    *,
    notification_id: int,
    db_path: Path,
    config_path: Optional[str],
    body: str,
    notify_type: str,
    worker,
    log_fn: Callable[[str], None],
    delay_secs: int = DEFAULT_DEFER_SECS,
) -> None:
    """Schedule a transient systemd-run timer to deliver the pending
    stop notification ``delay_secs`` later, OR fall back to eager
    Apprise delivery if systemd-run can't be used.

    Caller is expected to have already enqueued the row via
    ``_send_notification(..., category='lifecycle')`` and to pass that
    row's id here.

    ``log_fn`` is called with one short status line so the operator
    can see in the log which path was taken.
    """
    if config_path:
        invocation = _eneru_invocation_args()
        cmd = [
            "systemd-run",
            f"--on-active={int(delay_secs)}s",
            "--description=Eneru deferred stop-notification delivery (v5.2.1)",
            f"--unit=eneru-deliver-stop-{notification_id}",
            "--quiet",
            "--collect",  # auto-cleanup the transient unit after exit
            *invocation,
            "_deliver-stop",
            "--notification-id", str(notification_id),
            "--db-path", str(db_path),
            "--config", str(config_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                log_fn(
                    f"📅 Stop notification scheduled for delivery in "
                    f"{delay_secs}s (will be superseded if a new "
                    "daemon's classifier cancels the row first)"
                )
                return
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            log_fn(
                f"⚠️ systemd-run rc={result.returncode}: "
                f"{stderr[:200]} — falling back to eager delivery"
            )
        except FileNotFoundError:
            log_fn(
                "⚠️ systemd-run not on PATH — falling back to eager "
                "delivery (restart-coalescing won't apply on this host)"
            )
        except subprocess.TimeoutExpired:
            log_fn(
                "⚠️ systemd-run timed out — falling back to eager delivery"
            )
    else:
        log_fn(
            "⚠️ No config_path on Config — falling back to eager "
            "delivery (deferred path needs to re-load config out-of-process)"
        )

    _eager_send(
        notification_id=notification_id,
        db_path=db_path,
        body=body,
        notify_type=notify_type,
        worker=worker,
        log_fn=log_fn,
    )


def _eager_send(
    *,
    notification_id: int,
    db_path: Path,
    body: str,
    notify_type: str,
    worker,
    log_fn: Callable[[str], None],
) -> None:
    """Fallback: ship the stop notification synchronously via the
    worker's Apprise instance, then mark the row sent."""
    if worker is None:
        log_fn("⚠️ No worker — stop notification will stay pending")
        return
    try:
        # _send_via_apprise is the worker's private synchronous-send
        # method (see notifications.py); we reach in deliberately
        # rather than re-instantiating an Apprise object because the
        # worker has already validated the URLs at startup.
        success = worker._send_via_apprise(body, notify_type)
    except Exception as e:  # pragma: no cover -- defensive
        log_fn(f"⚠️ Eager-send via Apprise raised: {e}")
        return
    if not success:
        log_fn(
            "⚠️ Eager-send via Apprise returned False (network down?) "
            "— stop notification stays pending for the next start"
        )
        return
    try:
        from eneru.stats import StatsStore
        store = StatsStore(db_path)
        store.open()
        try:
            store.mark_notification_sent(notification_id)
        finally:
            store.close()
    except (sqlite3.Error, OSError, TypeError, ValueError) as e:
        # TypeError / ValueError catch the case where db_path isn't a
        # real path (mock objects in tests, malformed config). The
        # caller still got their Apprise delivery; only the row's
        # status didn't update. Worst case: the same row gets re-tried
        # by the next start, harmless duplicate.
        log_fn(
            f"⚠️ Eager-send shipped via Apprise but mark_sent failed: "
            f"{e} (row stays pending; harmless duplicate possible)"
        )


def deliver_pending_stop(
    *,
    notification_id: int,
    db_path: Path,
    config,
) -> int:
    """Invoked by the ``eneru _deliver-stop`` CLI subcommand from
    inside the systemd-run transient unit. Idempotent: if the row was
    already cancelled (next daemon's classifier superseded it) or
    already sent (somehow), this returns 0 without delivering.

    Returns a process exit code: 0 always (success or skipped); the
    transient unit doesn't have anyone to report failures to anyway.
    """
    from eneru.notifications import NotificationWorker
    from eneru.stats import StatsStore

    if not db_path.exists():
        return 0  # DB gone (uninstall? cleanup?). Nothing to do.

    store = StatsStore(db_path)
    try:
        store.open()
        row = store._conn.execute(
            "SELECT body, notify_type, status FROM notifications "
            "WHERE id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            return 0  # row purged
        body, notify_type, status = row
        if status != "pending":
            return 0  # superseded or already sent — skip

        worker = NotificationWorker(config)
        # start() initializes the Apprise instance from config.urls
        # and validates each URL. It also spins up the worker thread,
        # which we don't strictly need here, but the cost is
        # negligible for a one-shot delivery.
        if not worker.start():
            return 0  # apprise unavailable / no urls / etc.
        try:
            success = worker._send_via_apprise(body, notify_type)
            if success:
                store.mark_notification_sent(notification_id)
        finally:
            worker.stop()
    except (sqlite3.Error, OSError):
        return 0  # best-effort
    finally:
        try:
            store.close()
        except (sqlite3.Error, OSError):
            pass
    return 0
