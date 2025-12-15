#!/usr/bin/env python3
"""
UPS Monitor Script for NUT Server
Monitors UPS status and triggers safe shutdown on low battery.
"""

import subprocess
import sys
import os
import time
import signal
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from collections import deque
from dataclasses import dataclass, field
import threading
import requests

# ==============================================================================
# CONFIGURATION
# ==============================================================================

@dataclass
class Config:
    """Configuration settings for the UPS monitor."""
    
    # UPS Configuration
    UPS_NAME: str = "UPS@192.168.178.11"
    CHECK_INTERVAL: int = 1
    LOW_BATTERY_THRESHOLD: int = 20
    BATTERY_DEPLETION_WINDOW: int = 300
    CRITICAL_DEPLETION_RATE: float = 15.0
    DEPLETION_RATE_GRACE_PERIOD: int = 90
    CRITICAL_RUNTIME_THRESHOLD: int = 600
    MAX_STALE_DATA_TOLERANCE: int = 3
    
    # System Paths
    LOG_FILE: Path = field(default_factory=lambda: Path("/var/log/ups-monitor.log"))
    STATE_FILE: Path = field(default_factory=lambda: Path("/var/run/ups-monitor.state"))
    SHUTDOWN_SCHEDULED_FILE: Path = field(default_factory=lambda: Path("/var/run/ups-shutdown-scheduled"))
    BATTERY_HISTORY_FILE: Path = field(default_factory=lambda: Path("/var/run/ups-battery-history"))
    
    # Notification Configuration
    DISCORD_WEBHOOK_URL: str = "https://discord.com/api/webhooks/..."  # Your webhook

    NOTIFICATION_TIMEOUT: int = 3
    NOTIFICATION_TIMEOUT_BLOCKING: int = 10
    
    # Color Codes for Discord embeds (Decimal)
    COLOR_RED: int = 15158332
    COLOR_GREEN: int = 3066993
    COLOR_YELLOW: int = 15844367
    COLOR_ORANGE: int = 15105570
    COLOR_BLUE: int = 3447003
    
    # Shutdown Behavior
    DRY_RUN_MODE: bool = False
    MAX_VM_WAIT: int = 30
    
    # Extended time on battery (Safety Net)
    EXTENDED_TIME: int = 15 * 60
    EXTENDED_TIME_ON_BATTERY_SHUTDOWN: bool = True
    
    # Remote NAS configuration
    REMOTE_NAS_USER: str = "nas-admin"
    REMOTE_NAS_HOST: str = "192.168.178.229"
    
    # Unmount Configuration
    UNMOUNT_TIMEOUT: int = 15
    MOUNTS_TO_UNMOUNT: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("/mnt/media", ""),
        ("/mnt/nas", "-l"),
        ("/mnt/backup", ""),
    ])


config = Config()


# ==============================================================================
# STATE TRACKING
# ==============================================================================

@dataclass
class MonitorState:
    """Tracks the current state of the UPS monitor."""
    previous_status: str = ""
    on_battery_start_time: int = 0
    extended_time_logged: bool = False
    voltage_state: str = "NORMAL"
    avr_state: str = "INACTIVE"
    bypass_state: str = "INACTIVE"
    overload_state: str = "INACTIVE"
    connection_state: str = "OK"
    stale_data_count: int = 0
    voltage_warning_low: float = 0.0
    voltage_warning_high: float = 0.0
    nominal_voltage: float = 230.0
    battery_history: deque = field(default_factory=lambda: deque(maxlen=1000))


state = MonitorState()


# ==============================================================================
# LOGGING SETUP
# ==============================================================================

class TimezoneFormatter(logging.Formatter):
    """Custom formatter that includes timezone abbreviation."""
    
    def format(self, record):
        record.timezone = time.strftime('%Z')
        return super().format(record)


