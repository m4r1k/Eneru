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
  ``pending``. The deliver helper claims it as ``delivering``, opens the
  per-UPS Apprise instance, sends the row, and marks it ``sent``. The user gets a single
  ``🛑 Service Stopped`` notification.

Fallback path: when ``systemd-run`` isn't available in a foreground
non-container run, the helper falls back to shipping the stop
synchronously via the worker's Apprise instance. Containers are the
exception: Docker/Podman/Kubernetes send the same SIGTERM for a plain
stop and for an upgrade/restart, and there is no service-manager timer
outside the container to arbitrate intent. In that environment Eneru
leaves the stop row pending so the replacement container can supersede
it with one Restarted/Upgraded lifecycle notification.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional


logger = logging.getLogger(__name__)


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


# ============================================================================
# systemd intent detection — keep the defer-then-ship complexity OPT-IN.
#
# ROADMAP NOTE (K8s / Docker support):
# The whole defer+timer dance only makes sense when there's a service
# manager that will or won't restart us — i.e., systemd. In container
# contexts (Docker, K8s pods) the container runtime decides the
# replacement at a different layer (RestartPolicy / ReplicaSet), there
# is no "Job=restart" concept the daemon can introspect, and there's no
# `systemd-run` to schedule a deferred timer against. So the right
# behavior in container contexts is plain **eager send** — ship the
# stop notification synchronously and exit, same as v5.2.0 did
# unconditionally. When K8s / Docker support lands, this module's two
# entry points (`schedule_deferred_stop_or_eager_send` and
# `deliver_pending_stop`) can stay as-is — the systemd-specific paths
# are gated by `_running_under_systemd()` below, so non-systemd
# environments need their own branch. A detached in-container helper
# cannot reliably survive PID 1 exit / container removal, so Docker
# restart coalescing is handled by leaving the stop row pending for the
# next container's startup sweep.
# ============================================================================

def _running_under_systemd() -> bool:
    """True iff this process is being managed by systemd as a unit.

    Checked via the ``INVOCATION_ID`` env var that systemd sets for
    every service it launches. False in containers/K8s pods (no
    systemd), under foreground manual invocation (``eneru run``
    from a shell), and under most CI test environments.
    """
    return bool(os.environ.get("INVOCATION_ID"))


def _running_in_container() -> bool:
    """Best-effort container runtime check used before eager stop sends.

    Kept local to this module instead of importing ``eneru.cli`` because
    ``monitor.py`` imports this helper during module import, and ``cli.py``
    imports ``monitor.py``. A shared runtime-detection module would be
    cleaner eventually; this small detector covers the stop-notification
    decision without introducing that cycle.
    """
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    if os.environ.get("container", "").strip():
        return True

    try:
        cgroup_text = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup_text = ""
    if any(marker in cgroup_text for marker in (
        "docker", "kubepods", "containerd", "lxc",
    )):
        return True

    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        mountinfo = ""
    return (
        "/docker/containers/" in mountinfo
        or "/containers/storage/overlay-containers/" in mountinfo
    )


