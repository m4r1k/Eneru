#!/bin/bash
# Post-removal script for Eneru package
set -e

# Reload systemd to remove the service
systemctl daemon-reload

echo ""
echo "Eneru has been removed."
echo ""
echo "Note: Configuration files in /etc/ups-monitor/ have been preserved."
echo "To remove them completely: rm -rf /etc/ups-monitor/"
echo ""
