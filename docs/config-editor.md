# Config editor and checker

Eneru's config file keeps growing: UPS connection and credentials, triggers,
local VMs and containers, remote servers with sudo and ordering, redundancy
groups, notifications. `eneru config` lets you build or review it one step
at a time, and `eneru config check` inspects an existing file before a real
outage does.

Think of it as moving into a building. `eneru config` is the architect's
walkthrough, one room at a time, explaining every switch. `eneru config
check` is the inspector: it tries every key in every door (NUT login, SSH,
sudo, each shutdown tool) without ever flipping the main breaker.

## `eneru config check`

```bash
sudo eneru config check                    # /etc/ups-monitor/config.yaml
eneru config check --config ./config.yaml  # any file
eneru config check --offline               # skip the live probes
eneru config check --quiet                 # only problems and notes
```

The report groups findings by area. Red `ERROR` means Eneru would refuse to
start or would misbehave during an outage. Yellow `WARN` means it would work,
but you will likely regret it. Green `OK` is a check that passed. The exit
code is `1` when there is at least one error, so the command can gate a CI
job or an Ansible handler.

### What it checks

**Static checks** (always run, no network):

- YAML syntax, the loader's shape rules, and every semantic validation rule
  `eneru validate` applies.
- The same startup preparation `eneru run` performs: container loopback
  synthesis, Kubernetes notices, the missing-loopback contract.
- Privileges: whether this config needs root, and whether local actions are
  delegated over the loopback.
- Local binaries the daemon needs: `upsc`, the poweroff command, `virsh` when
  VMs are enabled, a container runtime, `ssh` for remotes, `logger`.
- Optional Python packages for features you enabled: Apprise, bcrypt,
  paho-mqtt.
- Startup warnings that are easy to miss in the log: dry-run left on, a UPS
  that powers this host with `local_shutdown` disabled, no `is_local` UPS
  with `trigger_on: any`, the API bound to a LAN address over plain HTTP,
  MQTT without TLS or without a scheme.
- A time budget. If the worst-case shutdown sequence takes longer than
  `critical_runtime_threshold`, the host could lose power mid-sequence.

**Live probes** (read-only; skipped with `--offline`):

| Target | What runs |
|--------|-----------|
| UPS | `upsc -l <host>` (the name exists?), `upsc <ups>` (status, charge, runtime, latency), `upscmd -l` with the configured NUT login (credentials work? self-test command exposed?) |
| UPS data | Warns when the UPS doesn't report `battery.charge` / `battery.runtime` (that trigger can never fire), is on battery right now, already reports less runtime than your threshold, or reports a watt rating below your `energy.nominal_power` |
| Remote server | One harmless SSH login (`remote_health.probe_command`, default `true`), then the host-identity probe for loopback entries |
| Predefined actions | `command -v` for every tool, plus a read-only listing: `docker ps -q` / `podman ps -q`, `docker compose version` and the compose file's presence, `virsh list`, `qm list`, `pct list`, `xe vm-list`, `vim-cmd vmsvc/getallvms`, `loginctl list-users`, and whether each unmount target is mounted |
| Shutdown command | `command -v <binary>`; if it runs through sudo, `sudo -n -l <binary> <args>` |
| Custom commands | `command -v <binary>` only; with `use_sudo` or an explicit `sudo`, also `sudo -n -l <binary> <args>` |
| This host | `virsh list`, `<runtime> ps`, `<runtime> compose version`, compose files exist, unmount targets are mounted |

`sudo -n -l <binary> <args>` asks sudo whether the SSH user may run that
exact command without a password, and prints the resolved command. It
**never executes it**. The arguments are included because sudoers rules may
pin them (for example `NOPASSWD: /usr/syno/sbin/synoshutdown -s`). The binary
is resolved by sudo itself, just like the real `sudo -n` call during an
outage. The shutdown command and custom commands are never run by the
checker. Eneru can't know whether an arbitrary command is safe to run, so it
only proves the command is reachable.

