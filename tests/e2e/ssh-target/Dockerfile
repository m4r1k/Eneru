# SSH Target container for E2E testing
# Simulates a remote server that Eneru can send shutdown commands to

FROM alpine:3.19

RUN apk add --no-cache \
    openssh-server \
    sudo \
    bash

# Create test user with sudo access
RUN adduser -D -s /bin/bash testuser && \
    echo "testuser:testpass" | chpasswd && \
    echo "testuser ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Generate host keys. Alpine's openssh-server doesn't create
# /var/run/sshd at install time, and sshd refuses to start without it
# (it uses the directory as the privsep chroot). Without this mkdir
# the container exits immediately after launch.
RUN mkdir -p /var/run/sshd && ssh-keygen -A

# Setup SSH directory for testuser
RUN mkdir -p /home/testuser/.ssh && \
    chmod 700 /home/testuser/.ssh && \
    chown -R testuser:testuser /home/testuser/.ssh

# Root loopback E2E uses the same generated key. Keep password auth off for
# root while allowing public-key login, matching the documented default path.
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

# Create a marker file to track if "shutdown" was called
RUN touch /var/run/server-alive

# Create a fake shutdown command that just creates a marker file
# instead of actually shutting down
RUN echo '#!/bin/bash' > /usr/local/bin/fake-shutdown && \
    echo 'echo "$(date): Shutdown command received: $@" >> /var/log/shutdown.log' >> /usr/local/bin/fake-shutdown && \
    echo 'touch /var/run/shutdown-triggered' >> /usr/local/bin/fake-shutdown && \
    echo 'rm -f /var/run/server-alive' >> /usr/local/bin/fake-shutdown && \
    echo 'echo "Shutdown initiated"' >> /usr/local/bin/fake-shutdown && \
    chmod +x /usr/local/bin/fake-shutdown

# F-090/F-080: verifies `augment_remote_path: false` sends a custom command
# without Eneru's POSIX PATH prefix. The command deliberately fails if the
# Synology-only directory appears in its inherited PATH.
RUN echo '#!/bin/sh' > /usr/local/bin/verify-unaugmented-path && \
    echo 'case ":${PATH}:" in *:/usr/syno/sbin:*) exit 42 ;; esac' >> /usr/local/bin/verify-unaugmented-path && \
    echo 'touch /tmp/eneru-path-verbatim' >> /usr/local/bin/verify-unaugmented-path && \
    chmod +x /usr/local/bin/verify-unaugmented-path

# Default-path probe: its bare command name resolves only if Eneru prepends the
# shipped Synology/POSIX directories to the non-interactive SSH command.
RUN mkdir -p /usr/syno/sbin && \
    echo '#!/bin/sh' > /usr/syno/sbin/eneru-path-probe && \
    echo 'touch /tmp/eneru-path-augmented' >> /usr/syno/sbin/eneru-path-probe && \
    chmod +x /usr/syno/sbin/eneru-path-probe

# Alias shutdown to our fake version
RUN ln -sf /usr/local/bin/fake-shutdown /usr/local/bin/shutdown

# Configure sshd
RUN echo "PermitRootLogin prohibit-password" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22

HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
    CMD nc -z localhost 22 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
