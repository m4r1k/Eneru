"""Stateful lifecycle classifier for v5.2 startup notifications.

Today's startup is stateless: every ``_initialize()`` emits a generic
"🚀 Started" notification regardless of how the previous instance
exited. v5.2 distinguishes:

- **Started**: cold start (no marker, fresh install or first ever boot).
- **Restarted**: graceful exit + new start within ~30 s.
- **Recovered**: graceful exit triggered by a power-loss shutdown
  sequence; the user sees one message that retroactively explains why
  the system went down and confirms it's back.
- **Upgraded**: deb/rpm postinstall set the upgrade marker before
  ``systemctl restart``; the daemon reads it and emits a single
  "📦 Upgraded vX → vY" message instead of stop+start.
- **Started after crash**: marker absent but ``meta.last_seen_version``
  is set — the previous instance died without writing its marker.

Two on-disk markers (under the stats directory) drive the
classification:

- ``.shutdown_state.json`` — written on graceful exit, contains
  ``shutdown_at`` (unix), ``version``, and ``reason`` (one of
  ``signal`` / ``sequence_complete`` / ``fatal``).
- ``.upgrade_marker.json`` — written by ``packaging/scripts/postinstall.sh``
  before restarting the unit, contains ``old_version`` and ``new_version``.

The pip path has no postinstall hook, so for pip users the upgrade is
detected by comparing ``meta.last_seen_version`` (in the stats DB) to
the current ``__version__`` on startup.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Tuple

from eneru.utils import format_seconds


SHUTDOWN_MARKER_NAME = ".shutdown_state.json"
UPGRADE_MARKER_NAME = ".upgrade_marker.json"

# Below this many seconds, a shutdown+start cycle reads as a restart
# rather than a fresh start. Picked to match the typical
# `systemctl restart` window without being so wide that an unrelated
# manual stop+start gets misclassified.
RESTART_DOWNTIME_THRESHOLD_SECS = 30

# Valid `reason` values for the shutdown marker. Anything else gets
# treated as "signal" by the classifier.
REASON_SIGNAL = "signal"
REASON_SEQUENCE_COMPLETE = "sequence_complete"
REASON_FATAL = "fatal"


def _marker_path(directory: Path, name: str) -> Path:
    return Path(directory) / name


def write_shutdown_marker(directory: Path, *, version: str,
                          reason: str = REASON_SIGNAL,
                          shutdown_at: Optional[int] = None) -> None:
    """Persist the shutdown context that the next startup will read.

    Best-effort: a write failure logs nothing and silently degrades the
    next startup classification to "Started after crash" — no harm
    done, just a less informative notification.
    """
    path = _marker_path(directory, SHUTDOWN_MARKER_NAME)
    payload = {
        "shutdown_at": int(shutdown_at if shutdown_at is not None
                           else time.time()),
        "version": str(version),
        "reason": str(reason),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    except OSError:
        pass


def read_shutdown_marker(directory: Path) -> Optional[dict]:
    """Return the shutdown marker dict, or ``None`` if absent / unreadable."""
    path = _marker_path(directory, SHUTDOWN_MARKER_NAME)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def delete_shutdown_marker(directory: Path) -> None:
    """Remove the shutdown marker. Idempotent."""
    try:
        _marker_path(directory, SHUTDOWN_MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def read_upgrade_marker(directory: Path) -> Optional[dict]:
    """Return the upgrade marker dict, or ``None`` if absent / unreadable.

    The marker is dropped by ``packaging/scripts/postinstall.sh`` BEFORE
    ``systemctl restart``, so the daemon's startup classifier finds it
    and emits a single "Upgraded" notification instead of stop+start.
    """
    path = _marker_path(directory, UPGRADE_MARKER_NAME)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def delete_upgrade_marker(directory: Path) -> None:
    """Remove the upgrade marker. Idempotent."""
    try:
        _marker_path(directory, UPGRADE_MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def classify_startup(*, current_version: str,
                     shutdown_marker: Optional[dict],
                     upgrade_marker: Optional[dict],
                     last_seen_version: Optional[str],
                     now_ts: Optional[int] = None,
                     ) -> Tuple[str, str]:
    """Pick the lifecycle notification body + notify_type for this start.

    Args:
        current_version: ``__version__`` at the moment of this start.
        shutdown_marker: Result of :func:`read_shutdown_marker`.
        upgrade_marker: Result of :func:`read_upgrade_marker` (postinstall
            path) — falls back to a version-comparison check below.
        last_seen_version: Last value of ``meta.last_seen_version`` from
            the stats DB, used as the pip-path upgrade detector when no
            on-disk upgrade marker is present.
        now_ts: Override "now" for deterministic tests.

    Returns ``(body, notify_type)``. ``notify_type`` is one of
    ``info`` / ``success`` / ``warning`` / ``failure``.
    """
    now = int(now_ts if now_ts is not None else time.time())

    # 1) Postinstall-set marker: deb/rpm upgrade. Authoritative.
    if upgrade_marker:
        old = str(upgrade_marker.get("old_version", "?"))
        new = str(upgrade_marker.get("new_version", current_version))
        return (
            f"📦 **Eneru Upgraded** v{old} → v{new}\n"
            "Service is back online with the new version.",
            "success",
        )

    # 2) Pip-path upgrade detection: no postinstall, but the version
    #    string changed since the previous start.
    if (last_seen_version and last_seen_version != current_version
            and not shutdown_marker):
        # Treat as upgrade only when there's no recent shutdown marker —
        # otherwise the marker's classification (Restarted / Recovered)
        # is more informative.
        return (
            f"📦 **Eneru Upgraded** v{last_seen_version} → v{current_version}\n"
            "Service is back online with the new version.",
            "success",
        )

    if shutdown_marker:
        downtime = max(0, now - int(shutdown_marker.get("shutdown_at", now)))
        prev_version = str(shutdown_marker.get("version", current_version))
        reason = str(shutdown_marker.get("reason", REASON_SIGNAL))

        # Pip-path upgrade BUT we also have a shutdown marker:
        # explain both via the upgrade phrasing — the version change
        # is the bigger story, the shutdown reason is secondary.
        if (last_seen_version and last_seen_version != current_version):
            return (
                f"📦 **Eneru Upgraded** v{last_seen_version} → v{current_version}\n"
                f"Resumed after {format_seconds(downtime)} downtime.",
                "success",
            )

        if reason == REASON_SEQUENCE_COMPLETE:
            return (
                f"📊 **Eneru Recovered**\n"
                f"Resumed after {format_seconds(downtime)} downtime "
                f"following a power-loss-triggered shutdown "
                f"(was v{prev_version}).",
                "success",
            )
        if reason == REASON_FATAL:
            return (
                f"🚀 **Eneru Restarted**\n"
                f"Last instance exited fatally; back up after "
                f"{format_seconds(downtime)} (was v{prev_version}).",
                "warning",
            )
        # reason == "signal" or unknown: distinguish quick restart from
        # a true cold start (manual stop, then later start).
        if downtime < RESTART_DOWNTIME_THRESHOLD_SECS:
            return (
                f"🔄 **Eneru Restarted** (downtime: "
                f"{format_seconds(downtime)})\nService is back online.",
                "info",
            )
        return (
            f"🚀 **Eneru Started** (last seen "
            f"{format_seconds(downtime)} ago)\nResumed monitoring.",
            "info",
        )

    # 3) No marker at all. Either a fresh install (no last_seen_version
    #    either) or a hard crash that didn't get to write a marker.
    if last_seen_version:
        return (
            f"🚀 **Eneru v{current_version} Started** (after crash)\n"
            f"Last clean run was v{last_seen_version}.",
            "warning",
        )
    return (
        f"🚀 **Eneru v{current_version} Started**\n"
        "Monitoring active.",
        "info",
    )