class UPSLogger:
    """Custom logger that handles both file and console output."""
    
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.logger = logging.getLogger("ups-monitor")
        self.logger.setLevel(logging.INFO)
        
        if self.logger.handlers:
            self.logger.handlers.clear()
        
        formatter = TimezoneFormatter(
            '%(asctime)s %(timezone)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except PermissionError:
            print("Warning: Cannot write to " + str(log_file) + ", logging to console only")
    
    def log(self, message: str):
        """Log a message with timezone info."""
        self.logger.info(message)
        
        if config.SHUTDOWN_SCHEDULED_FILE.exists():
            discord_safe_message = message.replace('`', '\\`')
            notification_text = "ℹ️ **Shutdown Detail:** " + discord_safe_message
            send_notification(notification_text, config.COLOR_BLUE)


ups_logger: Optional[UPSLogger] = None


def log_message(message: str):
    """Log a message using the global logger."""
    if ups_logger:
        ups_logger.log(message)
    else:
        tz_name = time.strftime('%Z')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(timestamp + " " + tz_name + " - " + message)


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False
    return False


def run_command(
    cmd: List[str],
    timeout: int = 30,
    capture_output: bool = True
) -> Tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_NUMERIC': 'C'}
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except FileNotFoundError:
        return 127, "", "Command not found: " + cmd[0]
    except Exception as e:
        return 1, "", str(e)


def command_exists(cmd: str) -> bool:
    """Check if a command exists in the system PATH."""
    exit_code, _, _ = run_command(["which", cmd])
    return exit_code == 0


def format_seconds(seconds: Any) -> str:
    """Format seconds into a human-readable string."""
    if not is_numeric(seconds):
        return "N/A"
    seconds = int(float(seconds))
    if seconds < 60:
        return str(seconds) + "s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return str(mins) + "m " + str(secs) + "s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return str(hours) + "h " + str(minutes) + "m"


# ==============================================================================
# NOTIFICATION FUNCTIONS
# ==============================================================================

def send_notification(message: str, color: Optional[int] = None):
    """Send a Discord notification."""
    if color is None:
        color = config.COLOR_YELLOW
    
    if not is_numeric(color):
        color = config.COLOR_YELLOW
    
    if not config.DISCORD_WEBHOOK_URL:
        return
    
    payload = {
        "embeds": [{
            "title": "UPS Monitor Alert",
            "description": message,
            "color": int(color),
            "footer": {
                "text": "UPS: " + config.UPS_NAME
            },
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        }]
    }
    
    is_shutdown = config.SHUTDOWN_SCHEDULED_FILE.exists()
    timeout_val = config.NOTIFICATION_TIMEOUT_BLOCKING if is_shutdown else config.NOTIFICATION_TIMEOUT
    
    def send_request():
        try:
            requests.post(
                config.DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=timeout_val,
                headers={"Content-Type": "application/json"}
            )
            if is_shutdown:
                time.sleep(0.5)
        except Exception:
            pass
    
    if is_shutdown:
        send_request()
    else:
        thread = threading.Thread(target=send_request, daemon=True)
        thread.start()


def log_power_event(event: str, details: str):
    """Log power events with centralized notification logic."""
    log_message("⚡ POWER EVENT: " + event + " - " + details)
    
    try:
        run_command([
            "logger", "-t", "ups-monitor", "-p", "daemon.warning",
            "⚡ POWER EVENT: " + event + " - " + details
        ])
    except Exception:
        pass
    
    if config.SHUTDOWN_SCHEDULED_FILE.exists():
        return
    
    discord_message: Optional[str] = None
    discord_color = config.COLOR_YELLOW
    
    if event == "ON_BATTERY":
        discord_message = (
            "⚠️ **POWER FAILURE DETECTED!**\n"
            "System running on battery.\n"
            "Details: " + details
        )
        discord_color = config.COLOR_ORANGE
    
    elif event == "POWER_RESTORED":
        discord_message = (
            "✅ **POWER RESTORED.**\n"
            "System back on line power/charging.\n"
            "Details: " + details
        )
        discord_color = config.COLOR_GREEN
    
    elif event in ("BROWNOUT_DETECTED", "OVER_VOLTAGE_DETECTED"):
        discord_message = (
            "⚠️ **VOLTAGE ISSUE:** " + event + "\n"
            "Details: " + details
        )
        discord_color = config.COLOR_ORANGE
    
    elif event == "VOLTAGE_NORMALIZED":
        return
    
    elif event in ("AVR_BOOST_ACTIVE", "AVR_TRIM_ACTIVE"):
        discord_message = (
            "⚡ **AVR ACTIVE:** " + event + "\n"
            "Details: " + details
        )
        discord_color = config.COLOR_YELLOW
    
    elif event == "AVR_INACTIVE":
        return
    
    elif event == "BYPASS_MODE_ACTIVE":
        discord_message = (
            "🚨 **UPS IN BYPASS MODE!**\n"
            "No protection active!\n"
            "Details: " + details
        )
        discord_color = config.COLOR_RED
    
    elif event == "BYPASS_MODE_INACTIVE":
        discord_message = (
            "✅ **Bypass Mode Inactive.**\n"
            "Protection restored.\n"
            "Details: " + details
        )
        discord_color = config.COLOR_GREEN
    
    elif event == "OVERLOAD_ACTIVE":
        discord_message = (
            "🚨 **UPS OVERLOAD DETECTED!**\n"
            "Details: " + details
        )
        discord_color = config.COLOR_RED
    
    elif event == "OVERLOAD_RESOLVED":
        discord_message = (
            "✅ **Overload Resolved.**\n"
            "Details: " + details
        )
        discord_color = config.COLOR_GREEN
    
    elif event == "CONNECTION_LOST":
        discord_message = (
            "❌ **ERROR: Connection Lost**\n" + details
        )
        discord_color = config.COLOR_RED
    
    elif event == "CONNECTION_RESTORED":
        discord_message = (
            "✅ **Connection Restored.**\n" + details
        )
        discord_color = config.COLOR_GREEN
    
    else:
        discord_message = (
            "⚡ **Event:** " + event + "\n"
            "Details: " + details
        )
    
    if discord_message:
        send_notification(discord_message, discord_color)


# ==============================================================================
# UPS INTERFACE FUNCTIONS
# ==============================================================================

def get_ups_var(var_name: str) -> Optional[str]:
    """Get a single UPS variable using upsc."""
    exit_code, stdout, _ = run_command(["upsc", config.UPS_NAME, var_name])
    if exit_code == 0:
        return stdout.strip()
    return None


def get_all_ups_data() -> Tuple[bool, Dict[str, str], str]:
    """Query all UPS data using a single upsc call."""
    exit_code, stdout, stderr = run_command(["upsc", config.UPS_NAME])
    
    if exit_code != 0:
        return False, {}, stderr
    
    if "Data stale" in stdout or "Data stale" in stderr:
        return False, {}, "Data stale"
    
    ups_data: Dict[str, str] = {}
    for line in stdout.strip().split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            ups_data[key.strip()] = value.strip()
    
    return True, ups_data, ""


# ==============================================================================
# BATTERY DEPLETION TRACKING
# ==============================================================================

def calculate_depletion_rate(current_battery: str) -> float:
    """Calculate battery depletion rate based on history."""
    current_time = int(time.time())
    
    if not is_numeric(current_battery):
        return 0.0
    
    current_battery_float = float(current_battery)
    cutoff_time = current_time - config.BATTERY_DEPLETION_WINDOW
    
    state.battery_history = deque(
        [(ts, bat) for ts, bat in state.battery_history if ts >= cutoff_time],
        maxlen=1000
    )
    state.battery_history.append((current_time, current_battery_float))
    
    try:
        temp_file = config.BATTERY_HISTORY_FILE.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            for ts, bat in state.battery_history:
                f.write(str(ts) + ":" + str(bat) + "\n")
        temp_file.replace(config.BATTERY_HISTORY_FILE)
    except Exception:
        pass
    
    if len(state.battery_history) < 30:
        return 0.0
    
    oldest_time, oldest_battery = state.battery_history[0]
    time_diff = current_time - oldest_time
    
    if time_diff > 0:
        battery_diff = oldest_battery - current_battery_float
        rate = (battery_diff / time_diff) * 60
        return round(rate, 2)
    
    return 0.0


# ==============================================================================
# SHUTDOWN SEQUENCE
# ==============================================================================

def shutdown_vms():
    """Shutdown all libvirt virtual machines."""
    log_message("🖥️ Shutting down all libvirt virtual machines...")
    
    if not command_exists("virsh"):
        log_message(" ℹ️ virsh not available, skipping VM shutdown")
        return
    
    exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
    if exit_code != 0:
        log_message(" ⚠️ Failed to get VM list")
        return
    
    running_vms = [vm.strip() for vm in stdout.strip().split('\n') if vm.strip()]
    
    if not running_vms:
        log_message(" ℹ️ No running VMs found")
        return
    
    for vm in running_vms:
        log_message(" ⏹️ Shutting down VM: " + vm)
        if config.DRY_RUN_MODE:
            log_message(" 🧪 [DRY-RUN] Would shutdown VM: " + vm)
        else:
            exit_code, stdout, stderr = run_command(["virsh", "shutdown", vm])
            if stdout.strip():
                log_message("    " + stdout.strip())
    
    if config.DRY_RUN_MODE:
        return
    
    log_message(" ⏳ Waiting up to " + str(config.MAX_VM_WAIT) + "s for VMs to shutdown gracefully...")
    wait_interval = 5
    time_waited = 0
    remaining_vms: List[str] = []
    
    while time_waited < config.MAX_VM_WAIT:
        exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
        still_running = set(vm.strip() for vm in stdout.strip().split('\n') if vm.strip())
        remaining_vms = [vm for vm in running_vms if vm in still_running]
        
        if not remaining_vms:
            log_message(" ✅ All VMs stopped gracefully after " + str(time_waited) + "s.")
            break
        
        log_message("  🕒 Still waiting for: " + " ".join(remaining_vms) + " (Waited " + str(time_waited) + "s)")
        time.sleep(wait_interval)
        time_waited += wait_interval
    
    if remaining_vms:
        log_message(" ⚠️ Timeout reached. Force destroying remaining VMs.")
        for vm in remaining_vms:
            log_message(" ⚡ Force destroying VM: " + vm)
            run_command(["virsh", "destroy", vm])
    
    log_message(" ✅ All VMs shutdown complete")


def shutdown_docker_containers():
    """Stop all Docker containers."""
    log_message("🐋 Stopping all Docker containers...")
    
    if not command_exists("docker"):
        log_message(" ℹ️ docker not available, skipping container shutdown")
        return
    
    exit_code, stdout, _ = run_command(["docker", "ps", "-q"])
    if exit_code != 0:
        log_message(" ⚠️ Failed to get container list")
        return
    
    container_ids = [cid.strip() for cid in stdout.strip().split('\n') if cid.strip()]
    
    if not container_ids:
        log_message(" ℹ️ No running Docker containers found")
        return
    
    if config.DRY_RUN_MODE:
        exit_code, stdout, _ = run_command(["docker", "ps", "--format", "{{.Names}}"])
        names = stdout.strip().replace('\n', ' ')
        log_message(" 🧪 [DRY-RUN] Would stop Docker containers: " + names)
    else:
        run_command(["docker", "stop"] + container_ids, timeout=60)
        log_message(" ✅ Docker containers stopped")


def sync_filesystems():
    """Sync all filesystems."""
    log_message("💾 Syncing all filesystems...")
    if config.DRY_RUN_MODE:
        log_message(" 🧪 [DRY-RUN] Would sync filesystems")
    else:
        os.sync()
        log_message(" ✅ Filesystems synced")


def unmount_filesystems():
    """Unmount configured filesystems."""
    log_message("📤 Unmounting filesystems (Max wait: " + str(config.UNMOUNT_TIMEOUT) + "s)...")
    
    for mount_spec in config.MOUNTS_TO_UNMOUNT:
        if isinstance(mount_spec, tuple) and len(mount_spec) == 2:
            mount_point, options = mount_spec
        else:
            mount_point = str(mount_spec)
            options = ""
        
        options_display = (" " + options) if options else ""
        log_message(" ➡️ Unmounting " + mount_point + options_display)
        
        if config.DRY_RUN_MODE:
            log_message(
                "  🧪 [DRY-RUN] Would execute: timeout " + str(config.UNMOUNT_TIMEOUT) +
                "s umount " + options + " " + mount_point
            )
            continue
        
        cmd = ["umount"]
        if options:
            cmd.append(options)
        cmd.append(mount_point)
        
        exit_code, _, stderr = run_command(cmd, timeout=config.UNMOUNT_TIMEOUT)
        
        if exit_code == 0:
            log_message("  ✅ " + mount_point + " unmounted successfully")
        elif exit_code == 124:
            log_message(
                "  ⚠️ " + mount_point + " unmount timed out "
                "(device may be busy/unreachable). Proceeding anyway."
            )
        else:
            check_code, _, _ = run_command(["mountpoint", "-q", mount_point])
            if check_code == 0:
                log_message(
                    "  ❌ Failed to unmount " + mount_point +
                    " (Error code " + str(exit_code) + "). Proceeding anyway."
                )
            else:
                log_message("  ℹ️ " + mount_point + " was likely not mounted.")


def shutdown_remote_nas():
    """Shutdown the remote NAS via SSH."""
    log_message("🌐 Initiating remote NAS shutdown at " + config.REMOTE_NAS_HOST + "...")
    
    if config.DRY_RUN_MODE:
        log_message(
            " 🧪 [DRY-RUN] Would send shutdown command (using sudo -i) to " +
            config.REMOTE_NAS_USER + "@" + config.REMOTE_NAS_HOST
        )
        return
    
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        config.REMOTE_NAS_USER + "@" + config.REMOTE_NAS_HOST,
        "sudo -i synoshutdown -s"
    ]
    
    exit_code, stdout, stderr = run_command(ssh_cmd, timeout=30)
    
    if exit_code == 0:
        log_message(" ✅ Remote NAS shutdown command sent successfully")
    else:
        log_message(
            " ❌ WARNING: Failed to execute shutdown command on remote NAS "
            "(Error code " + str(exit_code) + ")"
        )
        if stderr.strip():
            log_message("    Error: " + stderr.strip())


