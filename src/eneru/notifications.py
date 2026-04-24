"""DB-backed notification worker for Eneru (v5.2+).

Replaces the v5.1 in-memory queue with a SQLite-persisted queue so that
notifications survive process death, network outages, and reboots.
Pending rows live in each registered ``StatsStore``'s ``notifications``
table (schema v4) and only get marked ``sent`` once Apprise confirms
delivery. The "panic-attack" guarantee: a power outage that takes the
internet down still produces the notifications when the endpoint comes
back, even days later.

Architecture:
- Main thread: queues notifications via ``send()`` → INSERT pending row
  in the calling monitor's store, signal the worker.
- Worker thread: polls all registered stores for the globally-oldest
  pending row, attempts delivery via Apprise, marks ``sent`` on success
  or increments ``attempts`` on failure. Per-message exponential backoff
  prevents network hammering during prolonged outages.
- TTL pruning runs once per minute (cheap when nothing to delete) to
  bound DB growth.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from eneru.config import Config
from eneru.stats import StatsStore

# Optional import for Apprise
try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:
    apprise = None
    APPRISE_AVAILABLE = False


_PRUNE_INTERVAL_SECS = 60.0


class NotificationWorker:
    """Persistent, lossless notification worker.

    Queues live in per-store SQLite tables (the ``notifications`` table
    on each registered :class:`StatsStore`). The worker thread merges
    pending rows across all stores and delivers them in age order.

    Stores register themselves when their SQLite DB opens
    (:meth:`register_store`); sends made before any store is registered
    are buffered in memory and flushed to the first store as soon as it
    appears (covers the multi-UPS coordinator startup window).

    Failure handling:
    - Apprise success → row marked ``sent``, ``sent_at`` recorded.
    - Apprise failure → ``attempts`` incremented; per-message
      exponential backoff (``retry_interval`` doubling, capped at
      ``retry_backoff_max``) determines when to retry.
    - ``max_attempts`` (default 0 = unlimited) caps per-message retries
      before the row is ``cancelled (max_attempts)``. Default off
      because Apprise's bool return doesn't distinguish "bad URL" from
      "internet down" — giving up risks dropping legitimate messages.
    - ``max_age_days`` (default 30) cancels pending rows older than
      that bound, so a stuck message can't sit in the queue forever.
    - ``max_pending`` (default 10000) caps backlog; on overflow the
      oldest pending rows get ``cancelled (backlog_overflow)``.
    """

    def __init__(self, config: Config):
        self.config = config
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._apprise_instance: Optional[Any] = None
        self._initialized = False
        # Registered stores in registration order. The first store also
        # serves as the default destination for sends that don't pin a
        # specific store (notably the coordinator-level lifecycle
        # notifications in multi-UPS mode).
        self._stores: List[StatsStore] = []
        self._stores_lock = threading.Lock()
        # Pre-store memory buffer: tuples of
        # (body, notify_type, category, ts) for sends that arrived
        # before any store was registered. Drained on the first
        # ``register_store`` call.
        self._memory_buffer: List[Tuple[str, str, str, int]] = []
        # Per-message backoff state: (str(store.db_path), row_id) →
        # next_attempt_monotonic. Cleared on success / cancellation. We
        # key on the db_path STRING (not id(store)) so re-creation of a
        # StatsStore object at the same memory address can't bleed
        # backoff state across instances.
        self._backoff: Dict[Tuple[str, int], float] = {}
        # Track last prune time so we don't run DELETE on every iteration.
        self._last_prune_monotonic = 0.0
        # Used during stop() to surface the pending count to a "messages
        # left undelivered" log warning.
        self._final_pending_count = 0

    # ----- lifecycle -----

    def start(self) -> bool:
        """Initialize Apprise and start the background worker thread."""
        if not self.config.notifications.enabled:
            return False
        if not APPRISE_AVAILABLE:
            return False
        if not self.config.notifications.urls:
            return False

        self._apprise_instance = apprise.Apprise()
        for url in self.config.notifications.urls:
            if not self._apprise_instance.add(url):
                print(f"Warning: Failed to add notification URL: {url}")
        if len(self._apprise_instance) == 0:
            print("Warning: No valid notification URLs configured")
            return False

        self._stop_event.clear()
        self._wakeup_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="notify-worker",
        )
        self._worker_thread.start()
        self._initialized = True
        return True

    def stop(self) -> None:
        """Stop the background worker thread.

        Pending rows stay in the DB; the next process start will pick
        them up and resume delivery (the lossless guarantee). The 5s
        :meth:`flush` in the shutdown path (Slice 5) gives the worker
        a chance to drain the in-flight queue first; whatever's left
        survives in SQLite.
        """
        if self._worker_thread and self._worker_thread.is_alive():
            with self._stores_lock:
                pending = sum(
                    s.pending_notification_count() for s in self._stores
                )
            self._final_pending_count = pending
            if pending > 0:
                # Informational, not an error: the rows aren't lost,
                # they just go out on the next start.
                print(
                    f"ℹ️ Notification worker stopping with {pending} "
                    f"message(s) still pending in the persistent queue "
                    f"— they will deliver on the next start."
                )
            self._stop_event.set()
            self._wakeup_event.set()
            self._worker_thread.join(timeout=2)

    # ----- store registration -----

    def register_store(self, store: StatsStore) -> None:
        """Register a per-UPS stats store as a notification destination.

        Each :class:`UPSGroupMonitor` calls this once its
        :class:`StatsStore` is open. The first registered store also
        becomes the default for sends that don't pin a store (e.g. the
        coordinator-level lifecycle notifications fired before any
        per-UPS thread comes up).

        The in-memory buffer is drained AFTER the store is appended.
        Each ``enqueue_notification`` is checked for success; rows that
        fail to persist (transient SQLite error) get re-buffered so the
        next ``register_store`` call (or a worker retry) can try again.
        Without this, a transient sqlite failure during the drain would
        permanently drop coordinator-startup notifications (CR P1).
        """
        if store is None:
            return
        with self._stores_lock:
            if store in self._stores:
                return
            self._stores.append(store)
            # Snapshot the buffer; do NOT clear it yet — we only clear
            # entries that successfully persist below.
            buffered = list(self._memory_buffer)
            self._memory_buffer = []
        if not buffered:
            return
        leftovers: List[Tuple[str, str, str, int]] = []
        for body, notify_type, category, ts in buffered:
            row_id = store.enqueue_notification(
                body, notify_type, category, ts=ts,
            )
            if row_id is None:
                # enqueue returned None → store wasn't open or SQLite
                # raised; keep the row buffered so a future register
                # gets a chance.
                leftovers.append((body, notify_type, category, ts))
        if leftovers:
            with self._stores_lock:
                # Prepend so age order is preserved against any new
                # sends that arrived while we were draining.
                self._memory_buffer = leftovers + self._memory_buffer
        # Apply backlog cap after the drain (P2 follow-up to the
        # in-memory cap): if the buffer grew large before the store
        # registered, the persisted side now needs the same trim.
        cap = self.config.notifications.max_pending
        if cap > 0:
            store.cap_pending_notifications(cap)
        self._wakeup_event.set()

    # ----- producer side -----

    def send(self, body: str, notify_type: str = "info",
             category: str = "general",
             store: Optional[StatsStore] = None,
             blocking: bool = False) -> Optional[int]:
        """Queue a notification for persistent, retried delivery.

        Args:
            body: Notification body.
            notify_type: One of 'info', 'success', 'warning', 'failure'.
            category: Coarse classification for coalescing (Slice 4)
                and per-category queries. Common values: ``lifecycle``,
                ``power_event``, ``voltage``, ``shutdown``,
                ``shutdown_summary``, ``general``.
            store: Destination StatsStore. Defaults to the first
                registered store; falls back to in-memory buffer if no
                store is registered yet (multi-UPS coordinator startup).
            blocking: Reserved for future use. The v5.1 worker honoured
                this for the ``--test-notifications`` CLI; v5.2's DB
                queue means delivery is asynchronous by design. Kept in
                the signature for back-compat; ignored.

        Returns the new notification id, or ``None`` if the send was
        buffered (no store yet) or the worker isn't initialized.
        """
        del blocking  # back-compat shim; the v5.2 queue is always async
        if not self._initialized:
            return None

        ts = int(time.time())
        target_store = store
        with self._stores_lock:
            if target_store is None and self._stores:
                target_store = self._stores[0]
            if target_store is None:
                # Pre-store buffer: replayed verbatim (with original ts)
                # once register_store fires.
                self._memory_buffer.append(
                    (body, notify_type, category, ts)
                )
                self._trim_memory_buffer()
                self._wakeup_event.set()
                return None

        notification_id = target_store.enqueue_notification(
            body=body, notify_type=notify_type, category=category, ts=ts,
        )
        # Enforce backlog cap right after insert so the just-added row
        # stays (it's the newest by definition; cap_pending cancels
        # oldest first).
        max_pending = self.config.notifications.max_pending
        if max_pending > 0:
            target_store.cap_pending_notifications(max_pending)
        self._wakeup_event.set()
        return notification_id

    def _trim_memory_buffer(self) -> None:
        """Drop oldest in-memory pending notifications when the buffer
        overflows ``max_pending``. Without this, a misconfigured daemon
        (notifications.enabled=true but every store fails to open) would
        accumulate every send forever and OOM. Called from inside
        ``send()`` after appending, so the just-added row stays."""
        cap = self.config.notifications.max_pending
        if cap <= 0 or len(self._memory_buffer) <= cap:
            return
        excess = len(self._memory_buffer) - cap
        del self._memory_buffer[:excess]
        if not getattr(self, "_buffer_overflow_warned", False):
            print(
                f"⚠️ Notification memory buffer exceeded {cap} entries — "
                "oldest dropped. Stats DB unreachable? Check "
                f"{self.config.statistics.db_directory if hasattr(self.config, 'statistics') else '/var/lib/eneru'} "
                "is writable."
            )
            self._buffer_overflow_warned = True

    # ----- consumer side -----

    def flush(self, timeout: float) -> bool:
        """Block until every registered store's pending count reaches 0
        AND the in-memory pre-store buffer is empty, or until ``timeout``
        seconds elapse.

        Returns ``True`` if drained cleanly, ``False`` on timeout. Used
        from the shutdown path (Slice 5) to give in-flight notifications
        their best chance of being sent before the process exits.
        Whatever doesn't drain stays as ``pending`` rows in SQLite and
        flushes on the next start.

        The drain check includes ``_memory_buffer`` (CR P1): if shutdown
        fires before any store registers, the final lifecycle / shutdown
        message can still be sitting in memory, and ``flush`` would
        otherwise return ``True`` immediately and ``stop`` would drop it.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        # Wake the worker so it doesn't sit on its 1 s poll if there's
        # something to do.
        self._wakeup_event.set()
        while True:
            with self._stores_lock:
                buffered = len(self._memory_buffer)
                pending = sum(
                    s.pending_notification_count() for s in self._stores
                )
            if pending == 0 and buffered == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
            self._wakeup_event.set()

    def _worker_loop(self) -> None:
        """Background worker: drain pending rows from all stores."""
        while not self._stop_event.is_set():
            # Wait for a wakeup signal or a 1 s tick. Short tick so a
            # message that just hit its backoff window gets retried
            # promptly without needing an external poke.
            self._wakeup_event.wait(timeout=1.0)
            self._wakeup_event.clear()

            try:
                self._drain_once()
                self._maybe_prune()
            except Exception:
                # Defensive — the worker thread must NEVER crash, since
                # losing it would silently disable all future notifications.
                pass

    def _drain_once(self) -> None:
        """Drain pending rows: coalesce on every iteration, then deliver
        the oldest due candidate. Repeats until nothing is due (or the
        stop event fires).

        The coalesce pass runs INSIDE the inner loop because a single
        Apprise call can block for seconds on an unreachable endpoint;
        an on_battery + on_line pair that arrives during that block must
        still get folded into a single "Brief outage" before either
        half is wasted on a separate send. Coalesce is cheap (one
        SELECT + at most one INSERT/UPDATE per pair); running it per
        iteration keeps the merge window tight.
        """
        with self._stores_lock:
            stores_snapshot = list(self._stores)

        max_iters = 1000  # Defensive — large bursts must still drain.
        for _ in range(max_iters):
            if self._stop_event.is_set():
                return

            for store in stores_snapshot:
                try:
                    self._coalesce_pending_outages(store)
                except Exception:
                    pass  # coalescing must never block delivery

            candidate = self._next_due_candidate()
            if candidate is None:
                return

            store, ts, row_id, body, notify_type, attempts = candidate
            self._process_one(
                store=store, row_id=row_id, body=body,
                notify_type=notify_type, attempts=attempts,
            )

    def _coalesce_pending_outages(self, store) -> int:
        """Pair pending on_battery + on_line rows into a single
        "Brief outage" summary. Returns count of pairs coalesced.

        Pairs are identified by sub-typed category (set in
        ``UPSGroupMonitor._log_power_event``):
        ``power_event_on_battery`` and ``power_event_on_line``. Exact
        category match keeps the coalescer from depending on the
        user-visible body wording, which changes more often than the
        category enum.

        Both rows must still be ``pending`` for coalescing to apply.
        Once one of the pair has shipped (the network was up at one end
        but not the other), they go out separately.
        """
        on_batt_rows = store.find_pending_by_category(
            "power_event_on_battery"
        ) or []
        on_line_rows = store.find_pending_by_category(
            "power_event_on_line"
        ) or []
        if not on_batt_rows or not on_line_rows:
            return 0

        from datetime import datetime
        from eneru.utils import format_seconds

        # Pair each on_battery with the next on_line whose ts is >= this
        # on_battery's ts. Both lists are sorted by ts ASC from the
        # store query. Equal-ts pairs ARE valid (NUT polls at 1s
        # granularity; a same-second cycle is reachable in tests and
        # high-poll-rate setups), so this is strict-less-than.
        coalesced = 0
        line_idx = 0
        for ob_id, ob_ts, _, _ in on_batt_rows:
            # Advance the on_line cursor past anything strictly older
            # than this on_battery (those come from a previous outage
            # and either already shipped or will pair with an older
            # on_battery).
            while line_idx < len(on_line_rows) and on_line_rows[line_idx][1] < ob_ts:
                line_idx += 1
            if line_idx >= len(on_line_rows):
                break
            ol_id, ol_ts, _, _ = on_line_rows[line_idx]
            duration = max(0, ol_ts - ob_ts)
            start_str = datetime.fromtimestamp(ob_ts).strftime("%H:%M:%S")
            end_str = datetime.fromtimestamp(ol_ts).strftime("%H:%M:%S")
            summary_body = (
                f"📊 **Brief Power Outage**\n"
                f"On battery for {format_seconds(duration)} "
                f"({start_str} → {end_str})."
            )
            # Use the end timestamp so the summary sorts AFTER any
            # other unrelated pending power events from before the
            # outage. Keep the generic category so a downstream "where
            # are my power events?" query still finds it.
            new_id = store.enqueue_notification(
                body=summary_body, notify_type="info",
                category="power_event", ts=ol_ts,
            )
            # Only cancel the originals if the summary actually persisted
            # (Cubic P1). Otherwise we'd silently drop the outage
            # notifications on a transient SQLite error.
            if new_id is None:
                line_idx += 1
                continue
            store.cancel_notification(ob_id, "coalesced")
            store.cancel_notification(ol_id, "coalesced")
            line_idx += 1
            coalesced += 1
        return coalesced

    def _next_due_candidate(self) -> Optional[Tuple]:
        """Return the oldest pending row across all registered stores
        whose backoff window has elapsed, or ``None`` if nothing is due.

        Scans up to ``max_pending`` pending rows per store in a single
        SELECT so a head-of-queue cluster of backed-off rows can't
        starve newer due rows (CR P1). The previous ``limit=10`` made
        row 11+ invisible until the head drained, which never happened
        when the head was a poison message with ``max_attempts=0``.

        Returned tuple: ``(store, ts, id, body, notify_type, attempts)``.
        """
        now_mono = time.monotonic()
        best: Optional[Tuple] = None
        scan_cap = max(50,
                       int(self.config.notifications.max_pending or 10000))
        with self._stores_lock:
            stores_snapshot = list(self._stores)
        for store in stores_snapshot:
            rows = store.next_pending_notifications(limit=scan_cap)
            for ts, row_id, body, notify_type, attempts, _cat in rows:
                key = (str(store.db_path), int(row_id))
                next_attempt = self._backoff.get(key, 0.0)
                if now_mono < next_attempt:
                    continue
                cand = (store, int(ts), int(row_id), str(body),
                        str(notify_type), int(attempts))
                if best is None or cand[1] < best[1] or (
                    cand[1] == best[1] and cand[2] < best[2]
                ):
                    best = cand
        return best

    def _process_one(self, *, store: StatsStore, row_id: int,
                     body: str, notify_type: str,
                     attempts: int) -> None:
        """Attempt delivery of a single row. Marks sent on success;
        increments attempts and applies exponential backoff on failure.
        Cancels on max_attempts overrun (when configured)."""
        success = self._send_via_apprise(body, notify_type)
        key = (str(store.db_path), row_id)

        if success:
            store.mark_notification_sent(row_id)
            self._backoff.pop(key, None)
            return

        store.mark_notification_attempt(row_id)
        new_attempts = attempts + 1

        max_attempts = self.config.notifications.max_attempts
        if max_attempts > 0 and new_attempts >= max_attempts:
            store.cancel_notification(row_id, "max_attempts")
            self._backoff.pop(key, None)
            return

        # Exponential backoff: retry_interval * 2^(attempts-1), capped.
        # Treat retry_interval=0 as "every tick" (the 1 s poll loop).
        base = max(0, int(self.config.notifications.retry_interval))
        cap = max(base, int(self.config.notifications.retry_backoff_max))
        # Bound the shift to avoid pathological 2**N for huge attempts.
        shift = min(new_attempts - 1, 20)
        wait_secs = min(base * (2 ** shift) if base > 0 else 0, cap)
        self._backoff[key] = time.monotonic() + max(0, wait_secs)

    def _send_via_apprise(self, body: str, notify_type: str) -> bool:
        """Call Apprise. Returns True iff at least one backend
        accepted the message. Network/DNS errors → False (worker
        retries via the backoff schedule)."""
        if not self._apprise_instance:
            return False
        try:
            type_map = {
                "info": apprise.NotifyType.INFO,
                "success": apprise.NotifyType.SUCCESS,
                "warning": apprise.NotifyType.WARNING,
                "failure": apprise.NotifyType.FAILURE,
            }
            apprise_type = type_map.get(notify_type, apprise.NotifyType.INFO)

            notify_kwargs: Dict[str, Any] = {
                "body": body,
                "notify_type": apprise_type,
            }
            # Only include the title if explicitly configured (the v5.1
            # behaviour — None / empty title means body-only).
            title = self.config.notifications.title
            if title:
                notify_kwargs["title"] = title

            return bool(self._apprise_instance.notify(**notify_kwargs))
        except Exception:
            return False

    def _maybe_prune(self) -> None:
        """Run TTL prune at most once per ``_PRUNE_INTERVAL_SECS``.

        Two-step:
          1. ``DELETE`` ``sent`` / ``cancelled`` rows older than
             ``retention_days``.
          2. ``UPDATE`` pending rows older than ``max_age_days`` to
             ``cancelled (too_old)``. Skipped when ``max_age_days <= 0``
             (panic-attack guarantee — pending lives forever).
        """
        now_mono = time.monotonic()
        if now_mono - self._last_prune_monotonic < _PRUNE_INTERVAL_SECS:
            return
        self._last_prune_monotonic = now_mono

        retention = max(1, int(self.config.notifications.retention_days))
        max_age = max(0, int(self.config.notifications.max_age_days))
        with self._stores_lock:
            stores_snapshot = list(self._stores)
        for store in stores_snapshot:
            store.prune_old_notifications(retention, max_age)

    # ----- accessors (mostly for tests + status displays) -----

    def get_service_count(self) -> int:
        """Return the number of configured Apprise backends."""
        if self._apprise_instance:
            return len(self._apprise_instance)
        return 0

    def get_pending_count(self) -> int:
        """Sum of pending rows across all registered stores. 0 when no
        store is registered yet (memory buffer is opaque to callers —
        it flushes on first registration)."""
        with self._stores_lock:
            return sum(s.pending_notification_count() for s in self._stores)
