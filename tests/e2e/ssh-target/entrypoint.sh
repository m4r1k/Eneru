#!/bin/bash
# Entrypoint for SSH target container

set -e

# If authorized_keys is mounted, try to set proper permissions (may be read-only mount)
if [ -f /home/testuser/.ssh/authorized_keys ]; then
    chmod 600 /home/testuser/.ssh/authorized_keys 2>/dev/null || true
    chown testuser:testuser /home/testuser/.ssh/authorized_keys 2>/dev/null || true
fi

# Reset state on startup
rm -f /var/run/shutdown-triggered
touch /var/run/server-alive
: > /var/log/shutdown.log

echo "SSH target server starting..."
echo "User: testuser"
echo "Password: testpass (also accepts SSH keys)"

# Start sshd in foreground
exec /usr/sbin/sshd -D -e