def execute_shutdown_sequence():
    """Execute the controlled shutdown sequence."""
    config.SHUTDOWN_SCHEDULED_FILE.touch()
    
    log_message("🚨 ========== INITIATING EMERGENCY SHUTDOWN SEQUENCE ==========")
    
    if config.DRY_RUN_MODE:
        log_message("🧪 *** DRY-RUN MODE: No actual shutdown will occur ***")
    
    wall_msg = "🚨 CRITICAL: Executing emergency UPS shutdown sequence NOW!"
    if config.DRY_RUN_MODE:
        wall_msg = "[DRY-RUN] " + wall_msg
    
    run_command(["wall", wall_msg])
    
    shutdown_vms()
    shutdown_docker_containers()
    sync_filesystems()
    unmount_filesystems()
    shutdown_remote_nas()
    
    log_message("💾 Final filesystem sync...")
    if config.DRY_RUN_MODE:
        log_message(" 🧪 [DRY-RUN] Would perform final sync")
    else:
        os.sync()
        log_message(" ✅ Final sync complete")
    
    log_message("🔌 Shutting down local server NOW")
    log_message("✅ ========== SHUTDOWN SEQUENCE COMPLETE ==========")
    
    if config.DRY_RUN_MODE:
        log_message("🧪 [DRY-RUN] Would execute: shutdown -h now")
        log_message("🧪 [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)")
        config.SHUTDOWN_SCHEDULED_FILE.unlink(missing_ok=True)
    else:
        send_notification(
            "🛑 **Shutdown Sequence Complete.**\nShutting down local server NOW.",
            config.COLOR_RED
        )
        run_command(["shutdown", "-h", "now", "UPS battery critical - emergency shutdown"])


