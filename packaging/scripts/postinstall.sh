#!/bin/bash
# Post-installation script for Eneru package
# Called after package files are installed
#
# RPM: $1 = 1 (install), $1 = 2+ (upgrade)
# DEB: $1 = "configure"
set -e

# Reload systemd to pick up the new/updated service file
systemctl daemon-reload

# Detect if this is an upgrade or fresh install
is_upgrade=false
was_running=false
was_enabled=false

# RPM passes a number, DEB passes action name
if [ -n "$1" ]; then
    if [ "$1" = "configure" ]; then
        # DEB upgrade detection: check if service existed before
        if systemctl list-unit-files eneru.service &>/dev/null; then
            is_upgrade=true
        fi
    elif [ "$1" -ge 2 ] 2>/dev/null; then
        # RPM: $1 >= 2 means upgrade
        is_upgrade=true
    fi
fi

# Check current service state (before we potentially restart)
if systemctl is-active --quiet eneru.service 2>/dev/null; then
    was_running=true
fi
if systemctl is-enabled --quiet eneru.service 2>/dev/null; then
    was_enabled=true
fi

if [ "$is_upgrade" = true ]; then
    # UPGRADE: Restart service if it was running, otherwise leave it alone
    if [ "$was_running" = true ]; then
        echo "Restarting ups-monitor service..."
        systemctl restart eneru.service
    fi
    # Silent upgrade - no instructions needed
else
    # FRESH INSTALL: Show instructions (don't enable or start)
    echo ""
    echo "=============================================="
    echo "  Eneru has been installed successfully!"
    echo "=============================================="
    echo ""
    echo "Next steps:"
    echo "  1. Edit configuration: nano /etc/ups-monitor/config.yaml"
    echo "  2. Validate config:    python3 /opt/ups-monitor/ups_monitor.py --validate-config"
    echo "  3. Enable the service: systemctl enable eneru.service"
    echo "  4. Start the service:  systemctl start eneru.service"
    echo ""
fi
