FROM python:3.12-slim-trixie

ARG VERSION=dev

LABEL org.opencontainers.image.title="Eneru"
LABEL org.opencontainers.image.description="Intelligent UPS monitoring and shutdown orchestration for NUT"
LABEL org.opencontainers.image.source="https://github.com/m4r1k/Eneru"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        docker.io \
        libvirt-clients \
        nut-client \
        openssh-client \
        podman \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/eneru-src
COPY . .

RUN python -m pip install --no-cache-dir ".[notifications,mqtt]" \
    && install -d -o root -g root -m 0755 /etc/ups-monitor \
    && install -d -o root -g root -m 0755 /opt/ups-monitor \
    && cp packaging/eneru-wrapper.py /opt/ups-monitor/eneru.py \
    && cp examples/config-container-remote.yaml /etc/ups-monitor/config.yaml \
    && useradd --system --uid 10001 --home-dir /var/lib/eneru --shell /usr/sbin/nologin eneru \
    && install -d -o eneru -g eneru -m 0755 /var/lib/eneru /var/run/eneru /var/log/eneru \
    && cd / \
    && rm -rf /opt/eneru-src

WORKDIR /

USER eneru

EXPOSE 9191

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9191/health', timeout=3).read()"

ENTRYPOINT ["/usr/bin/tini", "--", "eneru"]
CMD ["run", "--config", "/etc/ups-monitor/config.yaml", "--api", "--api-bind", "0.0.0.0", "--api-port", "9191"]
