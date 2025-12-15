# UPS Monitor 3.0 - Installation and Configuration Guide

This Python script provides advanced, high-frequency monitoring of a UPS via a Network UPS Tools (NUT) server. It implements intelligent, multi-vector shutdown triggers and a robust, prioritized shutdown sequence to protect local VMs, Docker containers, and remote systems (like a Synology NAS) during power events.

## Version History

### Version 3.0 (Current) - Python Rewrite
Complete rewrite from Bash to Python, offering improved reliability, maintainability, and native handling of JSON, math operations, and complex data structures.

### Version 2.0 - Enhanced Bash
Major improvements including Discord notifications, stateful event tracking, dynamic VM wait times, hang-proof unmounting, and SSH key-based authentication for remote NAS shutdown.

### Version 1.0 - Original Bash
Initial implementation with basic UPS monitoring, battery depletion tracking, and shutdown sequence for VMs, Docker, and remote NAS.

---

## What's New in Each Version

### Version 3.0 Changes (from 2.0)

| Feature | Version 2.0 (Bash) | Version 3.0 (Python) |
|---------|-------------------|---------------------|
| Language | Bash 4.0+ | Python 3.9+ |
| JSON Handling | External `jq` dependency | Native Python (`requests` library) |
| Math Operations | External `bc` dependency | Native Python |
| Configuration | Shell variables | Python dataclass with type hints |
| State Management | File-based with shell parsing | In-memory with file persistence |
| Type Safety | None | Full type hints |
| Error Handling | Shell traps | Python exceptions |
| String Formatting | Shell variable expansion | Python string concatenation |

**Removed Dependencies in 3.0:**
- `jq` - JSON now handled natively
- `bc` - Math now handled natively
- `awk` - Text processing now handled natively
- `grep` - Pattern matching now handled natively

**New Dependencies in 3.0:**
- `python3-requests` - For Discord webhook notifications

### Version 2.0 Changes (from 1.0)

| Feature | Version 1.0 | Version 2.0 |
|---------|-------------|-------------|
| Notifications | None | Discord webhooks with rich embeds |
| Event Tracking | Basic logging | Stateful tracking (prevents log spam) |
| VM Shutdown | Fixed 10s wait | Dynamic wait up to 30s with force destroy |
| NAS Authentication | Password in script (`sshpass`) | SSH key-based (no passwords stored) |
| Unmounting | Basic unmount | Timeout-protected unmount (hang-proof) |
| Voltage Monitoring | Relative change detection | Absolute threshold-based detection |
| Depletion Rate | 60-second window, 15 samples | 300-second window, 30 samples, grace period |
| AVR Monitoring | None | Boost/Trim detection |
| Connection Handling | Basic retry | Stale data detection, failsafe shutdown |
| Crisis Reporting | None | Elevated notifications during shutdown |
| Shutdown Triggers | 4 triggers | 4 triggers + FSD flag detection |
| Configuration | Minimal | Comprehensive with mount options |

**New Features in 2.0:**
- Discord webhook integration with color-coded embeds
- Depletion rate grace period (prevents false triggers on power loss)
- Failsafe Battery Protection (FSB) - shutdown if connection lost while on battery
- FSD (Forced Shutdown) flag detection from UPS
- Configurable mount list with per-mount options (e.g., lazy unmount)
- Overload state tracking with resolution detection
- Bypass mode detection
- Service stop notifications
- Bash 4.0+ requirement for associative arrays
- `jq` requirement for robust JSON generation

**Security Improvements in 2.0:**
- Removed `sshpass` and password storage
- SSH key-based authentication for NAS
- Passwordless sudo configuration guide

---

## UPS Monitor 3.0 Capabilities

UPS Monitor 3.0 is designed for maximum robustness, performance, and visibility in complex environments.

### High-Performance and Reliability
- **Optimized Polling:** The script polls the NUT server every second (configurable) with minimal overhead by fetching all metrics in a single network call (`upsc`) and processing them efficiently in memory.
- **Robust Error Handling:** Implements comprehensive input validation on all data received from the UPS to prevent script failure due to corrupted or transient data.
- **Atomic State Updates:** Uses atomic file operations for tracking state and battery history, ensuring data integrity even if the script is interrupted.
- **Dependency Verification:** Checks for all required system utilities on startup.
- **Type Safety:** Full Python type hints throughout the codebase for better reliability.

