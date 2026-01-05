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

# Generate host keys
RUN ssh-keygen -A

# Setup SSH directory for testuser
RUN mkdir -p /home/testuser/.ssh && \
    chmod 700 /home/testuser/.ssh && \
    chown -R testuser:testuser /home/testuser/.ssh

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

# Alias shutdown to our fake version
RUN ln -sf /usr/local/bin/fake-shutdown /usr/local/bin/shutdown

# Configure sshd
RUN echo "PermitRootLogin no" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22

HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
    CMD nc -z localhost 22 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
