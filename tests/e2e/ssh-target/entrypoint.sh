#!/bin/bash
# Entrypoint for SSH target container

set -e

# Copy authorized_keys from mount point and set correct permissions
# (bind-mounted files have host UID which SSH rejects). Defensive
# mkdir on both .ssh dirs so set -e doesn't abort if the Dockerfile
# layer didn't create them.
if [ -f /tmp/host-authorized-keys ]; then
    mkdir -p /home/testuser/.ssh
    chmod 700 /home/testuser/.ssh
    chown testuser:testuser /home/testuser/.ssh
    cp /tmp/host-authorized-keys /home/testuser/.ssh/authorized_keys
    chmod 600 /home/testuser/.ssh/authorized_keys
    chown testuser:testuser /home/testuser/.ssh/authorized_keys
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    cp /tmp/host-authorized-keys /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    chown root:root /root/.ssh/authorized_keys
    echo "SSH authorized_keys installed for testuser and root"
fi

# Reset state on startup
rm -f /var/run/shutdown-triggered
touch /var/run/server-alive
: > /var/log/shutdown.log

echo "SSH target server starting..."
echo "User: testuser"
echo "Password: testpass (also accepts SSH keys)"

# Defensive: re-create /var/run/sshd if a tmpfs / volume mount wiped
# what the Dockerfile created. Cheap when the directory already exists.
mkdir -p /var/run/sshd

# Start sshd in foreground
exec /usr/sbin/sshd -D -e
