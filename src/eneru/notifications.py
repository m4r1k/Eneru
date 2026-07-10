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

# F-067: how long the worker keeps memory-buffered rows reserved for the
# normal register_store → SQLite path before falling back to DIRECT Apprise
# delivery. Within this window after start() (the startup register window),
# fresh rows wait for a store; past it — or as soon as a store IS registered
# but refuses rows (unwritable /var/lib/eneru) — buffered rows ship straight
# through Apprise so they are never silently lost.
_BUFFER_DIRECT_GRACE_SECS = 10.0


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

    def __init__(self, config: Config, logger: Optional[Any] = None):
        self.config = config
        # ISS-060: route operational warnings through the structured logger
        # when the caller has one (the daemon does), falling back to print
        # for pre-logger-init / one-shot CLI paths. Composes with ISS-008's
        # URL redaction (callers redact before passing the message text).
        self._logger = logger
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
        # A bounded stop() can return while Apprise is still sending. During
        # reload, failed in-flight memory rows are forwarded to this worker;
        # successful rows are already gone and therefore cannot be duplicated.
        self._memory_handoff_worker: Optional["NotificationWorker"] = None
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
        # F-067 direct-delivery state: monotonic start stamp (grace window
        # for the startup register path) + a single backoff for the direct
        # Apprise fallback so an unreachable endpoint isn't hammered every
        # 1s tick. Set/reset in start().
        self._start_mono = 0.0
        self._buffer_direct_attempts = 0
        self._buffer_direct_next_mono = 0.0

    # ----- lifecycle -----

    def _warn(self, message: str) -> None:
        """Emit an operational message via the structured logger when the
        worker was given one, else fall back to ``print`` (ISS-060)."""
        logger = self._logger
        if logger is not None:
            try:
                logger.log(message)
                return
            except Exception:
                pass  # never let a logging failure swallow the message
        print(message)

    def start(self) -> bool:
        """Initialize Apprise and start the background worker thread."""
        if not self.config.notifications.enabled:
            return False
        if not APPRISE_AVAILABLE:
            return False
        if not self.config.notifications.urls:
            return False

        from eneru.utils import redact_apprise_url
        self._apprise_instance = apprise.Apprise()
        for url in self.config.notifications.urls:
            if not self._apprise_instance.add(url):
                # ISS-008: never echo the raw URL -- it embeds webhook
                # tokens/passwords. Log the scheme only.
                self._warn(
                    "Warning: Failed to add notification URL: "
                    f"{redact_apprise_url(url)}"
                )
        if len(self._apprise_instance) == 0:
            self._warn("Warning: No valid notification URLs configured")
            return False

        self._stop_event.clear()
        self._wakeup_event.clear()
        self._start_mono = time.monotonic()
        self._buffer_direct_attempts = 0
        self._buffer_direct_next_mono = 0.0
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
                buffered = len(self._memory_buffer)
            self._final_pending_count = pending
            if pending > 0:
                # Informational, not an error: the rows aren't lost,
                # they just go out on the next start.
                self._warn(
                    f"ℹ️  Notification worker stopping with {pending} "
                    f"message(s) still pending in the persistent queue "
                    f"— they will deliver on the next start."
                )
            if buffered > 0:
                # F-067: memory-buffered rows have NO disk backing. On a
                # reload bounce the replacement worker adopts them (see
                # drain_memory_buffer); on a real process exit they are
                # gone, so say so instead of staying silent.
                self._warn(
                    f"⚠️  Notification worker stopping with {buffered} "
                    f"memory-buffered message(s) that never reached the "
                    f"store — lost unless a replacement worker adopts them."
                )
            self._stop_event.set()
            self._wakeup_event.set()
            self._worker_thread.join(timeout=2)

    # ----- reload handover (F-067) -----

    def drain_memory_buffer(self) -> List[Tuple[str, str, str, int]]:
        """Detach and return the in-memory buffered rows.

        Called on the OLD worker when a config reload bounces the
        notification subsystem, so the rows survive the swap instead of
        dying with the old object (they have no SQLite backing)."""
        with self._stores_lock:
            entries = self._memory_buffer
            self._memory_buffer = []
        return entries

    def handoff_memory_buffer_to(self, replacement: "NotificationWorker") -> None:
        """Move unclaimed rows and route late failures to ``replacement``."""
        if replacement is self:
            return
        with self._stores_lock:
            self._memory_handoff_worker = replacement
            entries = self._memory_buffer
            self._memory_buffer = []
        replacement.adopt_memory_buffer(entries)

    def adopt_memory_buffer(
            self, entries: Optional[List[Tuple[str, str, str, int]]]) -> None:
        """Prepend rows drained from a predecessor worker (age order kept)."""
        if not entries:
            return
        self._restore_memory_entries(entries, prepend=True)

    def _restore_memory_entries(
            self, entries: List[Tuple[str, str, str, int]], *,
            prepend: bool) -> None:
        """Put rows back locally, or forward them across a reload handoff."""
        replacement = None
        with self._stores_lock:
            replacement = self._memory_handoff_worker
            if replacement is None:
                if prepend:
                    self._memory_buffer = list(entries) + self._memory_buffer
                else:
                    self._memory_buffer.extend(entries)
                self._trim_memory_buffer()
        if replacement is not None:
            replacement._restore_memory_entries(entries, prepend=prepend)
        else:
            self._wakeup_event.set()

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
        if self._replay_memory_buffer(store) == 0:
            return
        # Apply backlog cap after the drain (P2 follow-up to the
        # in-memory cap): if the buffer grew large before the store
        # registered, the persisted side now needs the same trim.
        cap = self.config.notifications.max_pending
        if cap > 0:
            store.cap_pending_notifications(cap)
        self._wakeup_event.set()

    def _replay_memory_buffer(self, store: Optional[StatsStore] = None) -> int:
        """Try to persist buffered rows into ``store`` (default: first
        registered store). Returns the number of rows attempted.

        cubic P1 (round 1): rows buffered by ``send()`` when the store
        refused the insert (e.g. ``/var/lib/eneru`` unwritable at startup)
        were previously replayed ONLY by the next ``register_store`` call —
        which never comes once every monitor has registered, so the backlog
        sat in memory until process exit. ELI5: letters that bounced off a
        jammed mailbox slot went into a pocket, but nobody ever walked back
        to the mailbox. The worker loop now retries the pocket every tick,
        so a store that recovers (disk remounted, permissions fixed) gets
        the backlog. Rows that still fail are re-buffered; age order is
        preserved.
        """
        with self._stores_lock:
            if store is None and self._stores:
                store = self._stores[0]
            if store is None or not self._memory_buffer:
                return 0
            # Snapshot; do NOT clear-and-forget — only rows that actually
            # persist are dropped from the buffer.
            buffered = list(self._memory_buffer)
            self._memory_buffer = []
        leftovers: List[Tuple[str, str, str, int]] = []
        for body, notify_type, category, ts in buffered:
            row_id = store.enqueue_notification(
                body, notify_type, category, ts=ts,
            )
            if row_id is None:
                # enqueue returned None → store wasn't open or SQLite
                # raised; keep the row buffered for the next attempt.
                leftovers.append((body, notify_type, category, ts))
        if leftovers:
            # Prepend so age order is preserved against new sends. If reload
            # happened during the SQLite calls, forward to the replacement.
            self._restore_memory_entries(leftovers, prepend=True)
        if len(leftovers) < len(buffered):
            # Rows actually persisted: apply the same backlog cap the
            # register_store drain and the normal send() path enforce, so a
            # big recovery replay can't leave pending above max_pending.
            cap = self.config.notifications.max_pending
            if cap > 0:
                store.cap_pending_notifications(cap)
        return len(buffered)

    # ----- producer side -----

    def send(self, body: str, notify_type: str = "info",
             category: str = "general",
             store: Optional[StatsStore] = None,
             blocking: bool = False,
             require_persistent: bool = False) -> Optional[int]:
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
            blocking: DEPRECATED, ignored. The v5.1 worker honoured this
                for the ``--test-notifications`` CLI; v5.2's DB queue makes
                delivery asynchronous by design. Kept in the signature for
                back-compat only. ISS-063: slated for removal in the next
                MINOR release — callers must stop passing it.
            require_persistent: Return ``None`` without memory-buffering when
                SQLite cannot accept the row. Reports use this to roll back
                their cadence stamp and retry without creating duplicates.

        Returns the new notification id, or ``None`` if the send was
        buffered (no store yet) or the worker isn't initialized.
        """
        del blocking  # back-compat shim; the v5.2 queue is always async
        if not self._initialized:
            return None

        ts = int(time.time())
        target_store = store
        replacement = None
        entry = (body, notify_type, category, ts)
        with self._stores_lock:
            if target_store is None and self._stores:
                target_store = self._stores[0]
            if target_store is None:
                if require_persistent:
                    return None
                # Pre-store buffer: replayed verbatim (with original ts)
                # once register_store fires.
                replacement = self._memory_handoff_worker
                if replacement is None:
                    self._memory_buffer.append(entry)
                    self._trim_memory_buffer()
                    self._wakeup_event.set()
        if target_store is None:
            if replacement is not None:
                replacement._restore_memory_entries([entry], prepend=False)
            return None

        notification_id = target_store.enqueue_notification(
            body=body, notify_type=notify_type, category=category, ts=ts,
        )
        if notification_id is None:
            # The store exists but isn't open (e.g. /var/lib/eneru was
            # unwritable at startup), so it silently refused the row.
            # Think of a mailbox whose slot is jammed: dropping the letter
            # on the floor loses it forever. Instead we stuff it in the
            # same in-memory pocket the store-less path uses, so it gets
            # replayed once a store opens — the lossless guarantee holds.
            if require_persistent:
                return None
            self._restore_memory_entries([entry], prepend=False)
            if not getattr(self, "_enqueue_failed_warned", False):
                self._warn(
                    "⚠️  Notification store did not accept the message "
                    "(store not open); buffering in memory."
                )
                self._enqueue_failed_warned = True
            return None
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
            self._warn(
                f"⚠️  Notification memory buffer exceeded {cap} entries — "
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
                # cubic P1 (round 1): give memory-buffered rows a path back
                # into SQLite even when no further register_store ever fires.
                self._replay_memory_buffer()
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
                # Store-backed rows are drained; last resort for rows the
                # store refused (F-067) before going back to sleep.
                self._deliver_memory_buffer_direct()
                return

            store, ts, row_id, body, notify_type, attempts = candidate
            self._process_one(
                store=store, row_id=row_id, body=body,
                notify_type=notify_type, attempts=attempts,
            )

    def _deliver_memory_buffer_direct(self) -> None:
        """F-067 last resort: ship memory-buffered rows straight through
        Apprise when SQLite cannot take them.

        ELI5: letters that bounce off a jammed mailbox slot go into a pocket
        (``_memory_buffer``) and every tick someone walks them back to the
        mailbox (``_replay_memory_buffer``). But if the mailbox NEVER unjams
        (``/var/lib/eneru`` stays unwritable for the daemon's life), the
        letters used to sit in the pocket until process exit — silently lost
        despite the changelog's lossless promise. So when the mailbox keeps
        refusing, we hand-deliver: call Apprise directly and drop a row from
        the pocket only once delivery actually succeeded.

        Rules:
        - A registered-but-refusing store (the F-010 scenario) → deliver now.
        - No store registered at all → within ``_BUFFER_DIRECT_GRACE_SECS``
          of start() this is the normal startup register window, so hold the
          rows for the imminent ``register_store``; past the grace no store
          is coming, deliver directly.
        - One shared exponential backoff (same knobs as the per-row path) so
          an unreachable endpoint isn't hammered on every 1s tick.
        """
        with self._stores_lock:
            if not self._memory_buffer:
                return
            have_store = bool(self._stores)
            entry_count = len(self._memory_buffer)
        if not have_store and (
            time.monotonic() - self._start_mono < _BUFFER_DIRECT_GRACE_SECS
        ):
            return
        if time.monotonic() < self._buffer_direct_next_mono:
            return

        failed = False
        for _ in range(entry_count):  # oldest-first (buffer keeps age order)
            if self._stop_event.is_set():
                break
            with self._stores_lock:
                if not self._memory_buffer:
                    break
                # Claim before blocking in Apprise. Reload can safely transfer
                # the rest; this row is restored only if its delivery fails.
                entry = self._memory_buffer.pop(0)
            body, notify_type, _category, _ts = entry
            if self._send_via_apprise(body, notify_type):
                continue
            else:
                # Endpoint down/refusing — stop the sweep and back off
                # rather than burning one blocking Apprise call per row.
                self._restore_memory_entries([entry], prepend=True)
                failed = True
                break
        if failed:
            self._buffer_direct_attempts += 1
            base = max(0, int(self.config.notifications.retry_interval))
            cap = max(base, int(self.config.notifications.retry_backoff_max))
            shift = min(self._buffer_direct_attempts - 1, 20)
            wait_secs = min(base * (2 ** shift) if base > 0 else 0, cap)
            self._buffer_direct_next_mono = time.monotonic() + max(0, wait_secs)
        else:
            self._buffer_direct_attempts = 0
            self._buffer_direct_next_mono = 0.0

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
                f"⚡  **Brief Power Outage**\n"
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

        A head-of-queue cluster of backed-off rows must not starve newer
        due rows (CR P1): the previous ``limit=10`` made row 11+ invisible
        until the head drained, which never happened when the head was a
        poison message with ``max_attempts=0``. But scanning the full
        ``max_pending`` (10k) backlog every 1 s tick is wasteful (ISS-036).

        Reconcile both: only rows that have already FAILED carry a
        ``_backoff`` entry, so the number of currently-suppressed rows is
        exactly how far past the head we might have to look to find a due
        row. Fetch that many extra rows plus a small floor — bounded by
        the backlog cap so the worst case never exceeds the old behaviour,
        while the common case (few/no rows in backoff) scans only ~50.

        Returned tuple: ``(store, ts, id, body, notify_type, attempts)``.
        """
        now_mono = time.monotonic()
        best: Optional[Tuple] = None
        suppressed = sum(1 for na in self._backoff.values() if na > now_mono)
        # cubic P2: a non-positive max_pending means "no cap" (unlimited
        # backlog); treat it as the 10k default here, NOT a literal small
        # number — otherwise `max(50, -1)` would clamp the scan to 50 and
        # reintroduce the head-of-queue starvation this bound guards against.
        raw_pending_cap = int(self.config.notifications.max_pending or 10000)
        pending_cap = raw_pending_cap if raw_pending_cap > 0 else 10000
        scan_cap = min(max(50, suppressed + 50), pending_cap)
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
        # F-079: claim before calling Apprise. During a reload the old worker
        # can still be inside a slow send when its replacement starts; only the
        # worker that wins pending -> delivering may ship this row.
        if not store.claim_notification(row_id):
            return
        success = self._send_via_apprise(body, notify_type)
        key = (str(store.db_path), row_id)

        if success:
            store.mark_notification_sent(row_id)
            self._backoff.pop(key, None)
            return

        # claim_notification already incremented attempts atomically.
        new_attempts = attempts + 1

        max_attempts = self.config.notifications.max_attempts
        if max_attempts > 0 and new_attempts >= max_attempts:
            store.cancel_notification(row_id, "max_attempts")
            self._backoff.pop(key, None)
            return

        store.revert_claim(row_id)

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
        # Snapshot the instance once for a stable read within this call.
        instance = self._apprise_instance
        if not instance:
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

            return bool(instance.notify(**notify_kwargs))
        except Exception:
            return False

    def _send_via_apprise_bounded(self, body: str, notify_type: str,
                                  timeout: float = 5.0) -> bool:
        """Eager send bounded by a wall-clock timeout (M6).

        The lifecycle "Service Stopped" notification is shipped eagerly from the
        SIGTERM/SIGINT handler on the MAIN thread when no deferred delivery is
        available. ``apprise.notify()`` has no timeout, so a hung endpoint would
        block daemon exit -- violating the "shutdown must not wait on network"
        contract. Run the send on a short-lived daemon thread and give up after
        ``timeout`` seconds; on timeout the row stays pending and ships on the
        next start (the lossless guarantee). Mirrors the flush(timeout=5) budget.
        """
        result = {"ok": False}

        def _run():
            result["ok"] = self._send_via_apprise(body, notify_type)

        t = threading.Thread(target=_run, name="eneru-eager-notify", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return False  # timed out -> leave pending for the next start
        return result["ok"]

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
        self._prune_backoff(stores_snapshot)

    def _prune_backoff(self, stores_snapshot: List[StatsStore]) -> None:
        """Retain only ``_backoff`` entries whose ``(db_path, row_id)`` is
        still a pending row (ISS-036).

        Backoff keys are added in ``_process_one`` on a delivery failure
        but only removed there on success / ``max_attempts``. Rows
        cancelled out-of-band — by the backlog cap, coalescing, or TTL
        expiry — would otherwise leak their entry forever, growing the
        dict for the life of the process. Reconciling against the live
        pending set on the same once-a-minute cadence as the DB prune
        bounds the dict to the actual backlog.
        """
        if not self._backoff:
            return
        live = set()
        queried_paths = set()
        for store in stores_snapshot:
            ids = store.pending_notification_ids()
            if ids is None:
                # Transient error / store closed: we can't enumerate this
                # store's live rows, so leave its backoff entries alone
                # rather than wiping (and thereby resetting) their retry
                # timers on a hiccup.
                continue
            queried_paths.add(str(store.db_path))
            for row_id in ids:
                live.add((str(store.db_path), int(row_id)))
        # Only prune keys belonging to a store we successfully queried.
        stale = [
            key for key in self._backoff
            if key[0] in queried_paths and key not in live
        ]
        for key in stale:
            self._backoff.pop(key, None)

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