### Advanced Battery and Power Analysis
- **Depletion Rate Calculation:** The script maintains a rolling 5-minute history of battery charge levels to calculate the actual depletion rate (% per minute). This allows the script to adapt dynamically to real-time load changes.
- **Depletion Rate Grace Period:** A 90-second grace period after power loss prevents false shutdown triggers from initial battery fluctuations.
- **Multi-Layered Shutdown Triggers:** Shutdown is initiated based on the first condition met:
    1. **FSD Flag:** UPS signals forced shutdown (highest priority)
    2. **Critical Battery Level:** (e.g., < 20%)
    3. **Critical Runtime Remaining:** (e.g., < 10 minutes)
    4. **Dangerous Depletion Rate:** (e.g., > 15% per minute, after grace period)
    5. **Extended Time on Battery:** A crucial safety net (e.g., 15 minutes) to protect against inaccurate battery estimates or aged batteries that may suddenly fail.
- **Threshold-Based Voltage Monitoring:** Monitors absolute input voltage against dynamically determined (or manually set) thresholds to accurately detect sustained brownouts and over-voltage conditions.
- **Stateful Tracking:** All metrics (Voltage, AVR, Overload, Bypass, Connection Status) use state machines to prevent log spam, logging only when a status *changes*.
- **Failsafe Battery Protection (FSB):** If connection to the UPS is lost while running on battery, the script triggers an immediate emergency shutdown to prevent data loss.

### Comprehensive Shutdown Sequence
A prioritized sequence ensures data integrity across the infrastructure:
1. **Virtual Machines (libvirt/KVM):** Initiates graceful shutdown.
2. **Dynamic Wait:** Waits dynamically for VMs to stop (up to a configurable timeout, e.g., 30s) before forcing them off.
3. **Containers (Docker):** Stops all running containers.
4. **Filesystem Sync:** Flushes buffers to disk.
5. **Hang-Proof Unmounting:** Unmounts local and network shares using timeouts to prevent the shutdown from hanging due to unresponsive network mounts.
6. **Remote NAS Shutdown:** Sends a shutdown command to a remote Synology NAS (using secure SSH keys and configured `sudoers`).
7. **Local Host Shutdown:** Initiates the final shutdown of the monitoring server.

### Real-Time Notifications and Crisis Reporting
- **Robust Discord Integration:** Sends non-blocking notifications via webhook for all critical power events using the Python `requests` library for reliable JSON handling.
- **Color-Coded Embeds:** Different colors for different event types (red for critical, orange for warnings, green for restored, blue for info).
- **Elevated Crisis Reporting:** When a shutdown sequence begins, the notification level is automatically elevated. Every subsequent action (e.g., "Stopping VM X", "Unmounting Y", "NAS command sent") is immediately forwarded to Discord for full visibility during the emergency.
- **Service Lifecycle Notifications:** Notifications when the service starts or stops.

---

## Installation Guide

### 1. Prerequisites

The script requires Python 3.9+ and several system utilities.

#### Install Dependencies

**RHEL 9 / Fedora / CentOS Stream:**
```bash
sudo dnf install python3 python3-requests nut-client openssh-clients util-linux coreutils
```

**Debian / Ubuntu:**
```bash
sudo apt update
sudo apt install python3 python3-requests nut-client openssh-client util-linux coreutils
```

**Arch Linux:**
```bash
sudo pacman -S python python-requests nut openssh util-linux coreutils
```

**Key Dependencies Explained:**
- `python3`: Python 3.9 or higher
- `python3-requests`: HTTP library for Discord notifications
- `nut-client`: For `upsc` to communicate with the UPS
- `openssh-client(s)`: For remote NAS shutdown via SSH

**Verify Python Version:**
```bash
python3 --version
# Must be 3.9 or higher
```

### 2. Configure Remote NAS (Security Setup)

