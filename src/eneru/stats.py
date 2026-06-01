"""Per-UPS SQLite statistics: buffered writer + read API.

A lightweight, *always-on* metrics store. The hot path is
``buffer_sample()`` -- a constant-time append to an in-memory deque with
zero I/O. A separate :class:`StatsWriter` thread flushes the buffer to
SQLite every 10 s, then runs aggregation + purge every 5 min.

Failure isolation contract: every public method except :meth:`open`
catches ``sqlite3.Error`` and ``OSError`` and logs once with rate-limit.
A SQLite outage never raises into the daemon loop.
"""

import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote as urlquote


# Sample columns: 10 raw NUT metrics from spec 2.12 (battery.charge,
# battery.runtime, ups.load, input.voltage, output.voltage, battery.voltage,
# ups.temperature, input.frequency, output.frequency, ups.status) plus 3
# Eneru-derived state fields (depletion_rate, time_on_battery,
# connection_state) that are the foundation for the future API.
# Order is locked: appending only keeps INSERT tuples stable for migrations.
SAMPLE_FIELDS: Tuple[str, ...] = (
    "ts",                # epoch seconds
    "status",            # ups.status string
    "battery_charge",    # %
    "battery_runtime",   # seconds
    "ups_load",          # %
    "input_voltage",     # V
    "output_voltage",    # V
    "depletion_rate",    # %/min  (Eneru-derived)
    "time_on_battery",   # seconds since on-battery start (Eneru-derived)
    "connection_state",  # OK / GRACE_PERIOD / FAILED  (Eneru-derived)
    "battery_voltage",   # V                 (added v2)
    "ups_temperature",   # °C                (added v2)
    "input_frequency",   # Hz                (added v2)
    "output_frequency",  # Hz                (added v2)
)

# Bump and add a migration block in StatsStore._init_schema whenever the
# samples / agg_5min / agg_hourly / events / meta / notifications schema
# gains a column or table. See src/eneru/AGENTS.md "Stats schema evolution".
SCHEMA_VERSION = 5

# Bucket sizes for aggregation tiers.
BUCKET_5MIN = 5 * 60
BUCKET_HOURLY = 60 * 60

# Numeric sample columns that query_range may select. `metric` is interpolated
# into the SQL column position there, so this internal allowlist (L11) is
# defense-in-depth: callers already validate against status.HISTORY_METRICS, but
# a future caller that forgets cannot inject. Derived from SAMPLE_FIELDS so it
# stays in sync; the non-numeric/identity columns are excluded.
_QUERYABLE_METRICS = frozenset(SAMPLE_FIELDS) - {"ts", "status", "connection_state"}


