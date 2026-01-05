# NUT Server with dummy driver for E2E testing
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nut-server \
    nut-client \
    inotify-tools \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /var/run/nut /etc/nut /scenarios

# Copy NUT configuration files
COPY nut.conf /etc/nut/nut.conf
COPY ups.conf /etc/nut/ups.conf
COPY upsd.conf /etc/nut/upsd.conf
COPY upsd.users /etc/nut/upsd.users

# Copy default UPS state (online, fully charged)
COPY default.dev /etc/nut/TestUPS.dev

# Set permissions
RUN chown -R nut:nut /etc/nut /var/run/nut && \
    chmod 640 /etc/nut/*.conf /etc/nut/upsd.users && \
    chmod 644 /etc/nut/TestUPS.dev

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3493

# Health check
HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
    CMD upsc TestUPS@localhost 2>/dev/null | grep -q "ups.status" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