The script is designed to shut down a remote Synology NAS securely using SSH key authentication and passwordless `sudo`. **It does not store passwords.** The monitoring script runs as the `root` user.

> **Note:** Version 1.0 used `sshpass` with a password stored in the script. This was replaced in Version 2.0 with SSH key authentication for improved security.

#### 2a. Generate and Copy SSH Key

On the monitoring server, generate a dedicated SSH key and copy it to the NAS.

```bash
# Switch to root
sudo su

# Generate the key (press enter for defaults, no passphrase)
ssh-keygen -t ed25519 -f ~/.ssh/id_nas_shutdown -C "ups-monitor@$(hostname)"

# Copy the key to the NAS (You will need the nas-admin password this one time)
ssh-copy-id -i ~/.ssh/id_nas_shutdown.pub nas-admin@192.168.178.229
```

#### 2b. Configure Passwordless Sudo on Synology NAS

You must configure the NAS to allow the `nas-admin` user to run the `synoshutdown` command without a password.

1. SSH into the Synology NAS: `ssh nas-admin@192.168.178.229`
2. Elevate to root: `sudo -i`
3. Create a specific `sudoers` rule allowing the shutdown command:
    ```bash
    echo "nas-admin ALL=(ALL) NOPASSWD: /usr/syno/sbin/synoshutdown -s" > /etc/sudoers.d/ups_shutdown
    ```
4. Set correct permissions on the new file:
    ```bash
    chmod 0440 /etc/sudoers.d/ups_shutdown
    ```
5. Exit the root shell and the SSH session: `exit`, then `exit`.

#### 2c. Verification

Back on the monitoring server (as root), test the connection.

```bash
# Switch to root
sudo su

# Test the configuration. This should execute without asking for a password.
# WARNING: This command will shut down your NAS immediately!
ssh nas-admin@192.168.178.229 "sudo -i synoshutdown -s"
```

### 3. Install the Monitoring Script

Create the installation directory and copy the script:

```bash
# Create directory
sudo mkdir -p /opt/ups-monitor

# Copy the script (assuming it's in your current directory)
sudo cp ups-monitor.py /opt/ups-monitor/ups-monitor.py

# Make it executable
sudo chmod +x /opt/ups-monitor/ups-monitor.py
```

### 4. Configure the Script

Edit the configuration in the `Config` class at the top of the script:

```bash
sudo nano /opt/ups-monitor/ups-monitor.py
```

Adjust these key values in the `Config` dataclass:

```python
@dataclass
class Config:
    # UPS Configuration
    UPS_NAME: str = "UPS@192.168.178.11"  # Your UPS identifier
    CHECK_INTERVAL: int = 1                # Seconds between checks
    LOW_BATTERY_THRESHOLD: int = 20        # Percentage for immediate shutdown
    CRITICAL_RUNTIME_THRESHOLD: int = 600  # Seconds (10 minutes)
    CRITICAL_DEPLETION_RATE: float = 15.0  # %/minute threshold
    DEPLETION_RATE_GRACE_PERIOD: int = 90  # Seconds after power loss
    
    # Notification Configuration
    DISCORD_WEBHOOK_URL: str = "https://discord.com/api/webhooks/..."  # Your webhook
    
    # Shutdown Behavior
    DRY_RUN_MODE: bool = False  # Set to True for testing
    MAX_VM_WAIT: int = 30       # Maximum seconds to wait for VMs
    EXTENDED_TIME: int = 15 * 60  # 15 minutes safety net
    EXTENDED_TIME_ON_BATTERY_SHUTDOWN: bool = True
    
    # Remote NAS Configuration
    REMOTE_NAS_USER: str = "nas-admin"
    REMOTE_NAS_HOST: str = "192.168.178.229"
    
    # Unmount Configuration
    UNMOUNT_TIMEOUT: int = 15  # Seconds before unmount times out
    MOUNTS_TO_UNMOUNT: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("/mnt/media", ""),
        ("/mnt/nas", "-l"),      # -l for lazy unmount
        ("/mnt/backup", ""),
    ])
```

