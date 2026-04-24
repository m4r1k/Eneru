#!/bin/bash
# Pre-installation script for Eneru package
# Called BEFORE the new package files are unpacked.
#
# RPM: $1 = 1 (install), $1 = 2 (upgrade)
# DEB: $1 ∈ {install, upgrade, abort-upgrade}; $2 = new version when relevant
#
# Purpose (v5.2.1): capture the OUTGOING package version into a tmp file
# so postinstall.sh can drop it into /var/lib/eneru/.upgrade_marker.json.
#
# DEB's postinst already gets the previous version via "$2", so this is
# functionally needed only for RPM (rpm passes nothing useful in $2). We
# query both package managers anyway — single code path, and lets DEB
# benefit if the postinst arg ever stops being reliable.
set -e

OLD_VERSION=""

# Prefer rpm when both are present (Fedora/RHEL with dpkg in containers
# is rare; the reverse is more common and rpm correctly returns "package
# not installed" when eneru came from a deb).
if command -v rpm >/dev/null 2>&1 && rpm -q eneru >/dev/null 2>&1; then
    OLD_VERSION=$(rpm -q eneru --queryformat '%{VERSION}' 2>/dev/null || true)
elif command -v dpkg-query >/dev/null 2>&1 \
        && dpkg-query -W -f='${Status}' eneru 2>/dev/null \
        | grep -q "install ok installed"; then
    # dpkg-query returns the full version (e.g. "5.2.0-1"); strip the
    # debian revision so the displayed string matches the upstream
    # version the user sees in the UI.
    OLD_VERSION=$(dpkg-query -W -f='${Version}' eneru 2>/dev/null \
                  | sed 's/-[^-]*$//' || true)
fi

if [ -n "$OLD_VERSION" ]; then
    # /run/eneru is tmpfs on systemd hosts and is wiped on reboot, which
    # is exactly the lifetime we want — the file lives just long enough
    # for postinstall.sh to read it. No risk of stale data leaking into
    # a future package upgrade.
    mkdir -p /run/eneru 2>/dev/null || true
    printf '%s' "$OLD_VERSION" > /run/eneru/.old-version 2>/dev/null || true
fi

exit 0
