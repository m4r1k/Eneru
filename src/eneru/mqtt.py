"""Optional outbound MQTT publisher."""

import json
import threading
import time
from urllib.parse import urlparse

from eneru.status import collect_status

try:
    import paho.mqtt.client as mqtt_client
    MQTT_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional extra
    mqtt_client = None
    MQTT_AVAILABLE = False


class MQTTPublisher:
    """Publish read-only Eneru status snapshots to MQTT."""

    def __init__(self, source, config, stop_event, log_fn=None):
        self.source = source
        self.config = config
        self.stop_event = stop_event
        self.log_fn = log_fn or (lambda msg: None)
        self._thread = None

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

    def _run(self) -> None:
        parsed = urlparse(self.config.mqtt.broker)
        host = parsed.hostname or self.config.mqtt.broker
        port = parsed.port or 1883
        client = mqtt_client.Client()
        try:
            client.connect(host, port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            self.log_fn(f"⚠️ MQTT connection failed: {exc}")
            return

        interval = max(1, int(self.config.mqtt.publish_interval))
        topic = f"{self.config.mqtt.topic_prefix.rstrip('/')}/status"
        last_fingerprint = ""
        last_publish = 0.0
        try:
            while not self.stop_event.is_set():
                status = collect_status(self.source)
                payload = json.dumps(status, sort_keys=True)
                fingerprint = self._status_fingerprint(status)
                now = time.monotonic()
                should_publish = (
                    fingerprint != last_fingerprint
                    or now - last_publish >= interval
                )
                if should_publish:
                    client.publish(topic, payload, qos=0, retain=False)
                    last_fingerprint = fingerprint
                    last_publish = now
                if self.stop_event.wait(1):
                    break
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _status_fingerprint(status: dict) -> str:
        """Return a stable fingerprint excluding generated timestamps."""
        comparable = dict(status)
        comparable.pop("generatedAt", None)
        return json.dumps(comparable, sort_keys=True)