**DRY-RUN MODE:** Always test with `DRY_RUN_MODE = True` first to verify the logic and notifications work correctly!

### 5. Install the systemd Service

Create the service file:

```bash
sudo nano /etc/systemd/system/ups-monitor.service
```

Copy this content:

```ini
[Unit]
Description=UPS Monitor 3.0 Service (Python)
# Ensure dependencies are started first
After=network-online.target libvirtd.service docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
ExecStart=/usr/bin/python3 /opt/ups-monitor/ups-monitor.py
# Automatically restart the script if it crashes
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Ensure Python output is not buffered (important for real-time logging)
Environment=PYTHONUNBUFFERED=1

# Shutdown Behavior Configuration
# Wait up to 120 seconds for the script to finish if stopped (e.g., during system shutdown)
TimeoutStopSec=120
# Send the signal only to the main script process, not its children (like virsh/docker commands)
KillMode=mixed

# Basic Security hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Save and exit (Ctrl+X, Y, Enter).

### 6. Enable and Start the Service

```bash
# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable ups-monitor.service

# Start the service
sudo systemctl start ups-monitor.service
```

You should receive a "Service Started" notification in Discord shortly after starting.

---

## Management and Testing

### Management Commands

```bash
# Check service status
sudo systemctl status ups-monitor.service

# View real-time logs (System Journal - Primary)
sudo journalctl -u ups-monitor.service -f

# View detailed log file (Secondary)
sudo tail -f /var/log/ups-monitor.log

# Check current UPS state snapshot
cat /var/run/ups-monitor.state
```

### Testing Procedures

#### Dry-Run Mode (HIGHLY RECOMMENDED FIRST)

Always test with Dry-Run mode first to verify the logic and shutdown sequence work correctly without interruption.

1. Edit the script (`/opt/ups-monitor/ups-monitor.py`) and set `DRY_RUN_MODE: bool = True` in the Config class.
2. **Optional:** Temporarily lower `EXTENDED_TIME` to `60` (1 minute) to trigger the shutdown sequence faster during testing.
3. Restart the service: `sudo systemctl restart ups-monitor.service`
4. Watch the logs: `sudo journalctl -u ups-monitor.service -f`
5. Simulate a power outage (e.g., by unplugging the UPS input power).
6. Observe the logs and Discord notifications. All shutdown actions will be logged with a `[DRY-RUN]` prefix but not executed.
7. **Crucial:** Remember to revert `DRY_RUN_MODE` to `False` (and reset `EXTENDED_TIME`) for production.

#### Manually Resetting an Active Sequence

If a shutdown sequence (Dry-Run or real) is active and you want to manually clear the "Crisis Mode" state:

```bash
# The script checks for this file to know if a shutdown is active.
sudo rm -f /var/run/ups-shutdown-scheduled
```

---

## Upgrading Guide

### Upgrading from Version 2.0 (Bash)

1. **Stop the old service:**
    ```bash
    sudo systemctl stop ups-monitor.service
    sudo systemctl disable ups-monitor.service
    ```

2. **Install Python dependencies:**
    ```bash
    # RHEL/Fedora
    sudo dnf install python3-requests
    
    # Debian/Ubuntu
    sudo apt install python3-requests
    ```

3. **Install the new script:**
    ```bash
    sudo mkdir -p /opt/ups-monitor
    sudo cp ups-monitor.py /opt/ups-monitor/
    sudo chmod +x /opt/ups-monitor/ups-monitor.py
    ```

4. **Update the service file** (see Section 5 above)

5. **Migrate your configuration:** Copy your settings from the old Bash script to the new Python `Config` class. Key mappings:
    
    | Bash Variable | Python Config Attribute |
    |---------------|------------------------|
    | `UPS_NAME` | `UPS_NAME` |
    | `CHECK_INTERVAL` | `CHECK_INTERVAL` |
    | `LOW_BATTERY_THRESHOLD` | `LOW_BATTERY_THRESHOLD` |
    | `CRITICAL_DEPLETION_RATE` | `CRITICAL_DEPLETION_RATE` |
    | `CRITICAL_RUNTIME_THRESHOLD` | `CRITICAL_RUNTIME_THRESHOLD` |
    | `DEPLETION_RATE_GRACE_PERIOD` | `DEPLETION_RATE_GRACE_PERIOD` |
    | `DISCORD_WEBHOOK_URL` | `DISCORD_WEBHOOK_URL` |
    | `DRY_RUN_MODE` | `DRY_RUN_MODE` |
    | `MAX_VM_WAIT` | `MAX_VM_WAIT` |
    | `EXTENDED_TIME` | `EXTENDED_TIME` |
    | `REMOTE_NAS_USER` | `REMOTE_NAS_USER` |
    | `REMOTE_NAS_HOST` | `REMOTE_NAS_HOST` |
    | `MOUNTS_TO_UNMOUNT` | `MOUNTS_TO_UNMOUNT` (different format) |

6. **Enable and start:**
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable ups-monitor.service
    sudo systemctl start ups-monitor.service
    ```

