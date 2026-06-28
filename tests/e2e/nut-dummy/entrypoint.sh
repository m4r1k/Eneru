#!/bin/bash
# Entrypoint script for NUT dummy server
# Starts dummy-ups drivers and upsd daemon
# Supports both single-UPS (TestUPS) and multi-UPS (UPS1+UPS2) modes

set -e

echo "Starting NUT dummy-ups drivers..."
/usr/lib/nut/dummy-ups -a TestUPS -D &
/usr/lib/nut/dummy-ups -a UPS1 -D &
/usr/lib/nut/dummy-ups -a UPS2 -D &

# Wait for driver sockets to be ready
sleep 2

echo "Starting upsd daemon..."
/usr/sbin/upsd -D -F &
UPSD_PID=$!

# Wait for upsd to be ready
sleep 2

echo "NUT server ready."
echo "  Single-UPS: TestUPS@localhost:3493"
echo "  Multi-UPS:  UPS1@localhost:3493, UPS2@localhost:3493"
echo "Current status:"
upsc TestUPS@localhost 2>/dev/null | grep -E "ups.status|battery.charge" || true

# Apply one pending scenario trigger and publish a confirmation marker.
#
# Protocol (v6.1): the host-side helper apply_scenario() in
# tests/e2e/groups/lib.sh deletes the applied-<UPS> marker, writes the
# apply[-UPS].dev trigger, then polls for the marker to reappear. We only
# touch applied-<UPS> once upsd is actually SERVING the new state, not
# just after copying the file -- so the marker means "this scenario is
# live", a far tighter signal than the old blind host-side `sleep 3`.
#
# How we confirm "live" without a fixed settle: dummy-ups re-reads its
# .dev on its pollinterval (now 1s, see ups.conf), so we poll upsc until
# the served ups.status matches the status in the file we just copied.
# Every scenario sets ups.status, and the blind-sleep sites this replaces
# were all status transitions, so this resolves in ~1s. A rare
# same-status apply finds the status already matching (returns at once)
# or, if ups.status is somehow absent, falls back to the ~3s poll cap --
# never worse than the old behaviour. We clear the stale marker up front
# so a crash between cp and touch can't leave a false positive behind.
apply_one() {
    local trigger="$1" ups="$2"
    [ -f "/scenarios/$trigger" ] || return 0
    echo "Applying scenario to $ups"
    rm -f "/scenarios/applied-$ups"
    cp "/scenarios/$trigger" "/etc/nut/$ups.dev"
    chown nut:nut "/etc/nut/$ups.dev"
    rm -f "/scenarios/$trigger"

    local want served i
    want="$(grep -E '^ups\.status:' "/etc/nut/$ups.dev" | head -1 | cut -d: -f2- \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    for i in $(seq 1 30); do
        served="$(upsc "$ups@localhost" ups.status 2>/dev/null || true)"
        if [ -n "$want" ] && [ "$served" = "$want" ]; then
            break
        fi
        sleep 0.1
    done

    upsc "$ups@localhost" 2>/dev/null | grep -E "ups.status|battery.charge|battery.runtime" || true
    touch "/scenarios/applied-$ups"
}

# Function to handle scenario switching
# Supports per-UPS scenario files for multi-UPS testing
watch_scenarios() {
    echo "Watching /scenarios for changes..."
    while true; do
        apply_one apply.dev TestUPS        # single-UPS (backward compatible)
        apply_one apply-UPS1.dev UPS1      # multi-UPS member 1
        apply_one apply-UPS2.dev UPS2      # multi-UPS member 2
        sleep 0.3
    done
}

# Start scenario watcher in background
watch_scenarios &

# Wait for main processes
wait $UPSD_PID