Per remote server, the login probe (and, for a loopback, the identity probe)
each use their own short SSH connection; every predefined-action, custom
command and shutdown-command check then runs together in one more session, with the same
PATH augmentation the real shutdown uses (`/usr/sbin`, `/sbin`,
`/usr/local/sbin`, Synology's `/usr/syno/sbin`). A tool the checker finds is
a tool the shutdown will find.

!!! note "`use_sudo` and custom commands"
    `use_sudo: true` runs the **predefined actions**, **custom
    `pre_shutdown_commands`** and the **final shutdown command** through
    `sudo -n` (a command that already starts with `sudo` is left as written).
    Only the first command of a pipeline or list is prefixed. The checker
    notes when a custom command chains more commands under sudo.

### The power-loss preview

The report ends with what happens when the power goes out. For each UPS, it
shows the conditions that start the shutdown. For each redundancy group, it
shows the quorum. Then it lists every phase in order: VMs, containers, sync,
unmounts, remote servers by `shutdown_order` (parallel within a phase), the
final sync and the host poweroff. Each phase shows its time budget, and the
report ends with a worst-case total. This is the same plan the dashboard's
shutdown view uses.

## `eneru config` (the editor)

```bash
sudo eneru config                      # edit /etc/ups-monitor/config.yaml (or create it)
docker exec -it eneru eneru config     # container: edit the running daemon's config
eneru config --config ./config.yaml    # another file
eneru config --basic                   # skip the mode question
eneru config --advanced
```

It opens a full-screen terminal UI (it needs an interactive terminal; use
`eneru config check` in scripts). You pick a mode:

- **Basic**: the essentials, with safe defaults. The UPS and its NUT login,
  dry-run and the main triggers, this host's VMs, containers and
  filesystems, remote servers, notifications, then review and save.
- **Advanced**: every option, grouped by area. It adds redundancy groups, the
  API and dashboard, MQTT, logging, statistics, UPS control, battery health,
  self-test, energy and reports.

Press `M` at any time to switch modes.

Each row shows the current value, marked `(default)` when the key isn't in
your file. The panel below it explains what the selected option does and
how it affects the system. Every stage lists its findings. A red marker in
the stage list means that stage has errors. Moving forward with `N` from a
stage with errors is blocked once: fix them, or press `N` again to continue
anyway.

| Key | Action |
|-----|--------|
| `Up`/`Down`, `j`/`k` | Move |
| `Enter`, `Space`, `Right` | Edit a value, toggle a switch, open a section or list |
| `Esc`, `Left`, `Backspace` | Back |
| `N` / `P`, `Tab` / `Shift-Tab`, `1`-`9` | Next / previous stage, jump to a stage |
| `T` | Test the UPS or remote server under the cursor (live, read-only); elsewhere runs every live check |
| `A` | Add an item (UPS, remote server, pre-shutdown step, compose file, mount, list value) |
| `D` | Delete the selected item, or reset an option to its default |
| `<` / `>` | Move an item up or down (order matters for compose files, mounts and steps) |
| `M` | Switch between basic and advanced mode |
| `S` | Save |
| `Q` | Quit (asks when there are unsaved changes) |

The last stage runs every live check and shows the power-loss preview
before you save.

### How the file is written

- **In place, comments kept.** Eneru edits your file with a round-trip YAML
  parser. Your comments, quoting, key order and indentation style stay as
  they were; only the values you changed differ. The previous version is
  kept as `config.yaml.bak`.
- **What you type is what the daemon reads.** The editor writes YAML 1.2
  and the daemon reads YAML 1.1. A value such as `12:30`, `on`, `yes` or
  `0644` would read differently in the two, so it is quoted automatically.
  Every value and check in the editor comes from the daemon's view of the
  file.
- **Symlinks and bind mounts.** A symlinked config is written through to its
  real file. A single-file bind mount (common in containers) can't be
  replaced atomically, so it is rewritten in place, and when the config's
  directory isn't writable the `.bak` goes to the state directory
  (`statistics.db_directory`, `/var/lib/eneru` in the image). The mode of an
  existing file is kept; its owner and group are kept when the editor runs as
  root or when the file is rewritten in place (the usual container case). A
  non-root save that replaces the file writes it as that user. If the file changed on disk while you were
  editing, the editor asks before overwriting it.
- **New keys get an explanation.** When the editor adds a key (or creates a
  new file), it writes a comment above the key explaining it.
- **New files are private and safe.** A new file is created with mode `0600`
  (it can hold NUT, SSH and Apprise secrets). It starts with
  `behavior.dry_run: true`, so nothing is powered off until you have seen the
  preview and turned dry-run off.
- **Adding a second UPS** converts a single-UPS config to the multi-UPS
  list form. The existing UPS becomes the first entry, marked as the one
  that powers this host, and takes its VMs, containers, filesystems and
  remote servers with it.

After saving, apply the change with a hot reload (or a restart):

- package install: `sudo systemctl reload eneru`
- container: `docker kill -s HUP eneru` (Podman: `podman kill -s HUP eneru`)

## Requirements

The editor uses `ruamel.yaml`. It is a *recommended* package on deb and
rpm: the daemon and `eneru config check` never need it, so installing Eneru
never fails because of it. Without it, `eneru config` prints the command to
install it.

- **deb** (Debian 12/13, Ubuntu 22.04+): `python3-ruamel.yaml`, pulled in
  automatically (apt installs Recommends by default).
- **rpm**: `python3-ruamel-yaml`, pulled in automatically when its repo is
  enabled. RHEL 10 ships it in AppStream. On RHEL 9 it lives in CodeReady
  Builder (CRB): `sudo dnf config-manager --set-enabled crb` (Rocky/Alma) or
  `sudo subscription-manager repos --enable codeready-builder-for-rhel-9-$(arch)-rpms`
  (RHEL), then `sudo dnf install python3-ruamel-yaml`.
- **pip / container image**: always installed (a core dependency, bundled in
  the image).