def trigger_immediate_shutdown(reason: str):
    """Trigger an immediate shutdown if not already in progress."""
    if config.SHUTDOWN_SCHEDULED_FILE.exists():
        return
    
    config.SHUTDOWN_SCHEDULED_FILE.touch()
    
    send_notification(
        "🚨 **EMERGENCY SHUTDOWN INITIATED!**\n"
        "Reason: " + reason + "\n"
        "Executing shutdown tasks (VMs, Docker, NAS).",
        config.COLOR_RED
    )
    
    log_message("🚨 CRITICAL: Triggering immediate shutdown. Reason: " + reason)
    run_command([
        "wall",
        "🚨 CRITICAL: UPS battery critical! Immediate shutdown initiated! Reason: " + reason
    ])
    
    execute_shutdown_sequence()


# ==============================================================================
# SIGNAL HANDLERS
# ==============================================================================

def cleanup_and_exit(signum: int, frame):
    """Handle clean exit on signals (e.g., systemctl stop)."""
    if config.SHUTDOWN_SCHEDULED_FILE.exists():
        sys.exit(0)
    
    config.SHUTDOWN_SCHEDULED_FILE.touch()
    
    log_message("🛑 Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")
    send_notification(
        "🛑 **UPS Monitor Service Stopped.**\nMonitoring is now inactive.",
        config.COLOR_ORANGE
    )
    
    config.SHUTDOWN_SCHEDULED_FILE.unlink(missing_ok=True)
    sys.exit(0)


