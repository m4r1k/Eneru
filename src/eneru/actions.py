"""Remote action templates for Eneru.

Each entry is a shell snippet rendered with ``str.format(**ctx)`` and
shipped over SSH (or executed locally) by ``RemoteShutdownMixin``. The
templates are the **single source of truth** for what each action does
on the remote host — see ``shutdown/CONTRACT.md``-equivalent comments
inline.

Render context (always supplied by ``_render_action_context`` in
``shutdown/remote.py``; missing keys would KeyError at format time):

* ``timeout`` — int seconds; per-command timeout. Honored by the
  templates' graceful-wait loops and by ``timeout(1)`` invocations.
* ``path`` — ``shlex.quote``d compose-file path for ``stop_compose``;
  empty string otherwise.
* ``skip_ids`` — comma-separated list of container ID prefixes
  (12-char truncations are matched). Mandatory self-skip for the v5.5
  container loopback delegate so the host doesn't kill Eneru's own
  container mid-sequence. Empty when no skipping is required.
* ``wait_interval`` — int seconds; poll interval inside graceful-wait
  loops. Default 1 (matches pre-v5.5 behavior).
* ``umount_targets`` — newline-separated ``mount_point options``
  records for ``unmount_filesystems``. Empty disables the action.
* ``sudo`` — either ``"sudo -n "`` when ``remote_servers[].use_sudo``
  is enabled, or the empty string. Used only on privileged write-side
  host actions; rootless Podman keeps its explicit ``sudo -u`` calls.

Placeholders that may contain literal ``{`` / ``}`` characters (awk
programs, embedded shell ``${var}``) must be doubled to ``{{`` /
``}}`` so ``str.format`` leaves them alone.
"""

import shlex
from typing import Dict, List, Optional, Tuple, Union

