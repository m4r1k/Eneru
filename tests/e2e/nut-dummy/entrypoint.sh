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

# Function to handle scenario switching
# Supports per-UPS scenario files for multi-UPS testing
watch_scenarios() {
    echo "Watching /scenarios for changes..."
    while true; do
        # Single-UPS scenario (backward compatible)
        if [ -f /scenarios/apply.dev ]; then
            echo "Applying scenario to TestUPS"
            cp /scenarios/apply.dev /etc/nut/TestUPS.dev
            chown nut:nut /etc/nut/TestUPS.dev
            rm /scenarios/apply.dev
            sleep 1
            upsc TestUPS@localhost 2>/dev/null | grep -E "ups.status|battery.charge|battery.runtime" || true
        fi

        # Per-UPS scenarios for multi-UPS mode
        if [ -f /scenarios/apply-UPS1.dev ]; then
            echo "Applying scenario to UPS1"
            cp /scenarios/apply-UPS1.dev /etc/nut/UPS1.dev
            chown nut:nut /etc/nut/UPS1.dev
            rm /scenarios/apply-UPS1.dev
            sleep 1
            upsc UPS1@localhost 2>/dev/null | grep -E "ups.status|battery.charge|battery.runtime" || true
        fi

        if [ -f /scenarios/apply-UPS2.dev ]; then
            echo "Applying scenario to UPS2"
            cp /scenarios/apply-UPS2.dev /etc/nut/UPS2.dev
            chown nut:nut /etc/nut/UPS2.dev
            rm /scenarios/apply-UPS2.dev
            sleep 1
            upsc UPS2@localhost 2>/dev/null | grep -E "ups.status|battery.charge|battery.runtime" || true
        fi

        sleep 1
    done
}

# Start scenario watcher in background
watch_scenarios &

# Wait for main processes
wait $UPSD_PID
