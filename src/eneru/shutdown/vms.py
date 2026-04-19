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

        self._log_message("🖥️ Shutting down all libvirt virtual machines...")

        if not command_exists("virsh"):
            self._log_message("  ℹ️ virsh not available, skipping VM shutdown")
            return

        exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
        if exit_code != 0:
            self._log_message("  ⚠️ Failed to get VM list")
            return

        running_vms = [vm.strip() for vm in stdout.strip().split('\n') if vm.strip()]

        if not running_vms:
            self._log_message("  ℹ️ No running VMs found")
            return

        for vm in running_vms:
            self._log_message(f"  ⏹️ Shutting down VM: {vm}")
            if self.config.behavior.dry_run:
                self._log_message(f"  🧪 [DRY-RUN] Would shutdown VM: {vm}")
            else:
                exit_code, stdout, stderr = run_command(["virsh", "shutdown", vm])
                if stdout.strip():
                    self._log_message(f"    {stdout.strip()}")

        if self.config.behavior.dry_run:
            return

        max_wait = self.config.virtual_machines.max_wait
        self._log_message(f"  ⏳ Waiting up to {max_wait}s for VMs to shutdown gracefully...")
        wait_interval = 5
        time_waited = 0
        remaining_vms: List[str] = []

        while time_waited < max_wait:
            exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
            still_running = set(vm.strip() for vm in stdout.strip().split('\n') if vm.strip())
            remaining_vms = [vm for vm in running_vms if vm in still_running]

            if not remaining_vms:
                self._log_message(f"  ✅ All VMs stopped gracefully after {time_waited}s.")
                break

            self._log_message(f"  🕒 Still waiting for: {' '.join(remaining_vms)} (Waited {time_waited}s)")
            time.sleep(wait_interval)
            time_waited += wait_interval

        if remaining_vms:
            self._log_message("  ⚠️ Timeout reached. Force destroying remaining VMs.")
            for vm in remaining_vms:
                self._log_message(f"  ⚡ Force destroying VM: {vm}")
                run_command(["virsh", "destroy", vm])

        self._log_message("  ✅ All VMs shutdown complete")
