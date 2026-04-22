#!/bin/bash
# Pre-removal script for Eneru package
# Called before package files are removed
#
# RPM: $1 = 0 (removal), $1 = 1+ (upgrade - old package being removed)
# DEB: $1 ∈ {remove, purge, upgrade, failed-upgrade, deconfigure,
#            abort-install, abort-upgrade, disappear}
set -e

is_removal=false

case "$1" in
    remove|purge)
        # DEB: actual removal. (purge invokes prerm with "remove" first
        # and then with "purge" — both should stop and disable.)
        is_removal=true
        ;;
    upgrade|failed-upgrade|deconfigure|abort-install|abort-upgrade|disappear)
        # DEB: every non-removal lifecycle. The previous if/elif cascade
        # silently treated all of these as removals.
        is_removal=false
        ;;
    0)
        # RPM: actual removal.
        is_removal=true
        ;;
    *)
        # RPM: $1 >= 1 means upgrade; anything else: leave the service
        # alone rather than stop/disable on an unknown lifecycle arg.
        is_removal=false
        ;;
esac

if [ "$is_removal" = true ]; then
    # ACTUAL REMOVAL: Stop and disable the service
    if systemctl is-active --quiet eneru.service 2>/dev/null; then
        echo "Stopping Eneru service..."
        systemctl stop eneru.service
    fi

    if systemctl is-enabled --quiet eneru.service 2>/dev/null; then
        echo "Disabling Eneru service..."
        systemctl disable eneru.service
    fi
fi
# UPGRADE / non-removal lifecycle: do nothing — postinstall of new
# package handles restart.
