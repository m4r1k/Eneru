"""Filesystem sync + unmount phase of the shutdown sequence."""

import shlex
import subprocess
import time
from typing import List

from eneru.utils import run_command

# Wall-clock bound for the filesystem sync. A bare os.sync() blocks until EVERY
# mounted filesystem flushes; a hung/unreachable network mount (NFS/CIFS with a
# dead server, on the same failing power circuit) would leave it forever in
# uninterruptible D-state and the host would never reach poweroff. The kernel
# re-syncs during halt anyway, so abandoning a stuck sync after this many
# seconds is safe and strictly better than never powering off.
_SYNC_TIMEOUT_SECONDS = 30


class FilesystemShutdownMixin:
    """Mixin: filesystem sync and unmount during shutdown."""

    def _bounded_sync(self, label: str) -> None:
        """Run ``sync`` with a TRUE wall-clock bound so a hung mount can't stall
        the poweroff.

        Uses ``Popen`` + a polling deadline rather than ``subprocess.run(timeout=)``
        or the unbounded, uninterruptible ``os.sync()``. The reason
        (CodeRabbit): ``subprocess.run``'s timeout sends SIGKILL and then BLOCKS
        in wait() to reap -- and a ``sync`` stuck on a dead NFS/CIFS mount sits
        in uninterruptible D-state where even SIGKILL can't land, so that wait
        hangs and the "timeout" never returns. Here we poll ``proc.poll()`` and,
        if the deadline passes, we best-effort kill and ABANDON the process
        WITHOUT waiting -- the daemon thread is never blocked, the orphan dies at
        halt, and the kernel re-syncs during the halt regardless.
        """
        try:
            proc = subprocess.Popen(
                ["sync"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            self._log_message(
                f"  ⚠️ {label}: could not start `sync` ({exc}); proceeding.")
            return
        deadline = time.monotonic() + _SYNC_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        rc = proc.poll()
        if rc is None:
            # Still running at the deadline -- almost certainly a mount in
            # uninterruptible D-state. Signal best-effort and abandon it; do NOT
            # wait (that's exactly what would hang). Proceed to poweroff.
            try:
                proc.kill()
            except Exception:
                pass
            self._log_message(
                f"  ⚠️ {label} did not finish within {_SYNC_TIMEOUT_SECONDS}s "
                "(a hung mount?); proceeding without waiting -- the kernel "
                "re-syncs during halt."
            )
        elif rc == 0:
            self._log_message(f"  ✅ {label} complete")
        else:
            self._log_message(
                f"  ⚠️ {label} reported an error (exit {rc}); proceeding."
            )

    def _sync_filesystems(self):
        """Sync all filesystems.

        Note: os.sync() schedules buffers to be flushed but may return before
        physical write completion on some systems. The 2-second sleep allows
        storage controllers (especially battery-backed RAID) to flush their
        write-back caches before power is cut.
        """
        if not self.config.filesystems.sync_enabled:
            return

        self._log_message("💾 Syncing all filesystems...")
        if self.config.behavior.dry_run:
            self._log_message("  🧪 [DRY-RUN] Would sync filesystems")
        else:
            self._bounded_sync("Filesystem sync")
            time.sleep(2)  # Allow storage controller caches to flush

    def _unmount_filesystems(self):
        """Unmount configured filesystems."""
        if not self.config.filesystems.unmount.enabled:
            return

        if not self.config.filesystems.unmount.mounts:
            return

        timeout = self.config.filesystems.unmount.timeout
        self._log_message(f"📤 Unmounting filesystems (Max wait: {timeout}s)...")

        for mount in self.config.filesystems.unmount.mounts:
            mount_point = mount.get('path', '')
            options = mount.get('options', '')

            if not mount_point:
                continue

            options_display = f" {options}" if options else ""
            self._log_message(f"  ➡️ Unmounting {mount_point}{options_display}")

            # Build the argv up-front so the dry-run log can render the
            # exact tokens that would be exec'd (was: the dry-run line
            # printed the raw `options` string, which doesn't match what
            # actually runs after shlex.split). Malformed-options
            # detection now also happens in dry-run mode, surfacing
            # config errors before a real shutdown.
            opt_args: List[str] = []
            if options:
                try:
                    opt_args = shlex.split(options)
                except ValueError as exc:
                    self._log_message(
                        f"  ❌ Invalid umount options for {mount_point}: "
                        f"{exc}. Skipping this mount."
                    )
                    continue
            cmd = ["umount", *opt_args, mount_point]

            if self.config.behavior.dry_run:
                rendered = " ".join(shlex.quote(a) for a in cmd)
                self._log_message(
                    f"  🧪 [DRY-RUN] Would execute: timeout {timeout}s {rendered}"
                )
                continue

            exit_code, _, stderr = run_command(cmd, timeout=timeout)

            if exit_code == 0:
                self._log_message(f"  ✅ {mount_point} unmounted successfully")
            elif exit_code == 124:
                self._log_message(
                    f"  ⚠️ {mount_point} unmount timed out "
                    "(device may be busy/unreachable). Proceeding anyway."
                )
            else:
                check_code, _, _ = run_command(["mountpoint", "-q", mount_point])
                if check_code == 0:
                    self._log_message(
                        f"  ❌ Failed to unmount {mount_point} "
                        f"(Error code {exit_code}). Proceeding anyway."
                    )
                else:
                    self._log_message(f"  ℹ️ {mount_point} was likely not mounted.")
