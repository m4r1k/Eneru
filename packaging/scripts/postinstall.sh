#!/bin/bash
# Post-installation script for Eneru package
set -e

# Reload systemd to pick up the new service file
systemctl daemon-reload

echo ""
echo "=============================================="
echo "  Eneru has been installed successfully!"
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Edit configuration: nano /etc/ups-monitor/config.yaml"
echo "  2. Enable the service: systemctl enable ups-monitor.service"
echo "  3. Start the service:  systemctl start ups-monitor.service"
echo "  4. Check status:       systemctl status ups-monitor.service"
echo "  5. View logs:          journalctl -u ups-monitor.service -f"
echo ""
echo "For dry-run testing:"
echo "  python3 /opt/ups-monitor/ups_monitor.py --dry-run --config /etc/ups-monitor/config.yaml"
echo ""
echo "Validate your configuration:"
echo "  python3 /opt/ups-monitor/ups_monitor.py --validate-config --config /etc/ups-monitor/config.yaml"
echo ""
