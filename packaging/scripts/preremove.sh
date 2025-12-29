#!/bin/bash
# Pre-removal script for Eneru package
# Called before package files are removed
#
# RPM: $1 = 0 (removal), $1 = 1+ (upgrade - old package being removed)
# DEB: $1 = "remove" or "upgrade"
set -e

# Detect if this is a removal or upgrade
is_removal=true

if [ -n "$1" ]; then
    if [ "$1" = "upgrade" ]; then
        # DEB: upgrade means new version replacing old
        is_removal=false
    elif [ "$1" -ge 1 ] 2>/dev/null; then
        # RPM: $1 >= 1 means upgrade (packages remaining after this one removed)
        is_removal=false
    elif [ "$1" = "0" ]; then
        # RPM: $1 = 0 means actual removal
        is_removal=true
    fi
fi

if [ "$is_removal" = true ]; then
    # ACTUAL REMOVAL: Stop and disable the service
    if systemctl is-active --quiet ups-monitor.service 2>/dev/null; then
        echo "Stopping ups-monitor service..."
        systemctl stop ups-monitor.service
    fi

    if systemctl is-enabled --quiet ups-monitor.service 2>/dev/null; then
        echo "Disabling ups-monitor service..."
        systemctl disable ups-monitor.service
    fi
fi
# UPGRADE: Do nothing - let postinstall of new package handle restart