# ==============================================================================
# STARTUP AND INITIALIZATION
# ==============================================================================

def check_dependencies():
    """Check for required and optional dependencies."""
    required_cmds = [
        "upsc", "timeout", "sync", "shutdown", "mountpoint",
        "logger", "ssh", "wall", "curl"
    ]
    
    missing = []
    for cmd in required_cmds:
        if not command_exists(cmd):
            missing.append(cmd)
    
    if missing:
        error_msg = "❌ FATAL ERROR: Missing required commands: " + ", ".join(missing)
        print(error_msg)
        config.SHUTDOWN_SCHEDULED_FILE.touch()
        send_notification(
            "❌ **FATAL ERROR:** Missing dependencies: " + ", ".join(missing) +
            ". Script cannot start.",
            config.COLOR_RED
        )
        config.SHUTDOWN_SCHEDULED_FILE.unlink(missing_ok=True)
        sys.exit(1)
    
    optional_cmds = ["virsh", "docker"]
    for cmd in optional_cmds:
        if not command_exists(cmd):
            log_message(
                "⚠️ WARNING: Optional command '" + cmd + "' not found. "
                "Related shutdown tasks will be skipped."
            )
            send_notification(
                "⚠️ **Warning: Missing Optional Dependency**\n"
                "Command '" + cmd + "' not found.\n"
                "Related shutdown tasks (VMs/Containers) will be skipped during an emergency.",
                config.COLOR_YELLOW
            )


def initialize_voltage_thresholds():
    """Initialize voltage thresholds dynamically from UPS data."""
    nominal = get_ups_var("input.voltage.nominal")
    low_transfer = get_ups_var("input.transfer.low")
    high_transfer = get_ups_var("input.transfer.high")
    
    if is_numeric(nominal):
        state.nominal_voltage = float(nominal)
    else:
        state.nominal_voltage = 230.0
    
    if is_numeric(low_transfer):
        state.voltage_warning_low = float(low_transfer) + 5
    else:
        state.voltage_warning_low = state.nominal_voltage * 0.9
    
    if is_numeric(high_transfer):
        state.voltage_warning_high = float(high_transfer) - 5
    else:
        state.voltage_warning_high = state.nominal_voltage * 1.1
    
    log_message(
        "📊 Voltage Monitoring Active. Nominal: " + str(state.nominal_voltage) + "V. " +
        "Low Warning: " + str(state.voltage_warning_low) + "V. " +
        "High Warning: " + str(state.voltage_warning_high) + "V."
    )


def wait_for_initial_connection():
    """Wait for initial connection to NUT server."""
    log_message("⏳ Checking initial connection to " + config.UPS_NAME + "...")
    
    max_wait = 30
    wait_interval = 5
    time_waited = 0
    connected = False
    
    while time_waited < max_wait:
        success, _, _ = get_all_ups_data()
        if success:
            connected = True
            log_message("✅ Initial connection successful.")
            break
        time.sleep(wait_interval)
        time_waited += wait_interval
    
    if not connected:
        log_message(
            "⚠️ WARNING: Failed to connect to " + config.UPS_NAME +
            " within " + str(max_wait) + "s. Proceeding, but voltage thresholds may default."
        )


