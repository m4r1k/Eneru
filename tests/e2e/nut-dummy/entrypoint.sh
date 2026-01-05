#!/bin/bash
# Entrypoint script for NUT dummy server
# Starts the dummy-ups driver and upsd daemon

set -e

echo "Starting NUT dummy-ups driver..."
/usr/lib/nut/dummy-ups -a TestUPS -D &
DRIVER_PID=$!

# Wait for driver socket to be ready
sleep 2

echo "Starting upsd daemon..."
/usr/sbin/upsd -D -F &
UPSD_PID=$!

# Wait for upsd to be ready
sleep 2

echo "NUT server ready. TestUPS available at localhost:3493"
echo "Current UPS status:"
upsc TestUPS@localhost 2>/dev/null | head -10

# Function to handle scenario switching
watch_scenarios() {
    echo "Watching /scenarios for changes..."
    while true; do
        # Check if there's a scenario file to apply
        if [ -f /scenarios/apply.dev ]; then
            echo "Applying new scenario from /scenarios/apply.dev"
            cp /scenarios/apply.dev /etc/nut/TestUPS.dev
            chown nut:nut /etc/nut/TestUPS.dev
            rm /scenarios/apply.dev
            echo "Scenario applied. New status:"
            sleep 1
            upsc TestUPS@localhost 2>/dev/null | grep -E "ups.status|battery.charge|battery.runtime" || true
        fi
        sleep 1
    done
}

# Start scenario watcher in background
watch_scenarios &

# Wait for main processes
wait $UPSD_PID
