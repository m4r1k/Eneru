"""Remote action templates for Eneru."""

from typing import Dict

# Predefined actions for remote pre-shutdown commands
# {timeout} is replaced with the command timeout in seconds
# {path} is replaced with the compose file path (for stop_compose)
REMOTE_ACTIONS: Dict[str, str] = {
    # Stop all Docker/Podman containers
    "stop_containers": (
        't={timeout}; '
        'docker ps -q | xargs -r docker stop -t $t 2>/dev/null; '
        'podman ps -q | xargs -r podman stop -t $t 2>/dev/null; '
        'true'
    ),

    # Stop libvirt/KVM VMs with graceful shutdown, then force destroy
    "stop_vms": (
        'virsh list --name --state-running | xargs -r -n1 virsh shutdown; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && virsh list --name --state-running | grep -q .; do sleep 1; done; '
        'virsh list --name --state-running | xargs -r -n1 virsh destroy 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox QEMU VMs with graceful shutdown, then force stop.
    # Runs via sudo so the SSH user can be non-root with NOPASSWD on /usr/sbin/qm.
    "stop_proxmox_vms": (
        'sudo qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 sudo qm shutdown --timeout {timeout}; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && sudo qm list | awk \'$3=="running"\' | grep -q .; do sleep 1; done; '
        'sudo qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 sudo qm stop 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox LXC containers with graceful shutdown, then force stop.
    # Runs via sudo so the SSH user can be non-root with NOPASSWD on /usr/sbin/pct.
    "stop_proxmox_cts": (
        'sudo pct list | awk \'NR>1 && $2=="running" {{print $1}}\' | xargs -r -n1 sudo pct shutdown --timeout {timeout}; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && sudo pct list | awk \'$2=="running"\' | grep -q .; do sleep 1; done; '
        'sudo pct list | awk \'NR>1 && $2=="running" {{print $1}}\' | xargs -r -n1 sudo pct stop 2>/dev/null; '
        'true'
    ),

    # Stop XCP-ng/XenServer VMs with graceful shutdown, then force.
    # `xe` parses arguments as key=value pairs, so the UUID must be bound
    # to the `uuid=` parameter via `-I {{}}`; passing it positionally
    # makes `xe` silently ignore it.
    "stop_xcpng_vms": (
        'ids=$(xe vm-list power-state=running is-control-domain=false --minimal); '
        '[ -z "$ids" ] && exit 0; '
        'echo "$ids" | tr \',\' \'\\n\' | xargs -r -I {{}} xe vm-shutdown uuid={{}} 2>/dev/null; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ]; do '
        'ids=$(xe vm-list power-state=running is-control-domain=false --minimal); '
        '[ -z "$ids" ] && break; sleep 1; done; '
        'xe vm-list power-state=running is-control-domain=false --minimal | tr \',\' \'\\n\' | '
        'xargs -r -I {{}} xe vm-shutdown uuid={{}} force=true 2>/dev/null; '
        'true'
    ),

    # Stop VMware ESXi VMs with graceful shutdown, then force power-off
    "stop_esxi_vms": (
        'for i in $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\'); do '
        'vim-cmd vmsvc/power.shutdown $i 2>/dev/null; done; '
        'c=0; while [ $c -lt {timeout} ]; do '
        '[ $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1\' | wc -l) -eq 0 ] && break; '
        'pwr=$(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\' | '
        'while read i; do vim-cmd vmsvc/power.getstate $i 2>/dev/null; done | grep -c "Powered on"); '
        '[ "$pwr" -eq 0 ] && break; sleep 1; c=$((c+1)); done; '
        'for i in $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\'); do '
        'vim-cmd vmsvc/power.off $i 2>/dev/null; done; '
        'true'
    ),

    # Stop docker/podman compose stack.
    # {path} is shell-quoted at the format() call site (see
    # RemoteShutdownMixin._execute_remote_pre_shutdown) — leaving the
    # placeholder bare here so shlex.quote provides the only quoting
    # boundary; double-quoting wouldn't block $(), backticks, or ${...}.
    "stop_compose": (
        't={timeout}; '
        'if command -v docker &>/dev/null && docker compose version &>/dev/null; then '
        'docker compose -f {path} down -t $t; '
        'elif command -v podman &>/dev/null; then '
        'podman compose -f {path} down -t $t; fi; '
        'true'
    ),

    # Sync filesystems
    "sync": 'sync; sync; sleep 2',
}