def initialize():
    """Initialize the UPS monitor."""
    global ups_logger
    
    signal.signal(signal.SIGTERM, cleanup_and_exit)
    signal.signal(signal.SIGINT, cleanup_and_exit)
    
    ups_logger = UPSLogger(config.LOG_FILE)
    
    try:
        config.LOG_FILE.touch(exist_ok=True)
    except PermissionError:
        pass
    
    config.SHUTDOWN_SCHEDULED_FILE.unlink(missing_ok=True)
    
    try:
        config.BATTERY_HISTORY_FILE.write_text("")
    except PermissionError:
        log_message("⚠️ WARNING: Cannot write to " + str(config.BATTERY_HISTORY_FILE))
    
    check_dependencies()
    
    log_message("🚀 UPS Monitor starting - monitoring " + config.UPS_NAME + " using upsc")
    send_notification(
        "🚀 **UPS Monitor Service Started.**\nMonitoring " + config.UPS_NAME + ".",
        config.COLOR_BLUE
    )
    
    if config.DRY_RUN_MODE:
        log_message("🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")
    
    wait_for_initial_connection()
    initialize_voltage_thresholds()


# ==============================================================================
# MAIN MONITORING LOOP
# ==============================================================================

def save_state(ups_data: Dict[str, str]):
    """Save current UPS state to file."""
    state_content = (
        "STATUS=" + ups_data.get('ups.status', '') + "\n"
        "BATTERY=" + ups_data.get('battery.charge', '') + "\n"
        "RUNTIME=" + ups_data.get('battery.runtime', '') + "\n"
        "LOAD=" + ups_data.get('ups.load', '') + "\n"
        "INPUT_VOLTAGE=" + ups_data.get('input.voltage', '') + "\n"
        "OUTPUT_VOLTAGE=" + ups_data.get('output.voltage', '') + "\n"
        "TIMESTAMP=" + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + "\n"
    )
    try:
        temp_file = config.STATE_FILE.with_suffix('.tmp')
        temp_file.write_text(state_content)
        temp_file.replace(config.STATE_FILE)
    except Exception:
        pass


def check_voltage_issues(ups_status: str, input_voltage: str):
    """Check for voltage quality issues (brownout/over-voltage)."""
    if "OL" not in ups_status:
        if "OB" in ups_status or "FSD" in ups_status:
            state.voltage_state = "NORMAL"
        return
    
    if not is_numeric(input_voltage):
        return
    
    voltage = float(input_voltage)
    
    if voltage < state.voltage_warning_low:
        if state.voltage_state != "LOW":
            log_power_event(
                "BROWNOUT_DETECTED",
                "Voltage is low: " + str(voltage) + "V (Threshold: " +
                str(state.voltage_warning_low) + "V)"
            )
            state.voltage_state = "LOW"
    elif voltage > state.voltage_warning_high:
        if state.voltage_state != "HIGH":
            log_power_event(
                "OVER_VOLTAGE_DETECTED",
                "Voltage is high: " + str(voltage) + "V (Threshold: " +
                str(state.voltage_warning_high) + "V)"
            )
            state.voltage_state = "HIGH"
    elif state.voltage_state != "NORMAL":
        log_power_event(
            "VOLTAGE_NORMALIZED",
            "Voltage returned to normal: " + str(voltage) + "V. Previous state: " +
            state.voltage_state
        )
        state.voltage_state = "NORMAL"


def check_avr_status(ups_status: str, input_voltage: str):
    """Check for Automatic Voltage Regulation activity."""
    if is_numeric(input_voltage):
        voltage_str = str(input_voltage) + "V"
    else:
        voltage_str = "N/A"
    
    if "BOOST" in ups_status:
        if state.avr_state != "BOOST":
            log_power_event(
                "AVR_BOOST_ACTIVE",
                "Input voltage low (" + voltage_str + "). UPS is boosting output."
            )
            state.avr_state = "BOOST"
    elif "TRIM" in ups_status:
        if state.avr_state != "TRIM":
            log_power_event(
                "AVR_TRIM_ACTIVE",
                "Input voltage high (" + voltage_str + "). UPS is trimming output."
            )
            state.avr_state = "TRIM"
    elif state.avr_state != "INACTIVE":
        log_power_event("AVR_INACTIVE", "AVR is inactive. Input voltage: " + voltage_str + ".")
        state.avr_state = "INACTIVE"


def check_bypass_status(ups_status: str):
    """Check for bypass mode."""
    if "BYPASS" in ups_status:
        if state.bypass_state != "ACTIVE":
            log_power_event("BYPASS_MODE_ACTIVE", "UPS in bypass mode - no protection active!")
            state.bypass_state = "ACTIVE"
    elif state.bypass_state != "INACTIVE":
        log_power_event("BYPASS_MODE_INACTIVE", "UPS left bypass mode.")
        state.bypass_state = "INACTIVE"


def check_overload_status(ups_status: str, ups_load: str):
    """Check for overload condition."""
    if "OVER" in ups_status:
        if state.overload_state != "ACTIVE":
            log_power_event("OVERLOAD_ACTIVE", "UPS overload detected! Load: " + str(ups_load) + "%")
            state.overload_state = "ACTIVE"
    elif state.overload_state != "INACTIVE":
        if is_numeric(ups_load):
            reported_load = str(ups_load)
        else:
            reported_load = "N/A"
        log_power_event("OVERLOAD_RESOLVED", "UPS overload resolved. Load: " + reported_load + "%")
        state.overload_state = "INACTIVE"


