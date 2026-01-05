#!/bin/bash
# Entrypoint for SSH target container

set -e

# Copy authorized_keys from mount point and set correct permissions
# (bind-mounted files have host UID which SSH rejects)
if [ -f /tmp/host-authorized-keys ]; then
    cp /tmp/host-authorized-keys /home/testuser/.ssh/authorized_keys
    chmod 600 /home/testuser/.ssh/authorized_keys
    chown testuser:testuser /home/testuser/.ssh/authorized_keys
    echo "SSH authorized_keys installed for testuser"
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
