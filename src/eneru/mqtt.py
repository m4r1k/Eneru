"""Optional outbound MQTT publisher.

Publishes read-only status snapshots to a configured broker. The
publisher runs on its own daemon thread and is bounded by an
exponential-backoff reconnect loop so a transient broker outage doesn't
permanently disable observability. ``mqtts://`` URLs enable TLS using
the system trust store (mTLS / client certs are out of scope for v5.3).
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from eneru.status import collect_status

try:
    import paho.mqtt.client as mqtt_client
    MQTT_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional extra
    mqtt_client = None
    MQTT_AVAILABLE = False


# Reconnect-with-backoff parameters. Bounds the worst-case delay between
# the broker becoming reachable again and the publisher catching up,
# while keeping CPU and log noise low during long outages.
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0


class MQTTPublisher:
    """Publish read-only Eneru status snapshots to MQTT."""

    def __init__(
        self,
        source: Any,
        config: Any,
        stop_event: threading.Event,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.source = source
        self.config = config
        self.stop_event = stop_event
        self.log_fn = log_fn or (lambda msg: None)
        self._thread: Optional[threading.Thread] = None
        # Set by on_disconnect or by a failed publish. The publish loop
        # checks it each tick and bails out so the outer loop reconnects.
        self._needs_reconnect = threading.Event()

    def start(self) -> None:
        if not self.config.mqtt.enabled or self._thread is not None:
            return
        if not MQTT_AVAILABLE:
            self.log_fn("⚠️ MQTT enabled but paho-mqtt is not installed; publisher disabled")
            return
        self._thread = threading.Thread(
            target=self._run,
            name="eneru-mqtt",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: int = 5) -> None:
        """Signal the publisher loop and wait briefly for broker disconnect."""
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        # Outer reconnect loop. Each iteration: (1) block in
        # _connect_with_backoff until the broker is reachable or
        # stop_event fires, (2) publish until disconnect / stop,
        # (3) tear the client down, (4) loop back to reconnect ONLY
        # if _publish_loop exited because of an unexpected
        # disconnect; otherwise exit (stop_event drove the exit).
        while not self.stop_event.is_set():
            client = self._connect_with_backoff()
            if client is None:
                return
            try:
                self._publish_loop(client)
            finally:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception as exc:
                    logging.getLogger(__name__).exception("MQTT disconnect failed")
                    self.log_fn(f"⚠️ MQTT disconnect failed: {exc}")
            # Distinguish reconnect-needed from stop-driven exit. We
            # can't rely on stop_event.is_set() here because some test
            # fixtures (and some real Event subclasses) report False
            # even when their .wait() returned True.
            if not self._needs_reconnect.is_set():
                return
            self._needs_reconnect.clear()

    def _connect_with_backoff(self):
        """Connect to the broker, retrying with exponential backoff.

        Returns the connected client, or ``None`` if ``stop_event`` was
        set before any connect attempt succeeded.
        """
        backoff = _INITIAL_BACKOFF_SECONDS
        parsed = urlparse(self.config.mqtt.broker)
        use_tls = parsed.scheme == "mqtts"
        host = parsed.hostname or self.config.mqtt.broker
        port = parsed.port or (8883 if use_tls else 1883)
        while not self.stop_event.is_set():
            client = _create_client()
            if use_tls:
                # System CA bundle, default ciphers, TLS 1.2+.
                client.tls_set()
            if parsed.username:
                password = unquote(parsed.password or "") if parsed.password is not None else None
                client.username_pw_set(unquote(parsed.username), password)
            client.on_disconnect = self._on_disconnect
            try:
                client.connect(host, port, keepalive=30)
                client.loop_start()
                self.log_fn(f"📡 MQTT connected to {host}:{port}")
                return client
            except Exception as exc:
                wait = int(backoff)
                self.log_fn(
                    f"⚠️ MQTT connection failed: {exc} — retrying in {wait}s"
                )
                # stop_event.wait returns True if set during the wait,
                # which short-circuits the backoff for fast shutdown.
                if self.stop_event.wait(backoff):
                    return None
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
        return None

    def _publish_loop(self, client) -> None:
        interval = max(1, int(self.config.mqtt.publish_interval))
        topic = f"{self.config.mqtt.topic_prefix.rstrip('/')}/status"
        last_fingerprint = ""
        last_publish = 0.0
        while not self.stop_event.is_set():
            if self._needs_reconnect.is_set():
                return
            status = collect_status(self.source)
            payload = json.dumps(status, sort_keys=True)
            fingerprint = self._status_fingerprint(status)
            now = time.monotonic()
            should_publish = (
                fingerprint != last_fingerprint
                or now - last_publish >= interval
            )
            if should_publish:
                if not self._publish_one(client, topic, payload):
                    return
                last_fingerprint = fingerprint
                last_publish = now
            if self.stop_event.wait(1):
                return

    def _publish_one(self, client, topic: str, payload: str) -> bool:
        """Publish a single message; return False if reconnect is needed."""
        try:
            info = client.publish(topic, payload, qos=0, retain=False)
        except Exception as exc:
            self.log_fn(f"⚠️ MQTT publish failed: {exc}; reconnecting")
            self._needs_reconnect.set()
            return False
        rc = getattr(info, "rc", 0)
        if rc != 0:
            # paho returns MQTT_ERR_NO_CONN (or similar) on a broken
            # connection. Surface it and let the outer loop reconnect
            # rather than silently dropping subsequent publishes.
            self.log_fn(f"⚠️ MQTT publish returned rc={rc}; reconnecting")
            self._needs_reconnect.set()
            return False
        return True

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        # paho-mqtt 1.x signature: (client, userdata, rc)
        # paho-mqtt 2.x VERSION1 callbacks keep the same signature.
        # Either way, a non-zero rc means "we did not initiate this".
        rc = args[0] if args else kwargs.get("rc", 0)
        try:
            unexpected = int(getattr(rc, "value", rc) or 0) != 0
        except (TypeError, ValueError):
            unexpected = bool(rc)
        if unexpected:
            self.log_fn("⚠️ MQTT disconnected unexpectedly; will reconnect")
            self._needs_reconnect.set()

    @staticmethod
    def _status_fingerprint(status: dict) -> str:
        """Return a stable fingerprint excluding generated timestamps."""
        comparable = dict(status)
        comparable.pop("generatedAt", None)
        return json.dumps(comparable, sort_keys=True)


def _create_client():
    """Create a paho client compatible with paho-mqtt 1.x and 2.x."""
    kwargs = {}
    if hasattr(mqtt_client, "CallbackAPIVersion"):
        kwargs["callback_api_version"] = mqtt_client.CallbackAPIVersion.VERSION1
    return mqtt_client.Client(**kwargs)