def handle_on_battery(ups_data: Dict[str, str]):
    """Handle the On Battery state."""
    ups_status = ups_data.get('ups.status', '')
    battery_charge = ups_data.get('battery.charge', '')
    battery_runtime = ups_data.get('battery.runtime', '')
    ups_load = ups_data.get('ups.load', '')
    
    if "OB" not in state.previous_status and "FSD" not in state.previous_status:
        state.on_battery_start_time = int(time.time())
        state.extended_time_logged = False
        state.battery_history.clear()
        
        log_power_event(
            "ON_BATTERY",
            "Battery: " + str(battery_charge) + "%, Runtime: " +
            str(battery_runtime) + " seconds, Load: " + str(ups_load) + "%"
        )
        run_command([
            "wall",
            "⚠️ WARNING: Power failure detected! System running on UPS battery (" +
            str(battery_charge) + "% remaining, " + format_seconds(battery_runtime) + " runtime)"
        ])
    
    current_time = int(time.time())
    time_on_battery = current_time - state.on_battery_start_time
    
    depletion_rate = calculate_depletion_rate(battery_charge)
    
    shutdown_reason = ""
    
    # T1. Critical battery level
    if is_numeric(battery_charge):
        battery_int = int(float(battery_charge))
        if battery_int < config.LOW_BATTERY_THRESHOLD:
            shutdown_reason = (
                "Battery charge " + str(battery_charge) + "% below threshold " +
                str(config.LOW_BATTERY_THRESHOLD) + "%"
            )
    else:
        log_message("⚠️ WARNING: Received non-numeric battery charge value: '" + str(battery_charge) + "'")
    
    # T2. Critical runtime remaining
    if not shutdown_reason and is_numeric(battery_runtime):
        runtime_int = int(float(battery_runtime))
        if runtime_int < config.CRITICAL_RUNTIME_THRESHOLD:
            shutdown_reason = (
                "Runtime " + format_seconds(runtime_int) + " below threshold " +
                format_seconds(config.CRITICAL_RUNTIME_THRESHOLD)
            )
    
    # T3. Dangerous depletion rate (with grace period)
    if not shutdown_reason and is_numeric(depletion_rate) and depletion_rate > 0:
        if depletion_rate > config.CRITICAL_DEPLETION_RATE:
            if time_on_battery < config.DEPLETION_RATE_GRACE_PERIOD:
                log_message(
                    "🕒 INFO: High depletion rate (" + str(depletion_rate) + "%/min) ignored during " +
                    "grace period (" + str(time_on_battery) + "s/" +
                    str(config.DEPLETION_RATE_GRACE_PERIOD) + "s)."
                )
            else:
                shutdown_reason = (
                    "Depletion rate " + str(depletion_rate) + "%/min above threshold " +
                    str(config.CRITICAL_DEPLETION_RATE) + "%/min (after grace period)"
                )
    
    # T4. Extended time on battery
    if not shutdown_reason and time_on_battery > config.EXTENDED_TIME:
        if config.EXTENDED_TIME_ON_BATTERY_SHUTDOWN:
            shutdown_reason = (
                "Time on battery " + format_seconds(time_on_battery) + " exceeded " +
                "threshold " + format_seconds(config.EXTENDED_TIME)
            )
        elif not state.extended_time_logged:
            log_message(
                "⏳ INFO: System on battery for " + format_seconds(time_on_battery) +
                " exceeded threshold (" + format_seconds(config.EXTENDED_TIME) + ") - " +
                "extended time shutdown disabled"
            )
            state.extended_time_logged = True
    
    if shutdown_reason:
        trigger_immediate_shutdown(shutdown_reason)
    
    # Log status every 5 seconds
    if int(time.time()) % 5 == 0:
        log_message(
            "🔋 On battery: " + str(battery_charge) + "% (" +
            format_seconds(battery_runtime) + "), " +
            "Load: " + str(ups_load) + "%, Depletion: " + str(depletion_rate) + "%/min, " +
            "Time on battery: " + format_seconds(time_on_battery)
        )


def handle_on_line(ups_data: Dict[str, str]):
    """Handle the On Line / Charging state."""
    ups_status = ups_data.get('ups.status', '')
    battery_charge = ups_data.get('battery.charge', '')
    input_voltage = ups_data.get('input.voltage', '')
    
    if "OB" in state.previous_status or "FSD" in state.previous_status:
        time_on_battery = 0
        if state.on_battery_start_time > 0:
            time_on_battery = int(time.time()) - state.on_battery_start_time
        
        log_power_event(
            "POWER_RESTORED",
            "Battery: " + str(battery_charge) + "% (Status: " + ups_status + "), " +
            "Input: " + str(input_voltage) + "V, Outage duration: " +
            format_seconds(time_on_battery)
        )
        run_command([
            "wall",
            "✅ Power has been restored. UPS Status: " + ups_status +
            ". Battery at " + str(battery_charge) + "%."
        ])
        
        state.on_battery_start_time = 0
        state.extended_time_logged = False
        state.battery_history.clear()


