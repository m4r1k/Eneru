#!/bin/bash
# Post-removal script for Eneru package
# Called after package files are removed
#
# RPM: $1 = 0 (removal complete), $1 = 1+ (upgrade - old removed, new installed)
# DEB: $1 = "remove", "purge", or "upgrade"
set -e

# Detect if this is a removal or upgrade
is_removal=true

if [ -n "$1" ]; then
    if [ "$1" = "upgrade" ]; then
        # DEB: upgrade
        is_removal=false
    elif [ "$1" -ge 1 ] 2>/dev/null; then
        # RPM: $1 >= 1 means upgrade
        is_removal=false
    elif [ "$1" = "0" ]; then
        # RPM: $1 = 0 means actual removal
        is_removal=true
    fi
fi

# Always reload systemd
systemctl daemon-reload

if [ "$is_removal" = true ]; then
    echo ""
    echo "Eneru has been removed."
    echo ""
    echo "Note: Configuration files in /etc/ups-monitor/ have been preserved."
    echo "To remove them completely: rm -rf /etc/ups-monitor/"
    echo ""
fi
# UPGRADE: Silent - no message needed
