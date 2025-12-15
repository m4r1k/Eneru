#!/bin/bash

# Create directory
mkdir -p /opt/ups-monitor

# Copy script
/usr/bin/cp -af ./ups-monitor.py /opt/ups-monitor/ups-monitor.py

# Copy the SystemD unit
/usr/bin/cp -af ./ups-monitor.service /etc/systemd/system/ups-monitor.service

# Make executable
chmod +x /opt/ups-monitor/ups-monitor.py

# Install dependencies
dnf install -y python3 python3-requests nut-client openssh-clients util-linux coreutils

# Enable and start service
systemctl daemon-reload
systemctl enable --now ups-monitor.service

echo "tail -f /var/log/ups-monitor.log /var/run/ups-monitor.state"
echo "journalctl -u ups-monitor.service -f"