def main_loop():
    """Main monitoring loop."""
    while True:
        success, ups_data, error_msg = get_all_ups_data()
        
        # ==========================================================================
        # CONNECTION HANDLING AND FAILSAFE
        # ==========================================================================
        
        if not success:
            is_failsafe_trigger = False
            
            if "Data stale" in error_msg:
                state.stale_data_count += 1
                if state.connection_state != "FAILED":
                    log_message(
                        "⚠️ WARNING: Data stale from UPS " + config.UPS_NAME +
                        " (Attempt " + str(state.stale_data_count) + "/" +
                        str(config.MAX_STALE_DATA_TOLERANCE) + ")."
                    )
                
                if state.stale_data_count >= config.MAX_STALE_DATA_TOLERANCE:
                    if state.connection_state != "FAILED":
                        log_power_event(
                            "CONNECTION_LOST",
                            "Data from UPS " + config.UPS_NAME + " is persistently stale " +
                            "(>= " + str(config.MAX_STALE_DATA_TOLERANCE) +
                            " attempts). Monitoring is inactive."
                        )
                        state.connection_state = "FAILED"
                    is_failsafe_trigger = True
            else:
                if state.connection_state != "FAILED":
                    log_message(
                        "❌ ERROR: Cannot connect to UPS " + config.UPS_NAME +
                        ". Output: " + error_msg
                    )
                state.stale_data_count = 0
                
                if state.connection_state != "FAILED":
                    log_power_event(
                        "CONNECTION_LOST",
                        "Cannot connect to UPS " + config.UPS_NAME +
                        " (Network, Server, or Config error). Monitoring is inactive."
                    )
                    state.connection_state = "FAILED"
                is_failsafe_trigger = True
            
            # FAILSAFE: If connection lost while on battery, shutdown immediately
            if is_failsafe_trigger and "OB" in state.previous_status:
                config.SHUTDOWN_SCHEDULED_FILE.touch()
                log_message(
                    "🚨 FAILSAFE TRIGGERED (FSB): Connection lost or data persistently stale " +
                    "while On Battery. Initiating emergency shutdown."
                )
                send_notification(
                    "🚨 **FAILSAFE (FSB) TRIGGERED!**\n"
                    "Connection to UPS lost or data stale while system was running On Battery.\n"
                    "Assuming critical failure. Executing immediate shutdown.",
                    config.COLOR_RED
                )
                execute_shutdown_sequence()
            
            time.sleep(5)
            continue
        
        # ==========================================================================
        # DATA PROCESSING
        # ==========================================================================
        
        state.stale_data_count = 0
        
        if state.connection_state == "FAILED":
            log_power_event(
                "CONNECTION_RESTORED",
                "Connection to UPS " + config.UPS_NAME + " restored. Monitoring is active."
            )
            state.connection_state = "OK"
        
        ups_status = ups_data.get('ups.status', '')
        
        if not ups_status:
            log_message(
                "❌ ERROR: Received data from UPS but 'ups.status' is missing. " +
                "Check NUT configuration."
            )
            time.sleep(5)
            continue
        
        save_state(ups_data)
        
        # Detect status changes
        if ups_status != state.previous_status and state.previous_status:
            battery_charge = ups_data.get('battery.charge', '')
            battery_runtime = ups_data.get('battery.runtime', '')
            ups_load = ups_data.get('ups.load', '')
            log_message(
                "🔄 Status changed: " + state.previous_status + " -> " + ups_status +
                " (Battery: " + str(battery_charge) + "%, Runtime: " +
                format_seconds(battery_runtime) + ", Load: " + str(ups_load) + "%)"
            )
        
        # ==========================================================================
        # POWER STATE ANALYSIS AND SHUTDOWN TRIGGERS
        # ==========================================================================
        
        if "FSD" in ups_status:
            trigger_immediate_shutdown("UPS signaled FSD (Forced Shutdown) flag.")
        
        elif "OB" in ups_status:
            handle_on_battery(ups_data)
        
        elif "OL" in ups_status or "CHRG" in ups_status:
            handle_on_line(ups_data)
        
        # ==========================================================================
        # ENVIRONMENT MONITORING
        # ==========================================================================
        
        input_voltage = ups_data.get('input.voltage', '')
        ups_load = ups_data.get('ups.load', '')
        
        check_voltage_issues(ups_status, input_voltage)
        check_avr_status(ups_status, input_voltage)
        check_bypass_status(ups_status)
        check_overload_status(ups_status, ups_load)
        
        state.previous_status = ups_status
        
        time.sleep(config.CHECK_INTERVAL)


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    """Main entry point."""
    try:
        initialize()
        main_loop()
    except KeyboardInterrupt:
        cleanup_and_exit(signal.SIGINT, None)
    except Exception as e:
        error_message = str(e)
        log_message("❌ FATAL ERROR: " + error_message)
        send_notification("❌ **FATAL ERROR:** " + error_message, config.COLOR_RED)
        raise


if __name__ == "__main__":
    main()
