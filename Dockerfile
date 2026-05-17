# v5.5 multi-stage build:
#
# - Builder stage installs build deps + builds the wheel into a wheelhouse.
# - Runtime stage installs the wheel from the wheelhouse, plus only the
#   minimal host binaries Eneru actually needs after the loopback contract:
#   nut-client (upsc — the polling path; not optional), openssh-client (the
#   primary path for both remote shutdown AND v5.5 loopback delegation),
#   and tini (PID 1).
#
# Notably DROPPED vs v5.4: docker.io (~150 MB), podman (~80 MB), and
# libvirt-clients (~30 MB). These were only used by the in-process
# shutdown phases (shutdown/containers.py, shutdown/vms.py). Under v5.5
# the container delegates those actions to the host via SSH — the host
# already has the binaries; the container doesn't need them. Realistic
# target: image goes from 500+ MB to under 200 MB.
#
# Container runs as non-root by default (uid 10001). The v5.5 privilege
# check accepts container + loopback as a substitute for root, so no
# elevation is needed for full local-host ownership via SSH delegation.
# See docs/install-comparison.md for the three-profile framing.

FROM python:3.12-slim-trixie AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps only — none of these end up in the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . .

# Resolve and download every wheel (incl. notifications + mqtt extras)
# into /wheels. The runtime stage installs from this offline cache, so
# pip never hits PyPI at runtime-stage time — reproducible across
# rebuilds when the source is identical.
RUN pip wheel --wheel-dir /wheels ".[notifications,mqtt]"


# ----------------------------- runtime stage --------------------------------

FROM python:3.12-slim-trixie

ARG VERSION=dev

LABEL org.opencontainers.image.title="Eneru"
LABEL org.opencontainers.image.description="Intelligent UPS monitoring and shutdown orchestration for NUT"
LABEL org.opencontainers.image.source="https://github.com/m4r1k/Eneru"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime apt packages, in minimum-required form:
#   * tini           — PID 1 init / signal handling
#   * openssh-client — every remote shutdown path, AND v5.5 loopback delegate
#   * nut-client     — provides upsc, the UPS polling primary (monitor.py:782)
#
# Anything missing from this list lives on the HOST and is invoked over SSH
# by the loopback delegate or by configured remote_servers.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        nut-client \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Install the pre-built wheels from the builder stage. --no-index +
# --find-links makes pip refuse to touch PyPI even if the wheelhouse is
# incomplete; surfacing that as a build failure is preferable to a
# silent re-resolve at runtime-stage time.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels \
        "eneru[notifications,mqtt]" \
    && rm -rf /wheels

COPY packaging/eneru-wrapper.py /opt/ups-monitor/eneru.py
COPY examples/config-container-remote.yaml /etc/ups-monitor/config.yaml

RUN install -d -o root -g root -m 0755 /etc/ups-monitor /opt/ups-monitor \
    && useradd --system --uid 10001 --home-dir /var/lib/eneru \
       --shell /usr/sbin/nologin eneru \
    && install -d -o eneru -g eneru -m 0755 \
       /var/lib/eneru /var/run/eneru /var/log/eneru

WORKDIR /

USER eneru

EXPOSE 9191

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9191/ready', timeout=3).read()"

ENTRYPOINT ["/usr/bin/tini", "--", "eneru"]
CMD ["run", "--config", "/etc/ups-monitor/config.yaml", "--api", "--api-bind", "0.0.0.0", "--api-port", "9191"]
