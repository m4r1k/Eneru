#!/bin/bash
# Post-removal script for Eneru package
# Called after package files are removed
#
# RPM: $1 = 0 (removal complete), $1 = 1+ (upgrade - old removed, new installed)
# DEB: $1 ∈ {remove, purge, upgrade, failed-upgrade, abort-install,
#            abort-upgrade, disappear}
set -e

is_removal=false

case "$1" in
    remove|purge)
        is_removal=true
        ;;
    upgrade|failed-upgrade|abort-install|abort-upgrade|disappear)
        is_removal=false
        ;;
    0)
        # RPM: actual removal.
        is_removal=true
        ;;
    *)
        # RPM: $1 >= 1 means upgrade; anything else: don't message.
        is_removal=false
        ;;
esac

# Reload systemd, but only when the package manager is running on a host
# with a live systemd. Container / chroot builds (e.g. Docker layers)
# have no systemd; calling daemon-reload there aborts under set -e and
# leaves the package half-applied.
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

if [ "$is_removal" = true ]; then
    echo ""
    echo "Eneru has been removed."
    echo ""
    echo "Note: Configuration files in /etc/ups-monitor/ have been preserved."
    echo "To remove them completely: rm -rf /etc/ups-monitor/"
    echo ""
fi
# UPGRADE: silent — no message needed