7. **Optional cleanup:**
    ```bash
    sudo rm /usr/local/bin/ups-monitor.sh
    # Remove no-longer-needed dependencies
    sudo dnf remove jq bc  # or apt remove
    ```

### Upgrading from Version 1.0 (Original Bash)

If upgrading from Version 1.0, you'll need to:

1. **Follow all steps above for upgrading from 2.0**

2. **Set up SSH key authentication** (Version 1.0 used passwords):
    - Follow Section 2 (Configure Remote NAS) completely
    - Remove `sshpass` if installed: `sudo dnf remove sshpass`

3. **Update NAS sudo configuration:**
    - Version 1.0 used generic `shutdown` command
    - Version 3.0 uses Synology-specific `synoshutdown`
    - Follow Section 2b to configure the new sudoers rule

4. **Review new configuration options:**
    - `DEPLETION_RATE_GRACE_PERIOD` (new in 2.0)
    - `MAX_STALE_DATA_TOLERANCE` (new in 2.0)
    - `UNMOUNT_TIMEOUT` (new in 2.0)
    - `MOUNTS_TO_UNMOUNT` with options (new in 2.0)

5. **Discord webhook setup** (not available in 1.0):
    - Create a Discord webhook in your server
    - Add the URL to `DISCORD_WEBHOOK_URL` in the config

---

## Troubleshooting

### Service Fails to Start

Check the logs for error messages:

```bash
journalctl -u ups-monitor.service -e
```

Common issues:

```bash
# Check Python version (must be 3.9+)
python3 --version

# Check if requests is installed
python3 -c "import requests; print('OK')"

# Check for syntax errors
python3 -m py_compile /opt/ups-monitor/ups-monitor.py

# Check if upsc is available
which upsc
```

### Cannot Connect to UPS

If the logs show connection errors (`ERROR: Cannot connect to UPS...`):

```bash
# Test basic connectivity from the monitoring server
upsc UPS@192.168.178.11
```

If this fails, check the NUT server configuration, firewalls, and network connectivity. If you see "Data stale" errors frequently, this often indicates communication issues between the NUT server and the UPS hardware (e.g., bad USB cable).

### Discord Notifications Not Working

```bash
# Test the webhook manually
curl -H "Content-Type: application/json" \
     -d '{"content": "Test message"}' \
     "YOUR_WEBHOOK_URL"

# Check if requests can reach Discord
python3 -c "import requests; r = requests.get('https://discord.com'); print(r.status_code)"
```

### Remote NAS Shutdown Fails

Verify the SSH key and `sudoers` configuration as detailed in Section 2. Ensure the script is running as the user who owns the SSH key (`root`).

```bash
# Test the SSH command manually as root
sudo su
ssh nas-admin@192.168.178.229 "sudo -i synoshutdown -s"
```

---

## Security Considerations

### SSH Host Key Verification (`StrictHostKeyChecking=no`)

The script currently includes `-o StrictHostKeyChecking=no` in the SSH command.

**WARNING:** This disables Host Key Verification and makes the connection vulnerable to Man-in-the-Middle (MitM) attacks.

**Recommendation:** It is strongly recommended to enable host key verification.