def _detect_systemd_stop_intent() -> bool:
    """True iff systemd is in the middle of a ``stop`` job for
    ``eneru.service`` (user ran ``systemctl stop eneru``, NOT a
    restart). When True, the OLD daemon ships the lifecycle stop
    notification eagerly because no replacement is coming.

    Implementation: queries ``systemctl list-jobs --no-legend``,
    NOT ``systemctl show -p Job``. The latter returns only the job
    DBus path / numeric id (``Job=12345``) — it does NOT carry the
    job's TYPE, so an earlier rc parsed ``Job=N:type`` and always
    saw False (which silently regressed every `systemctl stop` to
    the 15-second defensive timer; caught in PR #35 review).

    ``list-jobs`` output (with ``--no-legend`` suppressing header
    and footer) is one line per active job:

        12345 eneru.service stop running

    Columns: JobID, Unit, Type, State. We match by exact unit name
    and treat the third field as the job type. ``LANG=C`` ensures
    the column header isn't translated on non-English locales.

    Returns False on any uncertainty (query failed, systemd not
    available, racing the queue, unexpected output) — the caller
    falls through to the defensive systemd-run timer in that case.
    """
    try:
        result = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend"],
            capture_output=True, timeout=2, check=False,
            env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout.decode("utf-8", errors="replace")
    for line in output.splitlines():
        parts = line.split()
        # Expect at least: JobID Unit Type State
        if len(parts) < 4:
            continue
        if parts[1] == "eneru.service":
            return parts[2].lower() == "stop"
    return False


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
    # v5.2.1: short-circuit to eager send in two cases — both produce
    # an INSTANT 🛑 Stopped notification with no 15-second wait.
    #
    # 1. Not running under systemd. Foreground non-container runs get an
    #    eager stop because no replacement manager exists. Containers
    #    keep the row pending because Docker/Podman/K8s use the same
    #    SIGTERM shape for stop and upgrade/restart; the next daemon's
    #    lifecycle sweep cancels it before delivery.
    if not _running_under_systemd():
        if _running_in_container():
            log_fn(
                "📤 Container runtime without systemd — leaving stop "
                "notification pending so the next daemon can supersede it"
            )
            return
        log_fn(
            "📤 Not running under systemd — shipping stop notification "
            "eagerly via Apprise"
        )
        _eager_send(
            notification_id=notification_id, db_path=db_path,
            body=body, notify_type=notify_type,
            worker=worker, log_fn=log_fn,
        )
        return

    # 2. systemd has queued a `stop` job for eneru.service — i.e. user
    #    ran `systemctl stop eneru` (NOT a restart). No replacement is
    #    coming, so the deferred-then-ship dance is pure latency: ship
    #    eagerly. For Job=restart / Job=start (and unknown / no job),
    #    fall through to the timer path so the cancel-on-startup
    #    mechanism gets a chance to win.
    if _detect_systemd_stop_intent():
        log_fn(
            "📤 systemctl stop detected — shipping stop notification "
            "eagerly (no replacement daemon coming)"
        )
        _eager_send(
            notification_id=notification_id, db_path=db_path,
            body=body, notify_type=notify_type,
            worker=worker, log_fn=log_fn,
        )
        return

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
        # Bounded (M6): the eager send can run from the signal handler; a hung
        # endpoint must not block daemon exit. Times out -> row stays pending.
        success = worker._send_via_apprise_bounded(body, notify_type)
    except Exception as e:  # pragma: no cover -- defensive
        log_fn(f"⚠️ Eager-send via Apprise raised: {e}")
        return
    if not success:
        # At-least-once by design (cubic): a bounded send that TIMED OUT may
        # still complete on its abandoned background thread, yet we return here
        # and leave the row pending so the next start retries -- which can yield
        # ONE duplicate "Service Stopped" notification. That is deliberate: for a
        # stop notification a possible duplicate is strictly preferable to silent
        # loss, and we must not block daemon exit to learn the orphan thread's
        # real outcome (the whole point of the bounded send, M6).
        log_fn(
            "⚠️ Eager-send via Apprise returned False/timed out (network down?) "
            "— stop notification stays pending for the next start "
            "(a duplicate is possible if a timed-out send later completes)"
        )
        return
    try:
        from eneru.stats import StatsStore
        store = StatsStore(db_path)
        store.open(recover_delivering=False)
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
        # Read the body so we know what to ship if we win the claim.
        row = store._conn.execute(
            "SELECT body, notify_type, status FROM notifications "
            "WHERE id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            return 0  # row purged
        body, notify_type, status = row
        if status != "pending":
            return 0  # already cancelled (superseded) or already sent

        # ATOMIC CLAIM (CodeRabbit P1): the previous version did a
        # SELECT pending → send → mark_sent sequence with no claim
        # step. If the next daemon's classifier cancelled the row
        # between the SELECT and the send, we'd ship the stale stop
        # AND mark_notification_sent would overwrite the row's
        # `cancelled` status back to `sent` — reopening the
        # duplicate-lifecycle race the v5.2.1 fix exists to close.
        # Claim by transitioning pending→delivering in a single statement;
        # only proceed with delivery if we actually won the row. Do not mark
        # sent until Apprise returns success — a crash between claim and send is
        # recovered by the next daemon open moving stale delivering rows to
        # pending.
        now = int(time.time())
        with store._db_lock:
            cur = store._conn.execute(
                "UPDATE notifications SET status='delivering', sent_at=NULL, "
                "delivering_at=?, attempts=attempts+1 "
                "WHERE id=? AND status='pending'",
                (now, notification_id),
            )
            store._conn.commit()
            won = cur.rowcount > 0
        if not won:
            return 0  # raced with the classifier; let it win

        worker = NotificationWorker(config)
        # start() initializes the Apprise instance from config.urls
        # and validates each URL. It also spins up the worker thread,
        # which we don't strictly need here, but the cost is
        # negligible for a one-shot delivery.
        if not worker.start():
            # apprise unavailable / no urls — revert the claim so a
            # future start can retry instead of leaving a row marked
            # `delivering` with no actual delivery.
            try:
                with store._db_lock:
                    store._conn.execute(
                        "UPDATE notifications SET status='pending', "
                        "sent_at=NULL, delivering_at=NULL "
                        "WHERE id=? AND status='delivering'",
                        (notification_id,),
                    )
                    store._conn.commit()
            except sqlite3.Error:
                pass
            return 0
        try:
            try:
                delivered = worker._send_via_apprise(body, notify_type)
            except Exception:
                logger.exception(
                    "Deferred stop notification delivery failed; "
                    "notification_id=%s notify_type=%s",
                    notification_id,
                    notify_type,
                )
                delivered = False
            if delivered:
                try:
                    with store._db_lock:
                        store._conn.execute(
                            "UPDATE notifications SET status='sent', sent_at=?, "
                            "delivering_at=NULL "
                            "WHERE id=? AND status='delivering'",
                            (int(time.time()), notification_id),
                        )
                        store._conn.commit()
                except sqlite3.Error:
                    logger.exception(
                        "Deferred stop notification sent but mark-sent failed; "
                        "notification_id=%s",
                        notification_id,
                    )
            else:
                # Apprise rejected — revert claim to give the next
                # daemon a chance to retry.
                try:
                    with store._db_lock:
                        store._conn.execute(
                            "UPDATE notifications SET status='pending', "
                            "sent_at=NULL, delivering_at=NULL "
                            "WHERE id=? AND status='delivering'",
                            (notification_id,),
                        )
                        store._conn.commit()
                except sqlite3.Error:
                    pass
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
