#!/bin/bash
# Pre-removal script for Eneru package
set -e

# Stop the service if it's running
if systemctl is-active --quiet ups-monitor.service 2>/dev/null; then
    echo "Stopping ups-monitor service..."
    systemctl stop ups-monitor.service
fi

# Disable the service if it's enabled
if systemctl is-enabled --quiet ups-monitor.service 2>/dev/null; then
    echo "Disabling ups-monitor service..."
    systemctl disable ups-monitor.service
fi
