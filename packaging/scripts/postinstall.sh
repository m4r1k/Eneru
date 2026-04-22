#!/bin/bash
# Post-installation script for Eneru package
# Called after package files are installed
#
# RPM: $1 = 1 (install), $1 = 2+ (upgrade)
# DEB: $1 = "configure"; $2 = previous version (empty on fresh install)
set -e

# Reload systemd to pick up the new/updated service file. Skip in
# environments without a live systemd (chroot, container build layer):
# `systemctl daemon-reload` aborts under `set -e` and leaves the package
# half-applied if pid 1 isn't systemd.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

# Detect whether this invocation is an upgrade or a fresh install.
is_upgrade=false
was_running=false

# RPM passes a number, DEB passes the action name plus the previous
# version string (when relevant).
if [ -n "$1" ]; then
    if [ "$1" = "configure" ]; then
        # DEB: postinst runs with `configure` for BOTH a fresh install
        # AND an upgrade. The disambiguator is $2: empty on a fresh
        # install, populated with the previous package version on an
        # upgrade. (Listing the unit file is not sufficient — the unit
        # file is part of the new package and is always present here.)
        if [ -n "$2" ]; then
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

if [ "$is_upgrade" = true ]; then
    # UPGRADE: Restart service if it was running, otherwise leave it alone
    if [ "$was_running" = true ]; then
        echo "Restarting Eneru service..."
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
    echo "  2. Validate config:    python3 /opt/ups-monitor/eneru.py --validate-config"
    echo "  3. Enable the service: systemctl enable eneru.service"
    echo "  4. Start the service:  systemctl start eneru.service"
    echo ""
fi
