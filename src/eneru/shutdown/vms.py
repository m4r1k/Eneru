"""Libvirt virtual-machine shutdown phase."""

import time
from typing import List

from eneru.utils import command_exists, run_command


class VMShutdownMixin:
    """Mixin: graceful libvirt VM shutdown with force-destroy fallback."""

    def _shutdown_vms(self):
        """Shutdown all libvirt virtual machines."""
        if not self.config.virtual_machines.enabled:
            return

        self._log_message("🖥️  Shutting down all libvirt virtual machines...")

        if not command_exists("virsh"):
            self._log_message("  ℹ️  virsh not available, skipping VM shutdown")
            return

        exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
        if exit_code != 0:
            self._log_message("  ⚠️  Failed to get VM list")
            return

        running_vms = [vm.strip() for vm in stdout.strip().split('\n') if vm.strip()]

        if not running_vms:
            self._log_message("  ℹ️  No running VMs found")
            return

        for vm in running_vms:
            self._log_message(f"  ⏹️  Shutting down VM: {vm}")
            if self.config.behavior.dry_run:
                self._log_message(f"  🧪  [DRY-RUN] Would shutdown VM: {vm}")
            else:
                exit_code, stdout, stderr = run_command(["virsh", "shutdown", vm])
                if stdout.strip():
                    self._log_message(f"    {stdout.strip()}")

        if self.config.behavior.dry_run:
            return

        max_wait = self.config.virtual_machines.max_wait
        self._log_message(f"  ⏳  Waiting up to {max_wait}s for VMs to shutdown gracefully...")
        wait_interval = 5
        # L7 / CodeRabbit: use a WALL-CLOCK deadline rather than counting only
        # the sleeps. The old loop advanced `time_waited` only after sleep(), so
        # each (bounded) `virsh list` poll plus a wedged libvirtd could stretch
        # the phase well past max_wait. A monotonic deadline charges poll time
        # too, and each poll/sleep is capped to the remaining budget so the whole
        # graceful wait is bounded by ~max_wait before force-destroy/poweroff.
        deadline = time.monotonic() + max_wait
        # Seed with the originally-running list so a transient virsh
        # failure on the first poll doesn't make the loop think the VMs
        # are gone (empty stdout would otherwise yield remaining_vms=[]
        # and skip force-destroy).
        remaining_vms: List[str] = list(running_vms)

        while time.monotonic() < deadline:
            remaining_budget = max(1, int(deadline - time.monotonic()))
            exit_code, stdout, _ = run_command(
                ["virsh", "list", "--name", "--state-running"],
                timeout=min(wait_interval, remaining_budget),
            )
            if exit_code != 0:
                # libvirtd may be wedged or restarting. Don't trust empty
                # stdout as "all stopped" — keep the previous remaining_vms
                # and re-poll on the next interval. If we exhaust max_wait
                # the force-destroy pass below still fires.
                self._log_message(
                    f"  ⚠️  virsh list returned exit {exit_code}; keeping prior "
                    f"remaining VMs ({len(remaining_vms)}) and retrying"
                )
            else:
                still_running = set(vm.strip() for vm in stdout.strip().split('\n') if vm.strip())
                remaining_vms = [vm for vm in running_vms if vm in still_running]

                if not remaining_vms:
                    self._log_message("  ✅  All VMs stopped gracefully.")
                    break

                self._log_message(f"  🕒  Still waiting for: {' '.join(remaining_vms)}")

            # Sleep only up to the remaining budget so we don't overshoot.
            time.sleep(max(0, min(wait_interval, deadline - time.monotonic())))

        if remaining_vms:
            self._log_message("  ⚠️  Timeout reached. Force destroying remaining VMs.")
            for vm in remaining_vms:
                self._log_message(f"  ⚡  Force destroying VM: {vm}")
                run_command(["virsh", "destroy", vm])

        self._log_message("  ✅  All VMs shutdown complete")
