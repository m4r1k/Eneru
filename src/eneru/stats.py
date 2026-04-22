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
from typing import Dict, List, Optional, Sequence, Tuple
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
# samples / agg_5min / agg_hourly / events / meta schema gains a column
# or table. See src/eneru/CLAUDE.md "Stats schema evolution".
SCHEMA_VERSION = 3

# Bucket sizes for aggregation tiers.
BUCKET_5MIN = 5 * 60
BUCKET_HOURLY = 60 * 60


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
    return (
        int(ts if ts is not None else time.time()),
        ups_data.get("ups.status", ""),
        _to_float(ups_data.get("battery.charge")),
        _to_float(ups_data.get("battery.runtime")),
        _to_float(ups_data.get("ups.load")),
        _to_float(ups_data.get("input.voltage")),
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
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        # Bound the writer's wait if a slow reader (TUI) is mid-query
        # on slow storage (SD card on a Raspberry Pi). 500 ms is well
        # under the 10 s flush interval, so a stalled reader can never
        # hold up the writer thread for longer than that bound.
        self._conn.execute("PRAGMA busy_timeout = 500")
        self._init_schema()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self.flush()
            except Exception:  # pragma: no cover -- defensive
                pass
            try:
                self._conn.close()
            except Exception:  # pragma: no cover -- defensive
                pass
            self._conn = None

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
            """)
            self._migrate_schema()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def _migrate_schema(self) -> None:
        """Apply additive schema migrations keyed off ``meta.schema_version``.

        See ``src/eneru/CLAUDE.md`` ("Stats schema evolution") for the
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

    def _safe_alter(self, table: str, column_def: str) -> None:
        """Idempotent ``ALTER TABLE ... ADD COLUMN``; ignores duplicates."""
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        except sqlite3.OperationalError:
            # Column already exists -- benign on retries / partial migrations.
            pass

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
            with self._conn:
                placeholders = ", ".join("?" for _ in SAMPLE_FIELDS)
                self._conn.executemany(
                    f"INSERT INTO samples ({', '.join(SAMPLE_FIELDS)}) "
                    f"VALUES ({placeholders})",
                    batch,
                )
            return len(batch)
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: flush failed: {e}")
            return 0

    def aggregate(self) -> Tuple[int, int]:
        """Roll samples into 5-min buckets, and 5-min into hourly buckets.

        Returns ``(rows_inserted_5min, rows_inserted_hourly)``.
        """
        if self._conn is None:
            return (0, 0)
        try:
            with self._conn:
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
        try:
            with self._conn:
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
            with self._conn:
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
            cur = self._conn.execute(
                "SELECT ts, event_type, detail FROM events "
                "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
                (int(start_ts), int(end_ts)),
            )
            return cur.fetchall()
        except (sqlite3.Error, OSError) as e:
            self._log_error_once(f"stats: query_events failed: {e}")
            return []

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

        tier = prefer_tier or self._pick_tier(start_ts, end_ts)
        try:
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
