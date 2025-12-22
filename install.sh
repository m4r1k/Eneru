#!/bin/bash
set -e

echo "=== UPS Monitor Installation ==="

# Create directories
echo "Creating directories..."
mkdir -p /opt/ups-monitor
mkdir -p /etc/ups-monitor

# Copy script
echo "Installing script..."
/usr/bin/cp -af ./ups-monitor.py /opt/ups-monitor/ups-monitor.py
chmod +x /opt/ups-monitor/ups-monitor.py

# Install config if it doesn't exist (don't overwrite existing config)
if [ ! -f /etc/ups-monitor/config.yaml ]; then
    echo "Installing default configuration..."
    /usr/bin/cp -af ./config.yaml /etc/ups-monitor/config.yaml
    echo "IMPORTANT: Edit /etc/ups-monitor/config.yaml with your settings!"
else
    echo "Configuration file exists, not overwriting."
    echo "New config template saved to /etc/ups-monitor/config.yaml.new"
    /usr/bin/cp -af ./config.yaml /etc/ups-monitor/config.yaml.new
fi

# Copy the SystemD unit
echo "Installing systemd service..."
/usr/bin/cp -af ./ups-monitor.service /etc/systemd/system/ups-monitor.service

# Detect package manager and install dependencies
echo "Installing dependencies..."
if command -v dnf &> /dev/null; then
    dnf install -y python3 python3-pyyaml python3-apprise apprise nut-client openssh-clients util-linux coreutils
elif command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install -y python3 python3-yaml apprise nut-client openssh-client util-linux coreutils
elif command -v pacman &> /dev/null; then
    pacman -S --noconfirm python python-yaml apprise nut openssh util-linux coreutils
else
    echo "Unknown package manager. Please install dependencies manually:"
    echo "  python3, python3-yaml (pyyaml), python3-apprise, nut-client, openssh, util-linux, coreutils"
fi

# Validate configuration
echo "Validating configuration..."
python3 /opt/ups-monitor/ups-monitor.py --validate-config --config /etc/ups-monitor/config.yaml || {
    echo "WARNING: Configuration validation failed. Please check /etc/ups-monitor/config.yaml"
}

# Enable and start service
echo "Enabling service..."
systemctl daemon-reload
systemctl enable --now ups-monitor.service

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit configuration: nano /etc/ups-monitor/config.yaml"
echo "  2. Start the service:  systemctl start ups-monitor.service"
echo "  3. Check status:       systemctl status ups-monitor.service"
echo "  4. View logs:          journalctl -u ups-monitor.service -f"
echo ""
echo "For dry-run testing:"
echo "  systemctl stop ups-monitor.service"
echo "  python3 /opt/ups-monitor/ups-monitor.py --dry-run --config /etc/ups-monitor/config.yaml"
