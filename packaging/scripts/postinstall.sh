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
        # v5.2: drop an upgrade marker before the restart so the daemon's
        # startup classifier emits "📦 Upgraded vX → vY" instead of a
        # generic "Started" (and so it can fold the previous instance's
        # pending "Stopped" notification, avoiding the stop+start pair).
        # Marker only needs old_version — new_version defaults to the
        # daemon's own __version__ at read time. Best-effort: a write
        # failure just means the user gets the legacy classification.
        # Marker is consumed (deleted) by the daemon on the next start.
        # v5.2.1: preinstall.sh queries `rpm -q eneru` / `dpkg-query`
        # BEFORE the new files unpack and stashes the outgoing version
        # in /run/eneru/.old-version. Prefer that — RPM's postinstall
        # gets nothing useful in $2 (DEB does), so without preinstall the
        # default below would render as "vunknown" in the notification.
        # Initialize explicitly so an environment-inherited OLD_VERSION
        # (theoretically possible under manual rpm/dpkg invocations)
        # can't slip past the empty-check below and render as a bogus
        # version in the upgrade marker. (CodeRabbit P2 from PR #35.)
        OLD_VERSION=""
        if [ -r /run/eneru/.old-version ]; then
            OLD_VERSION=$(cat /run/eneru/.old-version 2>/dev/null || echo "")
            rm -f /run/eneru/.old-version
        fi
        if [ -z "$OLD_VERSION" ]; then
            # DEB postinst still gets the previous version in $2; if that's
            # also missing (manual rpm -ivh --force, partial install, etc.)
            # the daemon's classifier falls back to shutdown_marker.version
            # / meta.last_seen_version, see lifecycle._resolve_old_version.
            OLD_VERSION="${2:-unknown}"
        fi
        # Resolve the stats directory the daemon will actually use.
        # Defaults to /var/lib/eneru but is overridable via
        # statistics.db_directory in /etc/ups-monitor/config.yaml
        # (Cubic P2: previously hardcoded). Fall back to /var/lib/eneru
        # if the config can't be parsed.
        STATS_DIR=$(python3 - <<'PY' 2>/dev/null || echo "/var/lib/eneru"
import sys
try:
    import yaml
    with open("/etc/ups-monitor/config.yaml") as f:
        data = yaml.safe_load(f) or {}
    stats = data.get("statistics") or {}
    print((stats.get("db_directory") or "/var/lib/eneru").strip())
except Exception:
    print("/var/lib/eneru")
PY
)
        MARKER_PATH="${STATS_DIR}/.upgrade_marker.json"
        mkdir -p "${STATS_DIR}" 2>/dev/null || true
        # JSON-encode via python3 so a version with shell-special or
        # JSON-special chars (quotes, backslashes) can't produce a
        # malformed marker. rpm/dpkg version policies don't permit "
        # in practice, but the daemon's read_upgrade_marker degrades
        # gracefully on JSONDecodeError to the _resolve_old_version
        # fallback chain regardless.
        python3 -c 'import json,sys; print(json.dumps({"old_version": sys.argv[1]}))' \
            "${OLD_VERSION}" > "${MARKER_PATH}" 2>/dev/null || true

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
