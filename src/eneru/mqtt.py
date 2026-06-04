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
        # Daemon-wide shutdown signal. Treat as read-only — never .set()
        # this from inside the publisher; that would force the rest of
        # the daemon to stop too.
        self.stop_event = stop_event
        # Publisher-local stop signal. ``stop()`` sets this; either
        # event firing exits the loop, but only the local one is set
        # by us so a future "bounce only the MQTT publisher" caller
        # (config reload, test fixture) won't take the daemon down.
        self._local_stop = threading.Event()
        self.log_fn = log_fn or (lambda msg: None)
        self._thread: Optional[threading.Thread] = None
        # Set by on_disconnect or by a failed publish. The publish loop
        # checks it each tick and bails out so the outer loop reconnects.
        self._needs_reconnect = threading.Event()

    def _stopping(self) -> bool:
        """Either the daemon-wide or publisher-local stop was requested."""
        return self.stop_event.is_set() or self._local_stop.is_set()

    # Cap each ``stop_event.wait`` slice so a long backoff (up to
    # ``_MAX_BACKOFF_SECONDS``) still notices a publisher-local stop
    # within ~5 s. Short waits (publish-loop tick of 1 s, early-backoff
    # values <= 5 s) take a single direct wait — preserving the test
    # fixtures' wait-call counting.
    _LOCAL_STOP_POLL_SECONDS = 5.0

    def _wait(self, timeout: float) -> bool:
        """Sleep for ``timeout`` seconds, returning True if stop fired.

        For ``timeout <= 5 s`` this is a single ``stop_event.wait`` call
        sandwiched between two cheap ``_local_stop`` checks — daemon
        shutdown (which sets both events together) returns promptly,
        and standalone ``publisher.stop()`` is bounded by ``timeout``.

        For longer waits (60 s reconnect-backoff), we slice into 5 s
        polls so ``publisher.stop()`` mid-backoff is detected within
        ~5 s instead of waiting out the full sleep.
        """
        if self._local_stop.is_set():
            return True
        if timeout <= self._LOCAL_STOP_POLL_SECONDS:
            if self.stop_event.wait(timeout):
                return True
            return self._local_stop.is_set()
        deadline = time.monotonic() + timeout
        while True:
            if self._local_stop.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            slice_ = min(self._LOCAL_STOP_POLL_SECONDS, remaining)
            if self.stop_event.wait(slice_):
                return True

    def start(self) -> None:
        if not self.config.mqtt.enabled or self._thread is not None:
            return
        if not MQTT_AVAILABLE:
            self.log_fn("⚠️  MQTT enabled but paho-mqtt is not installed; publisher disabled")
            return
        self._thread = threading.Thread(
            target=self._run,
            name="eneru-mqtt",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: int = 5) -> None:
        """Signal only the publisher loop and wait briefly for shutdown."""
        self._local_stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        # Outer reconnect loop. Each iteration:
        #   (1) clear the reconnect flag *before* we try to connect, so
        #       any stale signal from a prior teardown can't immediately
        #       short-circuit the new publish loop.
        #   (2) block in _connect_with_backoff until the broker is
        #       reachable or stop fires.
        #   (3) publish until disconnect / stop.
        #   (4) tear the client down (drop the on_disconnect callback
        #       first so a teardown-time disconnect doesn't re-set the
        #       reconnect flag).
        #   (5) reconnect only if both: stop was NOT requested AND
        #       _needs_reconnect is still set after teardown.
        while not self._stopping():
            self._needs_reconnect.clear()
            client = self._connect_with_backoff()
            if client is None:
                return
            try:
                self._publish_loop(client)
            finally:
                # Detach our callback so the disconnect path itself can't
                # spuriously set _needs_reconnect after we've decided to
                # exit. paho-mqtt allows None here.
                try:
                    client.on_disconnect = None
                except Exception:
                    pass
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception as exc:
                    logging.getLogger(__name__).exception("MQTT disconnect failed")
                    self.log_fn(f"⚠️  MQTT disconnect failed: {exc}")
            # Stop wins over reconnect — order matters for the "stop
            # requested mid-disconnect" case.
            if self._stopping():
                return
            if not self._needs_reconnect.is_set():
                return

    def _connect_with_backoff(self):
        """Connect to the broker, retrying with exponential backoff.

        Returns the connected client, or ``None`` if a stop was
        requested before any connect attempt succeeded. The
        ``on_disconnect`` callback is attached only AFTER ``connect()``
        and ``loop_start()`` both return — otherwise paho's background
        thread can fire a disconnect callback against a not-yet-fully-
        constructed publisher state, setting ``_needs_reconnect`` while
        the outer loop still thinks we're in steady state.
        """
        backoff = _INITIAL_BACKOFF_SECONDS
        parsed = urlparse(self.config.mqtt.broker)
        use_tls = parsed.scheme == "mqtts"
        host = parsed.hostname or self.config.mqtt.broker
        port = parsed.port or (8883 if use_tls else 1883)
        while not self._stopping():
            client = _create_client()
            if use_tls:
                # System CA bundle, default ciphers, TLS 1.2+.
                client.tls_set()
            if parsed.username:
                password = unquote(parsed.password or "") if parsed.password is not None else None
                client.username_pw_set(unquote(parsed.username), password)
            try:
                client.connect(host, port, keepalive=30)
                client.loop_start()
            except Exception as exc:
                wait = int(backoff)
                self.log_fn(
                    f"⚠️  MQTT connection failed: {exc} — retrying in {wait}s"
                )
                if self._wait(backoff):
                    return None
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                continue
            # Connection is live. Attach the disconnect callback now so
            # a future broker drop sets _needs_reconnect cleanly.
            client.on_disconnect = self._on_disconnect
            self.log_fn(f"📡  MQTT connected to {host}:{port}")
            return client
        return None

    def _publish_loop(self, client) -> None:
        interval = max(1, int(self.config.mqtt.publish_interval))
        topic = f"{self.config.mqtt.topic_prefix.rstrip('/')}/status"
        # Don't snapshot more often than once per second, but cap the
        # cadence at the configured publish_interval too — there's no
        # value in walking every monitor's lock + remote-health-lock
        # every tick when the user wants 30s publishes. The "publish
        # on change" guarantee still holds because every collect runs
        # the fingerprint check; we just space those collects out.
        collect_period = max(1.0, min(float(interval), 5.0))
        last_fingerprint = ""
        last_publish = 0.0
        last_collect = -collect_period  # force first collect immediately
        while not self._stopping():
            if self._needs_reconnect.is_set():
                return
            now = time.monotonic()
            if now - last_collect >= collect_period:
                status = collect_status(self.source)
                last_collect = now
                payload = json.dumps(status, sort_keys=True)
                fingerprint = self._status_fingerprint(status)
                should_publish = (
                    fingerprint != last_fingerprint
                    or now - last_publish >= interval
                )
                if should_publish:
                    if not self._publish_one(client, topic, payload):
                        return
                    last_fingerprint = fingerprint
                    last_publish = now
            if self._wait(1):
                return

    def _publish_one(self, client, topic: str, payload: str) -> bool:
        """Publish a single message; return False if reconnect is needed."""
        try:
            info = client.publish(topic, payload, qos=0, retain=False)
        except Exception as exc:
            self.log_fn(f"⚠️  MQTT publish failed: {exc}; reconnecting")
            self._needs_reconnect.set()
            return False
        rc = getattr(info, "rc", 0)
        if rc != 0:
            # paho returns MQTT_ERR_NO_CONN (or similar) on a broken
            # connection. Surface it and let the outer loop reconnect
            # rather than silently dropping subsequent publishes.
            self.log_fn(f"⚠️  MQTT publish returned rc={rc}; reconnecting")
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
            self.log_fn("⚠️  MQTT disconnected unexpectedly; will reconnect")
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
