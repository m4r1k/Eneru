# Getting Started

## Prerequisites

Before installing Eneru, ensure you have:

- **Python 3.9 or higher**
- **NUT (Network UPS Tools) client** - Your UPS must already be configured with NUT
- **SSH client** - For remote server shutdown (optional)
- **Root privileges** - Required for system shutdown operations

!!! note "Eneru monitors NUT, it doesn't replace it"
    Eneru connects to an existing NUT server to read UPS status. You need a working NUT installation with your UPS already configured before using Eneru.

---

## Package Installation (Recommended)

Native `.deb` and `.rpm` packages are available for easy installation.

### Option 1: APT/DNF Repository (Recommended)

Add the repository to get automatic updates:

=== "Debian/Ubuntu"

    ```bash
    # Import GPG key
    curl -fsSL https://m4r1k.github.io/Eneru/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/eneru.gpg

    # Add repository
    echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb stable main" | sudo tee /etc/apt/sources.list.d/eneru.list

    # Install
    sudo apt update
    sudo apt install eneru
    ```

=== "RHEL/Fedora"

    ```bash
    # RHEL 8/9: Enable EPEL first (required for apprise dependency)
    sudo dnf install -y epel-release

    # Add repository
    sudo curl -o /etc/yum.repos.d/eneru.repo https://m4r1k.github.io/Eneru/rpm/eneru.repo

    # Install
    sudo dnf install eneru
    ```

### Option 2: PyPI (pip)

Install from PyPI for a quick setup:

```bash
pip install eneru

# With notification support (recommended)
pip install eneru[notifications]
```

!!! note "System packages still recommended for production"
    The pip installation doesn't include the systemd service file. For production deployments, use the native packages which set up the service automatically.

After pip install, you'll need to:

1. Create the config directory: `sudo mkdir -p /etc/ups-monitor`
2. Create a config file: `sudo nano /etc/ups-monitor/config.yaml`
3. Run manually or set up your own service: `eneru --config /etc/ups-monitor/config.yaml`

### Option 3: Direct Download from GitHub Releases

Download the latest package from [GitHub Releases](https://github.com/m4r1k/Eneru/releases):

=== "Debian/Ubuntu"

    ```bash
    sudo dpkg -i eneru_*.deb
    sudo apt install -f  # Install dependencies if needed
    ```

=== "RHEL/Fedora"

    ```bash
    # RHEL 8/9: Enable EPEL first (required for apprise dependency)
    sudo dnf install -y epel-release

    sudo dnf install ./eneru-*.noarch.rpm
    ```

---

## After Installation

The package installs but does **not** auto-enable or auto-start the service. You must complete configuration first:

```bash
# 1. Edit configuration
sudo nano /etc/ups-monitor/config.yaml

# 2. Validate configuration
sudo python3 /opt/ups-monitor/eneru.py --validate-config

# 3. Enable and start the service
sudo systemctl enable eneru.service
sudo systemctl start eneru.service
```

---

## Minimal Configuration

Here's the simplest working configuration:

```yaml
# /etc/ups-monitor/config.yaml

# UPS connection (required)
ups:
  name: "UPS@192.168.1.100"

# Shutdown triggers
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

# Enable local shutdown
local_shutdown:
  enabled: true
```

This will:

- Connect to NUT server at `192.168.1.100` and monitor the UPS named `UPS`
- Trigger shutdown when battery drops below 20% or runtime below 10 minutes
- Execute `shutdown -h now` on the local system

For full configuration options, see [Configuration](configuration.md).

---

## Verify Installation

After starting the service, verify it's working:

```bash
# Check service status
sudo systemctl status eneru.service

# View logs
sudo journalctl -u eneru.service -f

# Check current UPS state
cat /var/run/ups-monitor.state
```

---

## Upgrading

When upgrading via APT/DNF, your configuration file is preserved. The package manager will not overwrite `/etc/ups-monitor/config.yaml`.

If new configuration options are added in a release, check the [Changelog](changelog.md) for details on new features.

---

## Uninstalling

=== "Debian/Ubuntu"

    ```bash
    sudo apt remove eneru
    # To also remove configuration:
    sudo apt purge eneru
    ```

=== "RHEL/Fedora"

    ```bash
    sudo dnf remove eneru
    ```

=== "pip"

    ```bash
    pip uninstall eneru
    ```

Configuration files in `/etc/ups-monitor/` may be preserved after uninstall. Remove them manually if desired.

---

## Next Steps

- [Configuration](configuration.md) - Full configuration reference
- [Shutdown Triggers](triggers.md) - Understand how shutdown decisions are made
- [Notifications](notifications.md) - Set up Discord, Slack, Telegram, and more