def _to_float(value) -> Optional[float]:
    """Lenient float coercion. Returns ``None`` for empty / non-numeric."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> Optional[int]:
    f = _to_float(value)
    return int(f) if f is not None else None


def _to_input_voltage(value, *, ups_status: str) -> Optional[float]:
    """Sanitize ``input.voltage`` against on-line phantom zeros.

    A reading of 0 V is meaningful when the UPS is on battery (mains is
    actually gone, the inverter holds the output). On line, 0 V is a
    sensor glitch or poll race against a NUT driver mid-transition --
    keeping it pollutes the graph and drags the AVG/MIN aggregates. Drop
    only the latter case; outage history stays intact.

    The "is OB present" predicate matches the convention used by
    ``_handle_on_battery`` in ``monitor.py`` -- ``OL OB`` (a transient
    mid-transition status) is treated as "on battery", so its 0 V is
    kept. ``BOOST`` / ``TRIM`` / ``BYPASS`` etc. are AVR/bypass states
    *of* the on-line condition; they're excluded from the drop set
    below by the explicit ``"OL" in status`` check.
    """
    f = _to_float(value)
    if f is None:
        return None
    status = ups_status or ""
    if f <= 0.0 and "OL" in status and "OB" not in status and "FSD" not in status:
        return None
    return f


def _sample_from_ups_data(
    ups_data: Dict[str, str],
    *,
    depletion_rate: float = 0.0,
    time_on_battery: int = 0,
    connection_state: str = "OK",
    ts: Optional[int] = None,
) -> Tuple:
    """Project a raw upsc dict + state context into a SAMPLE_FIELDS tuple.

    Tuple positions are locked to the SAMPLE_FIELDS order so the same
    ``executemany`` works against any schema version reached via
    additive migrations.
    """
    status = ups_data.get("ups.status", "")
    return (
        int(ts if ts is not None else time.time()),
        status,
        _to_float(ups_data.get("battery.charge")),
        _to_float(ups_data.get("battery.runtime")),
        _to_float(ups_data.get("ups.load")),
        _to_input_voltage(ups_data.get("input.voltage"), ups_status=status),
        _to_float(ups_data.get("output.voltage")),
        float(depletion_rate or 0.0),
        int(time_on_battery or 0),
        connection_state or "OK",
        _to_float(ups_data.get("battery.voltage")),
        _to_float(ups_data.get("ups.temperature")),
        _to_float(ups_data.get("input.frequency")),
        _to_float(ups_data.get("output.frequency")),
    )


class StatsStore:
    """Per-UPS SQLite store with WAL, in-memory buffer, and tiered retention.

    Construction is cheap; call :meth:`open` once before use. The store
    is single-writer (the :class:`StatsWriter` thread); read methods are
    safe from any thread because each opens its own short-lived
    connection.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        retention_raw_hours: int = 24,
        retention_5min_days: int = 30,
        retention_hourly_days: int = 1825,
        buffer_maxlen: int = 100_000,
        logger=None,
    ):
        self.db_path = Path(db_path)
        self.retention_raw_hours = max(1, int(retention_raw_hours))
        self.retention_5min_days = max(1, int(retention_5min_days))
        self.retention_hourly_days = max(1, int(retention_hourly_days))
        self._buffer: deque = deque(maxlen=buffer_maxlen)
        self._buffer_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        # v5.2: serialize ALL _conn access across threads — the
        # notification-worker thread polls pending rows while the main
        # thread inserts + caps the queue, while the StatsWriter thread
        # runs flush/aggregate/purge every 10 s / 5 min, while the TUI
        # may call query_events / query_range concurrently. Python
        # 3.13's stricter sqlite3 binding raises "SystemError: error
        # return without exception set" when concurrent execute()s
        # share a connection (CPython issue #118172). WAL underneath
        # already serializes writes; this lock keeps the Python
        # wrapper consistent. The lock is acquired by every public
        # method that touches self._conn AFTER open() returns.
        self._db_lock = threading.Lock()
        self._logger = logger
        # Rate-limit error logging (per error message text).
        self._last_error_log: Dict[str, float] = {}
        self._error_log_interval = 300.0  # 5 minutes

    # ----- lifecycle -----

    def open(self) -> None:
        """Open the SQLite connection and ensure the schema exists.

        Creates the parent directory if missing. Pragmas: WAL mode for
        concurrent reads, NORMAL synchronous mode (safe with WAL),
        foreign_keys off (we have none).
        """
        # Defensive: pip installs don't run nfpm's directory entry, so
        # the daemon must be willing to create /var/lib/eneru itself.
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log_error_once(f"stats: mkdir {self.db_path.parent} failed: {e}")
            raise
        # Default deferred isolation: sqlite3 opens an implicit
        # transaction on first DML, and the `with self._conn:` blocks
        # below commit/rollback as expected. With isolation_level=None
        # the connection runs in autocommit mode and the `with` blocks
        # become no-ops, so executemany() would have committed every
        # row individually instead of batching the flush.
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            # Bound the writer's wait if a slow reader (TUI) is mid-query
            # on slow storage (SD card on a Raspberry Pi). 500 ms is well
            # under the 10 s flush interval, so a stalled reader can never
            # hold up the writer thread for longer than that bound.
            conn.execute("PRAGMA busy_timeout = 500")
            self._conn = conn
            self._init_schema()
        except Exception:
            self._conn = None
            try:
                conn.close()
            except Exception:
                pass
            raise

    def apply_reload(self, stats_config) -> bool:
        """Live-apply retention changes from a reloaded config.

        Retention windows are plain attributes read by the purge cycle, so
        updating them is safe at runtime. The DB path is NOT changed here (that
        needs a restart). Returns True.
        """
        r = stats_config.retention
        # Hold the store lock so the purge thread doesn't read a half-updated
        # set of the three retention windows.
        with self._db_lock:
            self.retention_raw_hours = max(1, int(r.raw_hours))
            self.retention_5min_days = max(1, int(r.agg_5min_days))
            self.retention_hourly_days = max(1, int(r.agg_hourly_days))
        return True

    @property
    def is_open(self) -> bool:
        """True when the store has a live DB connection (open, not yet closed)."""
        return self._conn is not None

    def close(self) -> None:
        try:
            self.flush()
        except Exception:  # pragma: no cover -- defensive
            pass
        with self._db_lock:
            conn = self._conn
            self._conn = None
            if conn is None:
                return
            try:
                conn.close()
            except Exception:  # pragma: no cover -- defensive
                pass

    # ----- schema -----

    def _init_schema(self) -> None:
        cols_samples = ",\n  ".join(
            f"{name} {dtype}" for name, dtype in (
                ("ts", "INTEGER NOT NULL"),
                ("status", "TEXT"),
                ("battery_charge", "REAL"),
                ("battery_runtime", "REAL"),
                ("ups_load", "REAL"),
                ("input_voltage", "REAL"),
                ("output_voltage", "REAL"),
                ("depletion_rate", "REAL"),
                ("time_on_battery", "INTEGER"),
                ("connection_state", "TEXT"),
                # v2 additions:
                ("battery_voltage", "REAL"),
                ("ups_temperature", "REAL"),
                ("input_frequency", "REAL"),
                ("output_frequency", "REAL"),
            )
        )
        agg_cols = """
            ts INTEGER PRIMARY KEY,
            battery_charge_avg REAL,
            battery_charge_min REAL,
            battery_charge_max REAL,
            battery_runtime_avg REAL,
            ups_load_avg REAL,
            ups_load_max REAL,
            input_voltage_avg REAL,
            input_voltage_min REAL,
            input_voltage_max REAL,
            samples_count INTEGER,
            -- v2 additions:
            output_voltage_avg REAL,
            battery_voltage_avg REAL,
            ups_temperature_avg REAL,
            ups_temperature_min REAL,
            ups_temperature_max REAL,
            input_frequency_avg REAL,
            output_frequency_avg REAL
        """
        with self._conn:
            self._conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS samples (
                    {cols_samples}
                );
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

                CREATE TABLE IF NOT EXISTS agg_5min (
                    {agg_cols}
                );

                CREATE TABLE IF NOT EXISTS agg_hourly (
                    {agg_cols}
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    notification_sent INTEGER DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- v4: persistent notification queue. The worker reads/writes
                -- through this table so messages survive process death and
                -- prolonged endpoint outages. Pending rows are NEVER pruned
                -- by retention TTL — only sent/cancelled rows are.
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    notify_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at INTEGER,
                    cancel_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_status_ts
                    ON notifications(status, ts);
            """)
            self._migrate_schema()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def _migrate_schema(self) -> None:
        """Apply additive schema migrations keyed off ``meta.schema_version``.

        See ``src/eneru/AGENTS.md`` ("Stats schema evolution") for the
        full pattern. Rules:

        1. Migrations are append-only — never modify a previous block.
        2. Each ``ALTER TABLE`` is wrapped so duplicate columns are a
           no-op (idempotent on retries / partially-migrated DBs).
        3. ``meta.schema_version`` is bumped *after* the migrations
           succeed so a crash mid-migration is replayed safely.
        """
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        # No row -> brand-new DB; CREATE TABLE above already includes
        # every current column, so no ALTERs are needed.
        if cur is None:
            return
        try:
            current = int(cur[0])
        except (TypeError, ValueError):
            current = 1

        if current < 2:
            # v1 -> v2: 4 raw NUT metric columns on samples; matching
            # *_avg (and *_min/*_max for temperature) on the agg tables;
            # plus output_voltage_avg that closes the long-standing gap
            # in _agg_column_for.
            v2_samples = (
                "battery_voltage REAL",
                "ups_temperature REAL",
                "input_frequency REAL",
                "output_frequency REAL",
            )
            v2_agg = (
                "output_voltage_avg REAL",
                "battery_voltage_avg REAL",
                "ups_temperature_avg REAL",
                "ups_temperature_min REAL",
                "ups_temperature_max REAL",
                "input_frequency_avg REAL",
                "output_frequency_avg REAL",
            )
            for col in v2_samples:
                self._safe_alter("samples", col)
            for table in ("agg_5min", "agg_hourly"):
                for col in v2_agg:
                    self._safe_alter(table, col)

        if current < 3:
            # v2 -> v3: events.notification_sent (1 = logged + notified,
            # 0 = logged but suppressed). Default 1 for backfilled rows
            # so historical events stay queryable without losing the
            # "yes, this fired before suppression existed" interpretation.
            # Lets users audit muted events:
            #   SELECT event_type, COUNT(*) FROM events
            #   WHERE notification_sent = 0 GROUP BY event_type;
            self._safe_alter("events",
                             "notification_sent INTEGER DEFAULT 1")

        if current < 4:
            # v3 -> v4: persistent notification queue. New table + index;
            # no ALTERs to existing tables. Idempotent via CREATE IF NOT
            # EXISTS so a partially-migrated DB heals on next open.
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    notify_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at INTEGER,
                    cancel_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_status_ts
                    ON notifications(status, ts);
            """)

        if current < 5:
            # v4 -> v5: give events a stable, never-reused id
            # (INTEGER PRIMARY KEY AUTOINCREMENT). SQLite can't ALTER ADD a
            # PRIMARY KEY column, so rebuild the table, preserving each row's
            # identity as id = old rowid. AUTOINCREMENT guarantees a deleted
            # event's id is never handed to a later row -- that's what makes the
            # API's delete-by-id safe.
            self._migrate_events_to_v5()

    def _migrate_events_to_v5(self) -> None:
        """Rebuild ``events`` with a stable id column.

        DDL in ``executescript`` can commit outside the surrounding context
        manager, so the destructive rebuild uses an explicit savepoint. That
        keeps the old table intact if a crash or SQLite error lands between the
        copy and the rename.
        """
        self._conn.execute("SAVEPOINT migrate_events_v5")
        try:
            if self._events_has_v5_id():
                # Recover a DB opened after an older unsafe rebuild died after
                # copying rows and dropping events, but before the final rename.
                if self._table_exists("events_v5_new"):
                    event_count = self._conn.execute(
                        "SELECT COUNT(*) FROM events"
                    ).fetchone()[0]
                    scratch_count = self._conn.execute(
                        "SELECT COUNT(*) FROM events_v5_new"
                    ).fetchone()[0]
                    if event_count == 0 and scratch_count > 0:
                        self._conn.execute("DROP TABLE events")
                        self._conn.execute(
                            "ALTER TABLE events_v5_new RENAME TO events"
                        )
                    else:
                        self._conn.execute("DROP TABLE events_v5_new")
            else:
                self._conn.execute("DROP TABLE IF EXISTS events_v5_new")
                self._conn.execute("""
                    CREATE TABLE events_v5_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        detail TEXT,
                        notification_sent INTEGER DEFAULT 1
                    )
                """)
                self._conn.execute("""
                    INSERT INTO events_v5_new
                        (id, ts, event_type, detail, notification_sent)
                    SELECT rowid, ts, event_type, detail, notification_sent
                    FROM events
                """)
                self._conn.execute("DROP TABLE events")
                self._conn.execute("ALTER TABLE events_v5_new RENAME TO events")

            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            # Pin the AUTOINCREMENT high-water mark to the max preserved id so a
            # later delete of the highest row can never let a new event reuse it
            # (belt-and-suspenders alongside RENAME's sqlite_sequence fixup).
            self._conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name, seq) "
                "SELECT 'events', COALESCE(MAX(id), 0) FROM events"
            )
            self._conn.execute("RELEASE SAVEPOINT migrate_events_v5")
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT migrate_events_v5")
            self._conn.execute("RELEASE SAVEPOINT migrate_events_v5")
            raise

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _events_has_v5_id(self) -> bool:
        return any(
            row[1] == "id" and row[5] == 1
            for row in self._conn.execute("PRAGMA table_info(events)")
        )

    def _safe_alter(self, table: str, column_def: str) -> None:
        """Idempotent ``ALTER TABLE ... ADD COLUMN``; ignores duplicates."""
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        except sqlite3.OperationalError as exc:
            # Column already exists -- benign on retries / partial migrations.
            if "duplicate column name" in str(exc).lower():
                return
            raise

    # ----- hot path: zero-I/O sample buffering -----

    def buffer_sample(
        self,
        ups_data: Dict[str, str],
        *,
        depletion_rate: float = 0.0,
        time_on_battery: int = 0,
        connection_state: str = "OK",
        ts: Optional[int] = None,
    ) -> None:
        """Append one sample to the in-memory buffer. Constant time, no I/O."""
        sample = _sample_from_ups_data(
            ups_data,
            depletion_rate=depletion_rate,
            time_on_battery=time_on_battery,
            connection_state=connection_state,
            ts=ts,
        )
        with self._buffer_lock:
            self._buffer.append(sample)

    # ----- writer-thread operations -----

    def flush(self) -> int:
        """Persist the buffered samples in one transaction.

        Returns the number of rows actually written (0 if the buffer was
        empty or the DB is unavailable).
        """
        if self._conn is None:
            return 0
        with self._buffer_lock:
            if not self._buffer:
                return 0
            batch: List[Tuple] = list(self._buffer)
            self._buffer.clear()
        try:
            with self._db_lock:
                conn = self._conn
                if conn is None:
                    raise sqlite3.Error("connection closed")
                placeholders = ", ".join("?" for _ in SAMPLE_FIELDS)
                with conn:
                    conn.executemany(
                        f"INSERT INTO samples ({', '.join(SAMPLE_FIELDS)}) "
                        f"VALUES ({placeholders})",
                        batch,
                    )
            return len(batch)
        except (sqlite3.Error, OSError) as e:
            with self._buffer_lock:
                for sample in reversed(batch):
                    self._buffer.appendleft(sample)
            self._log_error_once(f"stats: flush failed: {e}")
            return 0

    def aggregate(self) -> Tuple[int, int]:
        """Roll samples into 5-min buckets, and 5-min into hourly buckets.

        Returns ``(rows_inserted_5min, rows_inserted_hourly)``.
        """
        if self._conn is None:
            return (0, 0)
        try:
            with self._db_lock, self._conn:
                inserted_5 = self._conn.execute(f"""
                    INSERT OR REPLACE INTO agg_5min (
                        ts,
                        battery_charge_avg, battery_charge_min, battery_charge_max,
                        battery_runtime_avg,
                        ups_load_avg, ups_load_max,
                        input_voltage_avg, input_voltage_min, input_voltage_max,
                        samples_count,
                        output_voltage_avg, battery_voltage_avg,
                        ups_temperature_avg, ups_temperature_min, ups_temperature_max,
                        input_frequency_avg, output_frequency_avg
                    )
                    SELECT
                        (ts / {BUCKET_5MIN}) * {BUCKET_5MIN} AS bucket,
                        AVG(battery_charge), MIN(battery_charge), MAX(battery_charge),
                        AVG(battery_runtime),
                        AVG(ups_load), MAX(ups_load),
                        AVG(input_voltage), MIN(input_voltage), MAX(input_voltage),
                        COUNT(*),
                        AVG(output_voltage), AVG(battery_voltage),
                        AVG(ups_temperature), MIN(ups_temperature), MAX(ups_temperature),
                        AVG(input_frequency), AVG(output_frequency)
                    FROM samples
                    GROUP BY bucket
                """).rowcount
                inserted_h = self._conn.execute(f"""
                    INSERT OR REPLACE INTO agg_hourly (
                        ts,
                        battery_charge_avg, battery_charge_min, battery_charge_max,
                        battery_runtime_avg,
                        ups_load_avg, ups_load_max,
                        input_voltage_avg, input_voltage_min, input_voltage_max,
                        samples_count,
                        output_voltage_avg, battery_voltage_avg,
                        ups_temperature_avg, ups_temperature_min, ups_temperature_max,
                        input_frequency_avg, output_frequency_avg
                    )
                    SELECT
                        (ts / {BUCKET_HOURLY}) * {BUCKET_HOURLY} AS bucket,
                        AVG(battery_charge_avg),
                        MIN(battery_charge_min), MAX(battery_charge_max),
                        AVG(battery_runtime_avg),
                        AVG(ups_load_avg), MAX(ups_load_max),
                        AVG(input_voltage_avg),
                        MIN(input_voltage_min), MAX(input_voltage_max),
                        SUM(samples_count),
                        AVG(output_voltage_avg), AVG(battery_voltage_avg),
                        AVG(ups_temperature_avg),
                        MIN(ups_temperature_min), MAX(ups_temperature_max),
                        AVG(input_frequency_avg), AVG(output_frequency_avg)
                    FROM agg_5min
                    GROUP BY bucket
                """).rowcount
            return (max(0, inserted_5), max(0, inserted_h))
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: aggregate failed: {e}")
            return (0, 0)

    def purge(self) -> Tuple[int, int, int]:
        """Apply retention. Returns ``(samples, agg_5min, agg_hourly)`` deleted."""
        if self._conn is None:
            return (0, 0, 0)
        now = int(time.time())
        cutoff_raw = now - self.retention_raw_hours * 3600
        cutoff_5min = now - self.retention_5min_days * 86400
        cutoff_hourly = now - self.retention_hourly_days * 86400
        # M5: align the raw/5-min cutoffs DOWN to the next-tier bucket boundary
        # so purge only ever deletes WHOLE buckets' worth of rows. Otherwise it
        # trims the early rows of the bucket straddling the cutoff, and the next
        # aggregate() re-derives that already-finalized bucket from the reduced
        # set -- overwriting its avg/min/max with wrong values that then
        # propagate into the hourly tier. Keeping at most one extra bucket of raw
        # data (~5 min) / 5-min data (~1 h) is a negligible retention cost.
        cutoff_raw = (cutoff_raw // BUCKET_5MIN) * BUCKET_5MIN
        cutoff_5min = (cutoff_5min // BUCKET_HOURLY) * BUCKET_HOURLY
        try:
            with self._db_lock, self._conn:
                deleted_raw = self._conn.execute(
                    "DELETE FROM samples WHERE ts < ?", (cutoff_raw,),
                ).rowcount
                deleted_5min = self._conn.execute(
                    "DELETE FROM agg_5min WHERE ts < ?", (cutoff_5min,),
                ).rowcount
                deleted_hourly = self._conn.execute(
                    "DELETE FROM agg_hourly WHERE ts < ?", (cutoff_hourly,),
                ).rowcount
                self._conn.execute(
                    "DELETE FROM events WHERE ts < ?", (cutoff_hourly,),
                )
            return (
                max(0, deleted_raw),
                max(0, deleted_5min),
                max(0, deleted_hourly),
            )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: purge failed: {e}")
            return (0, 0, 0)

    # ----- events API -----

    def log_event(self, event_type: str, detail: str = "",
                  ts: Optional[int] = None,
                  *, notification_sent: bool = True) -> None:
        """Insert one event row. Safe to call from any thread.

        ``notification_sent`` (v3+) records whether the daemon dispatched
        a notification for this event. Pass ``False`` when the event
        was logged but the notification was suppressed (via
        ``notifications.suppress`` or hysteresis debounce). Logs are
        sacred -- this column lets users audit what was muted:

            SELECT event_type, COUNT(*) FROM events
            WHERE notification_sent = 0 GROUP BY event_type;
        """
        if self._conn is None:
            return
        ts = int(ts if ts is not None else time.time())
        try:
            with self._db_lock, self._conn:
                self._conn.execute(
                    "INSERT INTO events (ts, event_type, detail, "
                    "notification_sent) VALUES (?, ?, ?, ?)",
                    (ts, str(event_type), str(detail),
                     1 if notification_sent else 0),
                )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: log_event failed: {e}")

    def query_events(self, start_ts: int, end_ts: int) -> List[Tuple]:
        """Return events in ``[start_ts, end_ts]`` ascending by ts."""
        if self._conn is None:
            return []
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT ts, event_type, detail FROM events "
                    "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
                    (int(start_ts), int(end_ts)),
                )
                return cur.fetchall()
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: query_events failed: {e}")
            return []

    def query_recent_events(
        self,
        *,
        end_ts: int,
        limit: int,
        start_ts: Optional[int] = None,
        include_types: Optional[set] = None,
        exclude_types: Optional[set] = None,
        include_id: bool = False,
        before_id: Optional[int] = None,
    ) -> List[Tuple]:
        """Return recent events ascending by ts without loading full history.

        * ``end_ts`` — inclusive upper bound (``ts <= end_ts``); "load older"
          paging sets this to the oldest timestamp already shown.
        * ``start_ts`` — inclusive lower bound (``ts >= start_ts``).
        * ``include_id`` — prepend the row's ``id`` to each returned tuple (off by
          default, so existing 3-tuple callers are unaffected). The id is unique
          only within this one per-UPS DB; the aggregating layer source-qualifies
          it and the dashboard de-dups by ``(source, id)`` across pages.
        * ``before_id`` — with ``end_ts``, use a strict same-second upper bound
          ``(ts, id) < (end_ts, before_id)`` for source-qualified paging.
        """
        if self._conn is None:
            return []
        limit = max(1, int(limit))
        if before_id is None:
            clauses = ["ts <= ?"]
            params: List[Any] = [int(end_ts)]
        else:
            clauses = ["(ts < ? OR (ts = ? AND id < ?))"]
            params = [int(end_ts), int(end_ts), int(before_id)]
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(int(start_ts))
        if include_types:
            placeholders = ", ".join("?" for _ in include_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(str(item) for item in include_types))
        if exclude_types:
            placeholders = ", ".join("?" for _ in exclude_types)
            clauses.append(f"event_type NOT IN ({placeholders})")
            params.extend(sorted(str(item) for item in exclude_types))
        params.append(limit)
        # ``rowid`` is the implicit insertion-order key — using it as
        # the tiebreaker makes "latest N events" deterministic when
        # multiple events share the same second (notification fanout,
        # rapid trigger flap), so paginated reads don't return
        # different subsets across calls.
        cols = "id, ts, event_type, detail" if include_id else \
            "ts, event_type, detail"
        query = (
            f"SELECT {cols} FROM events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC, id DESC LIMIT ?"
        )
        try:
            with self._db_lock:
                cur = self._conn.execute(query, params)
                return list(reversed(cur.fetchall()))
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: query_recent_events failed: {e}")
            return []

    def delete_events(self, items) -> int:
        """Delete events by ``(id, ts, event_type)``. Returns the count removed.

        Each row is matched on all three columns, not just ``id``: the id alone
        is authoritative (AUTOINCREMENT never reuses one), but the ts/type guard
        means a stale client request can only ever delete the exact row it saw —
        a mismatch deletes nothing (counts as 0, not an error). Idempotent on
        duplicates, and atomic: any SQLite error rolls the whole batch back.
        ``items`` is an iterable of ``(id, ts, event_type)``.
        """
        if self._conn is None:
            return 0
        seen = set()
        rows = []
        for it in items:
            key = (int(it[0]), int(it[1]), str(it[2]))
            if key not in seen:
                seen.add(key)
                rows.append(key)
        if not rows:
            return 0
        try:
            count = 0
            with self._db_lock, self._conn:
                for event_id, ts, event_type in rows:
                    cur = self._conn.execute(
                        "DELETE FROM events WHERE id = ? AND ts = ? "
                        "AND event_type = ?",
                        (event_id, ts, event_type),
                    )
                    count += cur.rowcount
            return count
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: delete_events failed: {e}")
            return 0

    # ----- notification queue (v4+) -----

    def enqueue_notification(self, body: str, notify_type: str,
                             category: str,
                             ts: Optional[int] = None) -> Optional[int]:
        """Persist a pending notification. Returns the new row id, or
        ``None`` if the store isn't open (caller should fall back).

        Safe to call from any thread (serialized via ``_db_lock``).
        """
        if self._conn is None:
            return None
        ts = int(ts if ts is not None else time.time())
        try:
            with self._db_lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO notifications "
                    "(ts, body, notify_type, category) "
                    "VALUES (?, ?, ?, ?)",
                    (ts, str(body), str(notify_type), str(category)),
                )
                return int(cur.lastrowid)
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: enqueue_notification failed: {e}")
            return None

    def next_pending_notifications(self,
                                   limit: int = 10,
                                   offset: int = 0) -> List[Tuple]:
        """Return up to ``limit`` oldest pending rows starting at
        ``offset``, as ``(ts, id, body, notify_type, attempts, category)``
        tuples. Order: ts ASC then id ASC for deterministic FIFO across
        same-ts ties.

        ``offset`` lets the worker paginate when the head of the queue
        is occupied by backoff-delayed rows, so newer due rows aren't
        starved (CR P1).
        """
        if self._conn is None:
            return []
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT ts, id, body, notify_type, attempts, category "
                    "FROM notifications WHERE status='pending' "
                    "ORDER BY ts ASC, id ASC LIMIT ? OFFSET ?",
                    (int(limit), int(offset)),
                )
                return cur.fetchall()
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: next_pending_notifications failed: {e}"
            )
            return []

    def find_pending_by_category(self, category: str,
                                 since_ts: Optional[int] = None
                                 ) -> List[Tuple]:
        """Return pending rows with the given category as
        ``(id, ts, body, notify_type)`` tuples. Used by the worker's
        coalescing pass (Slice 4): scan for an open on-battery / on-line
        pair to fold into one summary."""
        if self._conn is None:
            return []
        try:
            with self._db_lock:
                if since_ts is None:
                    cur = self._conn.execute(
                        "SELECT id, ts, body, notify_type FROM notifications "
                        "WHERE status='pending' AND category=? "
                        "ORDER BY ts ASC",
                        (str(category),),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT id, ts, body, notify_type FROM notifications "
                        "WHERE status='pending' AND category=? AND ts>=? "
                        "ORDER BY ts ASC",
                        (str(category), int(since_ts)),
                    )
                return cur.fetchall()
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: find_pending_by_category failed: {e}"
            )
            return []

    def mark_notification_sent(self, notification_id: int,
                               sent_at: Optional[int] = None) -> None:
        """Mark a notification as delivered."""
        if self._conn is None:
            return
        sent_at = int(sent_at if sent_at is not None else time.time())
        try:
            with self._db_lock, self._conn:
                self._conn.execute(
                    "UPDATE notifications SET status='sent', sent_at=? "
                    "WHERE id=?",
                    (sent_at, int(notification_id)),
                )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: mark_notification_sent failed: {e}"
            )

    def mark_notification_attempt(self, notification_id: int) -> None:
        """Increment a notification's attempt counter (it stays pending
        — the worker will retry on the next loop iteration)."""
        if self._conn is None:
            return
        try:
            with self._db_lock, self._conn:
                self._conn.execute(
                    "UPDATE notifications SET attempts=attempts+1 "
                    "WHERE id=?",
                    (int(notification_id),),
                )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: mark_notification_attempt failed: {e}"
            )

    def cancel_notification(self, notification_id: int,
                            reason: str) -> None:
        """Mark a notification as cancelled (won't retry, eligible for
        TTL pruning). Reason is one of ``max_attempts``, ``too_old``,
        ``backlog_overflow``, ``coalesced``, ``superseded``."""
        if self._conn is None:
            return
        try:
            with self._db_lock, self._conn:
                self._conn.execute(
                    "UPDATE notifications SET status='cancelled', "
                    "cancel_reason=? WHERE id=?",
                    (str(reason), int(notification_id)),
                )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: cancel_notification failed: {e}"
            )

    def pending_notification_count(self) -> int:
        """Return the number of pending rows in this store. Used by
        ``flush()`` to know when the queue has drained."""
        if self._conn is None:
            return 0
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE status='pending'"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: pending_notification_count failed: {e}"
            )
            return 0

    def cap_pending_notifications(self, max_pending: int) -> int:
        """Enforce the backlog cap. If pending count exceeds
        ``max_pending``, cancel the oldest rows down to the cap with
        ``cancel_reason='backlog_overflow'``. Returns number cancelled.

        Pass 0 / negative to disable (no cap)."""
        if self._conn is None or max_pending <= 0:
            return 0
        try:
            with self._db_lock, self._conn:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM notifications "
                    "WHERE status='pending'"
                )
                row = cur.fetchone()
                pending = int(row[0]) if row else 0
                excess = pending - int(max_pending)
                if excess <= 0:
                    return 0
                # Cancel the `excess` oldest pending rows.
                self._conn.execute(
                    "UPDATE notifications SET status='cancelled', "
                    "cancel_reason='backlog_overflow' "
                    "WHERE id IN ("
                    "  SELECT id FROM notifications WHERE status='pending' "
                    "  ORDER BY ts ASC, id ASC LIMIT ?"
                    ")",
                    (int(excess),),
                )
                return excess
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: cap_pending_notifications failed: {e}"
            )
            return 0

    def prune_old_notifications(self, retention_days: int,
                                max_age_days: int = 0) -> Tuple[int, int]:
        """Two-step cleanup:

        1. ``DELETE`` rows in (``sent``, ``cancelled``) older than
           ``retention_days``. The sent log isn't an audit log; that's
           ``events``. Default 7d keeps the recent flush history for
           debugging without unbounded growth.
        2. ``UPDATE`` pending rows older than ``max_age_days`` to
           ``cancelled`` with ``cancel_reason='too_old'``. Skipped when
           ``max_age_days <= 0`` (pending lives forever — the panic-attack
           guarantee). Default 30d means a month-long sabbatical still
           delivers; longer than that is probably stale anyway.

        Returns ``(deleted, expired)`` for logging."""
        if self._conn is None:
            return (0, 0)
        cutoff_sent = int(time.time()) - max(1, int(retention_days)) * 86400
        deleted = 0
        expired = 0
        try:
            with self._db_lock, self._conn:
                cur = self._conn.execute(
                    "DELETE FROM notifications "
                    "WHERE status IN ('sent','cancelled') AND ts < ?",
                    (cutoff_sent,),
                )
                deleted = cur.rowcount or 0
                if max_age_days > 0:
                    cutoff_pending = (
                        int(time.time()) - int(max_age_days) * 86400
                    )
                    cur = self._conn.execute(
                        "UPDATE notifications SET status='cancelled', "
                        "cancel_reason='too_old' "
                        "WHERE status='pending' AND ts < ?",
                        (cutoff_pending,),
                    )
                    expired = cur.rowcount or 0
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(
                f"stats: prune_old_notifications failed: {e}"
            )
        return (deleted, expired)

    # ----- generic meta key/value store -----

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the ``meta`` table. Used by Slice 3 to
        track ``last_seen_version`` for the pip-path lifecycle classifier."""
        if self._conn is None:
            return None
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT value FROM meta WHERE key=?", (str(key),),
                )
                row = cur.fetchone()
                return str(row[0]) if row else None
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: get_meta failed: {e}")
            return None

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to the ``meta`` table (insert-or-replace)."""
        if self._conn is None:
            return
        try:
            with self._db_lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (str(key), str(value)),
                )
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: set_meta failed: {e}")

    # ----- metric query API -----

    def query_range(
        self,
        metric: str,
        start_ts: int,
        end_ts: int,
        *,
        prefer_tier: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        """Return ``[(ts, value)]`` for a metric across a time window.

        Picks the smallest-resolution tier that still fits the window:
        samples (24 h), agg_5min (30 d), agg_hourly (5 y). Override with
        ``prefer_tier``.

        ``metric`` is one of: ``battery_charge``, ``battery_runtime``,
        ``ups_load``, ``input_voltage``, ``output_voltage``,
        ``depletion_rate``.
        """
        if self._conn is None:
            return []

        # L11: reject any metric not on the internal allowlist before it reaches
        # the interpolated column position.
        if metric not in _QUERYABLE_METRICS:
            return []

        tier = prefer_tier or self._pick_tier(start_ts, end_ts)
        try:
            with self._db_lock:
                if tier == "samples":
                    column = metric
                    cur = self._conn.execute(
                        f"SELECT ts, {column} FROM samples "
                        f"WHERE ts BETWEEN ? AND ? AND {column} IS NOT NULL "
                        "ORDER BY ts ASC",
                        (int(start_ts), int(end_ts)),
                    )
                else:
                    table = "agg_5min" if tier == "agg_5min" else "agg_hourly"
                    column = self._agg_column_for(metric)
                    cur = self._conn.execute(
                        f"SELECT ts, {column} FROM {table} "
                        f"WHERE ts BETWEEN ? AND ? AND {column} IS NOT NULL "
                        "ORDER BY ts ASC",
                        (int(start_ts), int(end_ts)),
                    )
                return [(int(r[0]), float(r[1])) for r in cur.fetchall()]
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: query_range failed: {e}")
            return []

    @staticmethod
    def _pick_tier(start_ts: int, end_ts: int) -> str:
        span = max(1, end_ts - start_ts)
        if span <= 24 * 3600:
            return "samples"
        if span <= 30 * 86400:
            return "agg_5min"
        return "agg_hourly"

    @staticmethod
    def _agg_column_for(metric: str) -> str:
        """Map a raw-metric name to its ``*_avg`` column on the agg tables.

        ``depletion_rate``, ``time_on_battery`` and ``connection_state``
        are not aggregated (state-shaped, not signal-shaped) and fall
        through to a column that doesn't exist on agg tables -- callers
        receive an empty result, which is the right behavior.
        """
        avg_map = {
            "battery_charge": "battery_charge_avg",
            "battery_runtime": "battery_runtime_avg",
            "ups_load": "ups_load_avg",
            "input_voltage": "input_voltage_avg",
            "output_voltage": "output_voltage_avg",
            "battery_voltage": "battery_voltage_avg",
            "ups_temperature": "ups_temperature_avg",
            "input_frequency": "input_frequency_avg",
            "output_frequency": "output_frequency_avg",
        }
        return avg_map.get(metric, metric)

    # ----- read-only API for the TUI -----

    @classmethod
    def open_readonly(cls, db_path: Path) -> Optional[sqlite3.Connection]:
        """Open a read-only ``sqlite3.Connection`` (URI mode=ro).

        Returns ``None`` if the file doesn't exist; otherwise the
        connection is the caller's responsibility to close. Designed for
        the TUI to query metrics without contending for the writer.
        """
        path = Path(db_path)
        if not path.exists():
            return None
        # urlquote so a path containing '?' or '#' (legal on POSIX
        # filesystems, illegal in a SQLite URI without escaping) doesn't
        # truncate the path or get parsed as the URI's query / fragment.
        uri = f"file:{urlquote(str(path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        # Same bound as the writer connection: a slow query against the
        # writer's WAL can't stall the TUI refresh for more than 500 ms.
        conn.execute("PRAGMA busy_timeout = 500")
        return conn

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "StatsStore":
        """Wrap an existing SQLite connection without taking ownership."""
        store = cls(Path(":memory:"))
        # N5: __init__ does NOT open a connection (self._conn stays None until
        # open()), so there is no throw-away handle to close here -- the previous
        # store._conn.close() always raised AttributeError into a swallowed
        # except. Just rebind to the caller-owned connection.
        store._conn = conn
        return store

    # ----- error rate-limiting -----

    def _log_error_once(self, message: str) -> None:
        now = time.time()
        last = self._last_error_log.get(message, 0.0)
        if now - last < self._error_log_interval:
            return
        self._last_error_log[message] = now
        if self._logger is not None:
            try:
                self._logger.log(message)
            except Exception:  # pragma: no cover
                pass


class StatsWriter(threading.Thread):
    """Background thread: flushes buffer every 10 s, aggregates+purges every 5 min.

    All work is wrapped in a try/except that logs once per error message
    -- a misbehaving SQLite never raises into the daemon's main loop.
    """

    def __init__(
        self,
        store: StatsStore,
        stop_event: threading.Event,
        *,
        flush_interval: float = 10.0,
        maintenance_interval: float = 300.0,
        log_prefix: str = "",
    ):
        super().__init__(name="stats-writer", daemon=True)
        self._store = store
        self._stop_event = stop_event
        self._flush_interval = flush_interval
        self._maintenance_interval = maintenance_interval
        self._log_prefix = log_prefix
        self._last_maintenance = time.monotonic()

    def run(self) -> None:  # pragma: no cover -- exercised by integration test
        try:
            while not self._stop_event.is_set():
                try:
                    self._store.flush()
                    if (time.monotonic() - self._last_maintenance
                            >= self._maintenance_interval):
                        self._store.aggregate()
                        self._store.purge()
                        self._last_maintenance = time.monotonic()
                except Exception:
                    # Defensive: every error path inside the store already
                    # logs once and swallows; this is a final guard.
                    pass
                self._stop_event.wait(self._flush_interval)
        finally:
            try:
                self._store.flush()
            except Exception:
                pass