1. As `root`, manually SSH to the NAS *once* to accept the host key:
    ```bash
    sudo ssh nas-admin@192.168.178.229
    ```
    Type "yes" when prompted.

2. Edit the script (`/opt/ups-monitor/ups-monitor.py`) and modify the `shutdown_remote_nas` function to remove the `-o StrictHostKeyChecking=no` option:
    ```python
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        config.REMOTE_NAS_USER + "@" + config.REMOTE_NAS_HOST,
        "sudo -i synoshutdown -s"
    ]
    ```

### Password Storage (Version 1.0 Issue)

Version 1.0 stored the NAS password directly in the script using `sshpass`. This was a significant security risk as:
- Passwords were visible in plaintext
- Process listings could expose the password
- Script backups could leak credentials

Versions 2.0 and 3.0 use SSH key authentication, eliminating password storage entirely.

### Running as Root

The script requires root privileges for:
- Shutting down the system
- Managing VMs (virsh)
- Managing Docker containers
- Accessing SSH keys
- Unmounting filesystems

The systemd service includes basic security hardening (`NoNewPrivileges=true`, `PrivateTmp=true`) to limit potential damage if the script were compromised.

---

## File Locations

| File | Purpose |
|------|---------|
| `/opt/ups-monitor/ups-monitor.py` | Main script |
| `/etc/systemd/system/ups-monitor.service` | Systemd service file |
| `/var/log/ups-monitor.log` | Detailed log file |
| `/var/run/ups-monitor.state` | Current UPS state snapshot |
| `/var/run/ups-shutdown-scheduled` | Shutdown sequence flag file |
| `/var/run/ups-battery-history` | Battery depletion history |

---

## Configuration Reference

### Complete Config Class Options

```python
@dataclass
class Config:
    # UPS Configuration
    UPS_NAME: str = "UPS@192.168.178.11"
    CHECK_INTERVAL: int = 1  # seconds
    LOW_BATTERY_THRESHOLD: int = 20  # percentage
    BATTERY_DEPLETION_WINDOW: int = 300  # seconds (5 minutes)
    CRITICAL_DEPLETION_RATE: float = 15.0  # %/minute
    DEPLETION_RATE_GRACE_PERIOD: int = 90  # seconds
    CRITICAL_RUNTIME_THRESHOLD: int = 600  # seconds (10 minutes)
    MAX_STALE_DATA_TOLERANCE: int = 3  # attempts before connection lost
    
    # System Paths
    LOG_FILE: Path = Path("/var/log/ups-monitor.log")
    STATE_FILE: Path = Path("/var/run/ups-monitor.state")
    SHUTDOWN_SCHEDULED_FILE: Path = Path("/var/run/ups-shutdown-scheduled")
    BATTERY_HISTORY_FILE: Path = Path("/var/run/ups-battery-history")
    
    # Notification Configuration
    DISCORD_WEBHOOK_URL: str = ""
    NOTIFICATION_TIMEOUT: int = 3  # seconds (non-blocking)
    NOTIFICATION_TIMEOUT_BLOCKING: int = 10  # seconds (during shutdown)
    
    # Discord Embed Colors (Decimal)
    COLOR_RED: int = 15158332
    COLOR_GREEN: int = 3066993
    COLOR_YELLOW: int = 15844367
    COLOR_ORANGE: int = 15105570
    COLOR_BLUE: int = 3447003
    
    # Shutdown Behavior
    DRY_RUN_MODE: bool = False
    MAX_VM_WAIT: int = 30  # seconds
    EXTENDED_TIME: int = 900  # seconds (15 minutes)
    EXTENDED_TIME_ON_BATTERY_SHUTDOWN: bool = True
    
    # Remote NAS Configuration
    REMOTE_NAS_USER: str = "nas-admin"
    REMOTE_NAS_HOST: str = "192.168.178.229"
    
    # Unmount Configuration
    UNMOUNT_TIMEOUT: int = 15  # seconds
    MOUNTS_TO_UNMOUNT: List[Tuple[str, str]] = [
        ("/mnt/media", ""),
        ("/mnt/nas", "-l"),
        ("/mnt/backup", ""),
    ]
```
