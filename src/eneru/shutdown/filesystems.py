"""Filesystem sync + unmount phase of the shutdown sequence."""

import shlex
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
        """Run ``sync`` with a hard timeout so a hung mount can't stall poweroff.

        Runs the coreutils ``sync`` binary as a subprocess (run_command bounds
        it with a timeout) instead of the unbounded, uninterruptible
        ``os.sync()``. On timeout/error we log and proceed -- the kernel flushes
        again during the halt, so the poweroff must never be held hostage to a
        dead network filesystem.
        """
        exit_code, _, _ = run_command(["sync"], timeout=_SYNC_TIMEOUT_SECONDS)
        if exit_code == 124:
            self._log_message(
                f"  ⚠️ {label} timed out after {_SYNC_TIMEOUT_SECONDS}s "
                "(a hung mount?); proceeding -- the kernel re-syncs during halt."
            )
        elif exit_code != 0:
            self._log_message(
                f"  ⚠️ {label} reported an error (exit {exit_code}); proceeding."
            )
        else:
            self._log_message(f"  ✅ {label} complete")

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