# Predefined actions for remote pre-shutdown commands.
REMOTE_ACTIONS: Dict[str, str] = {
    # Stop all Docker/Podman containers.
    # v5.5: ``skip_ids`` filter is **mandatory** for the loopback path —
    # Eneru must never stop its own container. The container loopback
    # delegate computes the skip set from the in-container's detected IDs
    # (hostname / cgroup / mountinfo) and embeds it here. For regular
    # remote targets the skip set is empty and the filter is a no-op.
    "stop_containers": (
        't={timeout}; '
        'skip="{skip_ids}"; '
        # Filter by 12-char prefix in both directions so short IDs from
        # `docker ps -q` match full IDs in the skip set and vice versa.
        '_filter() {{ awk -v skip="$skip" \''
        'BEGIN {{ n = split(skip, a, ","); for (i=1;i<=n;i++) m[substr(a[i],1,12)]=1 }} '
        '!(substr($0,1,12) in m)\'; }}; '
        'command -v docker >/dev/null 2>&1 && '
        '{sudo}docker ps -q 2>/dev/null | _filter | xargs -r {sudo}docker stop -t $t 2>/dev/null; '
        'command -v podman >/dev/null 2>&1 && '
        '{sudo}podman ps -q 2>/dev/null | _filter | xargs -r {sudo}podman stop -t $t 2>/dev/null; '
        'true'
    ),

    # Stop rootless Podman containers across every non-system user.
    # v5.5: separate action so it can be omitted on hosts without
    # rootless Podman (cheap on a Linux server, but skips the loginctl
    # call on hosts where it would error). Same mandatory skip set as
    # ``stop_containers``.
    "stop_containers_rootless": (
        't={timeout}; '
        'skip="{skip_ids}"; '
        '_filter() {{ awk -v skip="$skip" \''
        'BEGIN {{ n = split(skip, a, ","); for (i=1;i<=n;i++) m[substr(a[i],1,12)]=1 }} '
        '!(substr($0,1,12) in m)\'; }}; '
        'command -v loginctl >/dev/null 2>&1 || exit 0; '
        'loginctl list-users --no-legend 2>/dev/null | '
        'awk \'$1+0 >= 1000 {{print $2}}\' | '
        'while read -r user; do '
        '  sudo -u "$user" podman ps -q 2>/dev/null | _filter | '
        '  xargs -r sudo -u "$user" podman stop -t $t 2>/dev/null; '
        'done; '
        'true'
    ),

    # Stop libvirt/KVM VMs with graceful shutdown, then force destroy.
    # v5.5: ``wait_interval`` parameterized so the in-process and
    # delegated paths can share timing.
    "stop_vms": (
        't={timeout}; '
        'wait={wait_interval}; '
        '{sudo}virsh list --name --state-running | xargs -r -n1 {sudo}virsh shutdown; '
        # F-006: portable elapsed-seconds counter. $SECONDS is a bash-only
        # variable; on dash/BusyBox remotes it is empty, so the old
        # `$((SECONDS+t))` deadline was `t` and the grace loop never ran
        # (immediate destroy). Accumulate `wait` per iteration instead.
        'c=0; '
        'while [ $c -lt $t ] && {sudo}virsh list --name --state-running | grep -q .; do sleep $wait; c=$((c+wait)); done; '
        '{sudo}virsh list --name --state-running | xargs -r -n1 {sudo}virsh destroy 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox QEMU VMs with graceful shutdown, then force stop.
    # Runs via sudo so the SSH user can be non-root with NOPASSWD on /usr/sbin/qm.
    "stop_proxmox_vms": (
        'sudo qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 sudo qm shutdown --timeout {timeout}; '
        # F-006: portable counter (see stop_vms) — $SECONDS is bash-only.
        'c=0; '
        'while [ $c -lt {timeout} ] && sudo qm list | awk \'$3=="running"\' | grep -q .; do sleep 1; c=$((c+1)); done; '
        'sudo qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 sudo qm stop 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox LXC containers with graceful shutdown, then force stop.
    # Runs via sudo so the SSH user can be non-root with NOPASSWD on /usr/sbin/pct.
    "stop_proxmox_cts": (
        'sudo pct list | awk \'NR>1 && $2=="running" {{print $1}}\' | xargs -r -n1 sudo pct shutdown --timeout {timeout}; '
        # F-006: portable counter (see stop_vms) — $SECONDS is bash-only.
        'c=0; '
        'while [ $c -lt {timeout} ] && sudo pct list | awk \'$2=="running"\' | grep -q .; do sleep 1; c=$((c+1)); done; '
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
        # F-006: portable counter (see stop_vms) — $SECONDS is bash-only.
        'c=0; '
        'while [ $c -lt {timeout} ]; do '
        'ids=$(xe vm-list power-state=running is-control-domain=false --minimal); '
        '[ -z "$ids" ] && break; sleep 1; c=$((c+1)); done; '
        'xe vm-list power-state=running is-control-domain=false --minimal | tr \',\' \'\\n\' | '
        'xargs -r -I {{}} xe vm-shutdown uuid={{}} force=true 2>/dev/null; '
        'true'
    ),

    # Stop VMware ESXi VMs with graceful shutdown, then force power-off.
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
    # ``path`` is shell-quoted by render_action before it is assigned into
    # the shell variable. Every later use still double-quotes "$path" so
    # spaces survive the shell's word-splitting pass.
    # v5.5: self-skip — if this compose stack contains any container in
    # ``skip_ids`` (the loopback delegate's own container ID set), the
    # stack is left alone so the host shutdown doesn't kill Eneru
    # mid-sequence.
    "stop_compose": (
        't={timeout}; '
        'path={path}; '
        'skip="{skip_ids}"; '
        '_compose() {{ '
        '  if command -v docker >/dev/null 2>&1 && {sudo}docker compose version >/dev/null 2>&1; then '
        '    {sudo}docker compose "$@"; '
        '  elif command -v podman >/dev/null 2>&1; then '
        '    {sudo}podman compose "$@"; '
        '  else return 127; fi; }}; '
        # Detect self-inclusion: if any container in this stack matches
        # a 12-char prefix from skip_ids, abort the stack teardown.
        'if [ -n "$skip" ]; then '
        '  hit=$(_compose -f "$path" ps -q 2>/dev/null | awk -v skip="$skip" '
        '\'BEGIN {{ n = split(skip, a, ","); for (i=1;i<=n;i++) m[substr(a[i],1,12)]=1 }} '
        'substr($0,1,12) in m {{ print; exit }}\'); '
        '  if [ -n "$hit" ]; then exit 0; fi; '
        'fi; '
        '_compose -f "$path" down -t "$t"; '
        'true'
    ),

    # Unmount filesystems with configurable per-mount options.
    # v5.5: NEW template; ``unmount`` was missing from REMOTE_ACTIONS
    # entirely until now (the existing ``sync`` template only flushed
    # caches). Format of ``umount_targets``: newline-separated
    # shell-quoted ``mount_point options`` records. Empty options field is fine.
    "unmount_filesystems": (
        't={timeout}; '
        'targets={umount_targets}; '
        '[ -z "$targets" ] && exit 0; '
        'printf "%s\\n" "$targets" | while IFS= read -r record; do '
        '  [ -z "$record" ] && continue; '
        '  eval "set -- $record"; '
        '  mp=$1; opts=$2; '
        '  [ -z "$mp" ] && continue; '
        '  if [ -n "$opts" ]; then '
        '    timeout "$t" {sudo}umount $opts "$mp" 2>/dev/null || '
        '      timeout "$t" {sudo}umount -l "$mp" 2>/dev/null || true; '
        '  else '
        '    timeout "$t" {sudo}umount "$mp" 2>/dev/null || '
        '      timeout "$t" {sudo}umount -l "$mp" 2>/dev/null || true; '
        '  fi; '
        'done; '
        'true'
    ),

    # Sync filesystems
    "sync": 'sync; sync; sleep 2',
}


# v5.5: which placeholders each template requires. Used by
# ``_render_action_context`` in shutdown/remote.py to only supply the
# keys a given template actually needs (and by the parity / drift
# detector tests to verify every action is wired through the registry).
REMOTE_ACTION_PLACEHOLDERS: Dict[str, Tuple[str, ...]] = {
    "stop_containers": ("timeout", "skip_ids", "sudo"),
    "stop_containers_rootless": ("timeout", "skip_ids"),
    "stop_vms": ("timeout", "wait_interval", "sudo"),
    "stop_proxmox_vms": ("timeout",),
    "stop_proxmox_cts": ("timeout",),
    "stop_xcpng_vms": ("timeout",),
    "stop_esxi_vms": ("timeout",),
    "stop_compose": ("timeout", "path", "skip_ids", "sudo"),
    "unmount_filesystems": ("timeout", "umount_targets", "sudo"),
    "sync": (),
}


def render_action(
    action_name: str,
    *,
    timeout: int,
    path: str = "",
    skip_ids: str = "",
    umount_targets: str = "",
    wait_interval: int = 1,
    use_sudo: bool = False,
) -> str:
    """Render a REMOTE_ACTIONS template with the supplied context.

    Centralizes the format() call so callers (the SSH path in
    ``RemoteShutdownMixin._execute_remote_pre_shutdown`` and any future
    local executor) can't accidentally pass an incomplete context. All
    keyword args default to safe empty/no-op values so a non-loopback
    caller doesn't need to know which template uses which placeholder.
    """
    template = REMOTE_ACTIONS[action_name]
    sudo = "sudo -n " if use_sudo else ""
    rendered_path = shlex.quote(path) if action_name == "stop_compose" else path
    return template.format(
        timeout=timeout,
        path=rendered_path,
        skip_ids=skip_ids,
        umount_targets=shlex.quote(umount_targets),
        wait_interval=wait_interval,
        sudo=sudo,
    )


def serialize_umount_targets(
    mounts: List[Union[str, Dict[str, Optional[str]]]],
) -> str:
    """Serialize unmount config entries into the ``unmount_filesystems`` format.

    Input: list of mount path strings or ``{"path": str, "options": str | None}``
    mappings, matching the public filesystems config shape.

    Output: newline-separated shell-quoted ``mount_point options`` records,
    ready to embed into the rendered shell template. ``shlex.quote`` keeps
    paths with spaces, pipes, quotes, or shell metacharacters as data.
    """
    records: List[str] = []
    for m in mounts:
        if isinstance(m, str):
            mp = m.strip()
            opts = ""
        else:
            mp = (m.get("path") or "").strip()
            opts = (m.get("options") or "").strip()
        if not mp:
            continue
        records.append(f"{shlex.quote(mp)} {shlex.quote(opts)}")
    return "\n".join(records)
