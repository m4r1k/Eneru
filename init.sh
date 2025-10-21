#!/bin/bash

/usr/bin/cp -af ./ups-monitor.service /etc/systemd/system/ups-monitor.service
/usr/bin/cp -af ./ups-monitor.sh /usr/local/bin/ups-monitor.sh

systemctl daemon-reload
systemctl enable --now ups-monitor.service

sleep 10s
systemctl status ups-monitor.service

echo "tail -f /var/log/ups-monitor.log /var/run/ups-monitor.state"
echo "journalctl -u ups-monitor.service -l"

