# Getting started

This page gets one UPS monitoring safely. Add VMs, containers, remote servers, and multi-UPS policy after the basic loop works.

## Prerequisites

- Linux with Python 3.9 or newer.
- A working NUT server that answers `upsc` for your UPS.
- Root access on the Eneru host. Shutdown, filesystem, VM, and container actions need host privileges.
- SSH client access if Eneru will shut down remote servers.

Check NUT first:

```bash
upsc -l 192.168.1.100
upsc UPS@192.168.1.100
```

If those commands fail, fix NUT before installing Eneru.

## Install

### Debian or Ubuntu package

```bash
curl -fsSL https://m4r1k.github.io/Eneru/KEY.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/eneru.gpg

echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/eneru.list

sudo apt update
sudo apt install eneru
```

### RHEL or Fedora package

```bash
sudo dnf install -y epel-release
sudo curl -o /etc/yum.repos.d/eneru.repo https://m4r1k.github.io/Eneru/rpm/eneru.repo
sudo dnf install eneru
```

### PyPI

```bash
python3 -m venv ~/.venv/eneru
source ~/.venv/eneru/bin/activate
pip install "eneru[notifications]"
```

The PyPI install is useful for development or user-managed services. Native packages are recommended for production because they install the wrapper, config directory, systemd unit, shell completions, and package-managed dependencies.

## Create the first config

Edit `/etc/ups-monitor/config.yaml`:

```yaml
ups:
  name: "UPS@192.168.1.100"

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

local_shutdown:
  enabled: true
```

This connects to `UPS` on `192.168.1.100`, starts shutdown at 20% battery or 10 minutes estimated runtime, and powers off the local host with `shutdown -h now`.

## Validate

Package install:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
```

PyPI install:

```bash
eneru validate --config /etc/ups-monitor/config.yaml
```

Validation prints the UPS groups, enabled resources, remote shutdown phases, notification status, and configuration errors. Do not start the service until validation passes.

## Test in dry-run mode

Dry-run logs every action but does not stop VMs, containers, remote servers, filesystems, or the host.

Package install:

```bash
sudo eneru run --dry-run --config /etc/ups-monitor/config.yaml
```

PyPI install:

```bash
eneru run --dry-run --config /etc/ups-monitor/config.yaml
```

Use a real power event only when you can recover the machine locally. For early tests, lower `extended_time.threshold` in the config and keep `behavior.dry_run: true`.

## Start the service

The package does not enable the service automatically. Enable it only after validation and dry-run testing.

```bash
sudo systemctl enable --now eneru.service
sudo systemctl status eneru.service
sudo journalctl -u eneru.service -f
```

The packaged service runs the same command through systemd:

```bash
sudo eneru run --config /etc/ups-monitor/config.yaml
```

## Watch status

Use the TUI on a terminal session:

```bash
sudo eneru monitor --config /etc/ups-monitor/config.yaml
```

For scripts or SSH sessions that should not open curses:

```bash
sudo eneru monitor --once --config /etc/ups-monitor/config.yaml
sudo eneru monitor --once --events-only --config /etc/ups-monitor/config.yaml
```

## Add features one at a time

Do not add every feature at once. Add one section, validate, run dry-run, then move on.

| Next step | Page |
|-----------|------|
| Full config keys and defaults | [Configuration reference](configuration.md) |
| Battery, runtime, depletion, and voltage policy | [Shutdown triggers](triggers.md) |
| Discord, Slack, Telegram, ntfy, email | [Notifications](notifications.md) |
| NAS, Proxmox, ESXi, XCP-ng, Docker hosts | [Remote servers](remote-servers.md) |
| Multiple independent UPSes | [Configuration reference](configuration.md#multi-ups-example) |
| Dual-PSU A+B power | [Redundancy groups](redundancy-groups.md) |
