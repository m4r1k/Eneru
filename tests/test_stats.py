"""Tests for the per-UPS SQLite stats store + writer thread."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from eneru import (
    StatsConfig,
    StatsRetentionConfig,
    StatsStore,
    StatsWriter,
)
from eneru.stats import (
    BUCKET_5MIN,
    BUCKET_HOURLY,
    SAMPLE_FIELDS,
    SCHEMA_VERSION,
)


# Sample dict roughly matching what _save_state passes in.
SAMPLE_UPS_DATA = {
    "ups.status": "OL CHRG",
    "battery.charge": "85",
    "battery.runtime": "1200",
    "ups.load": "30",
    "input.voltage": "230.5",
    "output.voltage": "230.0",
}


@pytest.fixture
def store(tmp_path):
    s = StatsStore(tmp_path / "test.db")
    s.open()
    yield s
    s.close()


# ===========================================================================
# Schema + lifecycle
# ===========================================================================

class TestSchema:

    @pytest.mark.unit
    def test_open_creates_database_file(self, tmp_path):
        path = tmp_path / "x" / "y" / "test.db"
        s = StatsStore(path)
        s.open()
        try:
            assert path.exists()
        finally:
            s.close()

    @pytest.mark.unit
    def test_open_creates_parent_directory(self, tmp_path):
        # Defensive: pip installs need this.
        path = tmp_path / "nested" / "stats" / "test.db"
        assert not path.parent.exists()
        s = StatsStore(path)
        s.open()
        try:
            assert path.parent.is_dir()
        finally:
            s.close()

    @pytest.mark.unit
    def test_schema_tables_exist(self, store):
        cur = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert {"samples", "agg_5min", "agg_hourly", "events", "meta"} <= tables

    @pytest.mark.unit
    def test_schema_version_recorded(self, store):
        cur = store._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        )
        assert int(cur.fetchone()[0]) == SCHEMA_VERSION

    @pytest.mark.unit
    def test_samples_columns_match_sample_fields(self, store):
        cur = store._conn.execute("PRAGMA table_info(samples)")
        col_names = [r[1] for r in cur.fetchall()]
        assert col_names == list(SAMPLE_FIELDS)

    @pytest.mark.unit
    def test_wal_mode_enabled(self, store):
        cur = store._conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"

    @pytest.mark.unit
    def test_synchronous_mode_normal(self, store):
        cur = store._conn.execute("PRAGMA synchronous")
        # NORMAL = 1
        assert cur.fetchone()[0] == 1

    @pytest.mark.unit
    def test_double_open_is_idempotent_on_schema(self, tmp_path):
        # Re-opening must not error and must keep the schema intact.
        path = tmp_path / "test.db"
        s1 = StatsStore(path)
        s1.open()
        s1.close()
        s2 = StatsStore(path)
        s2.open()
        cur = s2._conn.execute("SELECT COUNT(*) FROM samples")
        assert cur.fetchone()[0] == 0
        s2.close()

    @pytest.mark.unit
    def test_busy_timeout_pragma_on_writer_connection(self, store):
        # Bounds writer waits when a slow TUI reader holds the lock.
        cur = store._conn.execute("PRAGMA busy_timeout")
        assert cur.fetchone()[0] == 500

    @pytest.mark.unit
    def test_busy_timeout_pragma_on_readonly_connection(self, store, tmp_path):
        # The readonly connection inherits the same bound so a slow
        # query never stalls the TUI refresh loop indefinitely.
        # store fixture already opened+closed implicitly created the file.
        store.flush()  # ensure file exists on disk
        ro = StatsStore.open_readonly(store.db_path)
        try:
            cur = ro.execute("PRAGMA busy_timeout")
            assert cur.fetchone()[0] == 500
        finally:
            ro.close()

    @pytest.mark.unit
    def test_v2_schema_includes_new_raw_nut_metrics(self, store):
        # I2: spec 2.12 raw NUT metrics added in v5.1.0-rc6.
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(samples)")}
        assert {"battery_voltage", "ups_temperature",
                "input_frequency", "output_frequency"} <= cols

    @pytest.mark.unit
    def test_v2_agg_tables_include_new_avg_columns(self, store):
        # S3 + I2: closes the long-standing output_voltage_avg gap and
        # adds the v2 metric aggregates.
        for table in ("agg_5min", "agg_hourly"):
            cols = {r[1] for r in store._conn.execute(f"PRAGMA table_info({table})")}
            assert {"output_voltage_avg", "battery_voltage_avg",
                    "ups_temperature_avg", "ups_temperature_min",
                    "ups_temperature_max", "input_frequency_avg",
                    "output_frequency_avg"} <= cols, (
                f"missing v2 agg columns on {table}: {cols}"
            )


class TestSchemaMigration:
    """v1 -> v2 idempotent migration of pre-rc6 databases."""

    @staticmethod
    def _build_v1_db(path: Path) -> None:
        """Synthesize a v1-shaped DB by hand (no v2 columns)."""
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE samples ("
            "ts INTEGER NOT NULL, status TEXT, battery_charge REAL, "
            "battery_runtime REAL, ups_load REAL, input_voltage REAL, "
            "output_voltage REAL, depletion_rate REAL, "
            "time_on_battery INTEGER, connection_state TEXT)"
        )
        for table in ("agg_5min", "agg_hourly"):
            c.execute(
                f"CREATE TABLE {table} ("
                "ts INTEGER PRIMARY KEY, "
                "battery_charge_avg REAL, battery_charge_min REAL, "
                "battery_charge_max REAL, battery_runtime_avg REAL, "
                "ups_load_avg REAL, ups_load_max REAL, "
                "input_voltage_avg REAL, input_voltage_min REAL, "
                "input_voltage_max REAL, samples_count INTEGER)"
            )
        c.execute(
            "CREATE TABLE events (ts INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, detail TEXT)"
        )
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute(
            "INSERT INTO meta(key,value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        # Seed a v1-shaped sample so we can prove preservation.
        c.execute(
            "INSERT INTO samples VALUES (1000,'OL',95.0,1800.0,15.0,"
            "230.0,231.0,0.0,0,'OK')"
        )
        c.close()

    @pytest.mark.unit
    def test_v1_db_migrates_samples_to_v2(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_v1_db(path)
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(samples)")}
            assert {"battery_voltage", "ups_temperature",
                    "input_frequency", "output_frequency"} <= cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v1_db_migrates_agg_tables_to_v2(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_v1_db(path)
        s = StatsStore(path)
        s.open()
        try:
            for table in ("agg_5min", "agg_hourly"):
                cols = {r[1] for r in s._conn.execute(
                    f"PRAGMA table_info({table})"
                )}
                assert {"output_voltage_avg", "battery_voltage_avg",
                        "ups_temperature_avg", "ups_temperature_min",
                        "ups_temperature_max", "input_frequency_avg",
                        "output_frequency_avg"} <= cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v1_data_preserved_after_migration(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_v1_db(path)
        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT ts, status, battery_charge, battery_voltage FROM samples"
            ).fetchone()
            # Pre-existing row preserved; new column NULL until next sample.
            assert row == (1000, "OL", 95.0, None)
        finally:
            s.close()

    @pytest.mark.unit
    def test_v1_to_v2_migration_bumps_meta(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_v1_db(path)
        s = StatsStore(path)
        s.open()
        try:
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION
        finally:
            s.close()

    @pytest.mark.unit
    def test_migration_idempotent_on_repeated_open(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_v1_db(path)
        # Open + close + reopen must not raise (ALTER TABLE is wrapped).
        StatsStore(path).open()
        s2 = StatsStore(path)
        s2.open()
        try:
            cols = {r[1] for r in s2._conn.execute("PRAGMA table_info(samples)")}
            assert "battery_voltage" in cols
        finally:
            s2.close()


# ===========================================================================
# buffer_sample (hot path)
# ===========================================================================

class TestBufferSample:

    @pytest.mark.unit
    def test_buffer_is_in_memory_only(self, store):
        store.buffer_sample(SAMPLE_UPS_DATA)
        # No row in the DB until flush.
        cur = store._conn.execute("SELECT COUNT(*) FROM samples")
        assert cur.fetchone()[0] == 0
        # But it's in the buffer.
        with store._buffer_lock:
            assert len(store._buffer) == 1

    @pytest.mark.unit
    def test_buffer_sample_thread_safe(self, store):
        # 10 threads × 100 buffers each => 1000 rows expected.
        N = 100
        T = 10

        def push():
            for _ in range(N):
                store.buffer_sample(SAMPLE_UPS_DATA)

        threads = [threading.Thread(target=push) for _ in range(T)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with store._buffer_lock:
            assert len(store._buffer) == T * N

    @pytest.mark.unit
    def test_buffer_sample_constant_time_microbench(self, store):
        # Loose bench: 5,000 samples should buffer in well under 1s.
        # This is a smoke test against accidental I/O on the hot path.
        t0 = time.monotonic()
        for _ in range(5000):
            store.buffer_sample(SAMPLE_UPS_DATA)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0

    @pytest.mark.unit
    def test_buffer_overflow_drops_oldest(self, tmp_path):
        s = StatsStore(tmp_path / "x.db", buffer_maxlen=10)
        s.open()
        try:
            for i in range(20):
                s.buffer_sample(SAMPLE_UPS_DATA, ts=i)
            with s._buffer_lock:
                assert len(s._buffer) == 10
                # deque drops the oldest -> ts 10..19 remain.
                tss = [row[0] for row in s._buffer]
                assert tss == list(range(10, 20))
        finally:
            s.close()

    @pytest.mark.unit
    def test_sample_fields_coercion(self, store):
        store.buffer_sample({
            "ups.status": "OL",
            "battery.charge": "abc",  # not numeric -> NULL
            "battery.runtime": "",     # empty -> NULL
            "ups.load": "30",
            "input.voltage": "230.5",
            "output.voltage": "230",
        })
        store.flush()
        cur = store._conn.execute(
            "SELECT battery_charge, battery_runtime, ups_load, "
            "input_voltage, output_voltage FROM samples"
        )
        row = cur.fetchone()
        assert row[0] is None and row[1] is None
        assert row[2] == 30.0
        assert row[3] == 230.5
        assert row[4] == 230.0

    @pytest.mark.unit
    def test_buffer_extracts_v2_raw_nut_metrics(self, store):
        # I2: the 4 v2 metric columns are extracted from the raw upsc dict.
        store.buffer_sample({
            **SAMPLE_UPS_DATA,
            "battery.voltage": "26.4",
            "ups.temperature": "31.5",
            "input.frequency": "59.98",
            "output.frequency": "60.00",
        })
        store.flush()
        cur = store._conn.execute(
            "SELECT battery_voltage, ups_temperature, "
            "input_frequency, output_frequency FROM samples"
        )
        row = cur.fetchone()
        assert row == (26.4, 31.5, pytest.approx(59.98), 60.00)

    @pytest.mark.unit
    def test_buffer_v2_metrics_default_to_null_when_missing(self, store):
        # Most NUT drivers don't expose all 4 -- missing keys must NULL out.
        store.buffer_sample(SAMPLE_UPS_DATA)
        store.flush()
        cur = store._conn.execute(
            "SELECT battery_voltage, ups_temperature, "
            "input_frequency, output_frequency FROM samples"
        )
        assert cur.fetchone() == (None, None, None, None)


# ===========================================================================
# flush + aggregate + purge
# ===========================================================================

class TestFlush:

    @pytest.mark.unit
    def test_flush_writes_buffered_samples(self, store):
        for i in range(5):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=1_000_000 + i)
        n = store.flush()
        assert n == 5
        cur = store._conn.execute("SELECT COUNT(*) FROM samples")
        assert cur.fetchone()[0] == 5
        with store._buffer_lock:
            assert len(store._buffer) == 0

    @pytest.mark.unit
    def test_flush_empty_buffer_is_zero(self, store):
        assert store.flush() == 0

    @pytest.mark.unit
    def test_flush_uses_single_transaction(self, store):
        # Insert N rows, ensure the rowcount post-flush matches.
        for i in range(50):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=2_000_000 + i)
        store.flush()
        cur = store._conn.execute("SELECT COUNT(*) FROM samples")
        assert cur.fetchone()[0] == 50

    @pytest.mark.unit
    def test_flush_swallows_sqlite_error(self, store):
        store.buffer_sample(SAMPLE_UPS_DATA)

        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def executemany(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")
        # Swap in a connection that raises on every write.
        original = store._conn
        store._conn = _BoomConn()
        try:
            # Must not raise; must return 0.
            assert store.flush() == 0
        finally:
            store._conn = original


class TestAggregate:

    @pytest.mark.unit
    def test_aggregate_buckets_into_5min(self, store):
        # Two buckets: ts 1000000..1000010 and 1000301..1000310 (>5min apart).
        bucket1 = 1_000_000
        bucket2 = 1_000_000 + BUCKET_5MIN + 1
        for i in range(11):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=bucket1 + i)
        for i in range(11):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=bucket2 + i)
        store.flush()
        store.aggregate()
        cur = store._conn.execute(
            "SELECT COUNT(*) FROM agg_5min WHERE samples_count > 0"
        )
        assert cur.fetchone()[0] == 2

    @pytest.mark.unit
    def test_aggregate_min_max_avg(self, store):
        ts = 3_000_000
        for i, charge in enumerate([10.0, 50.0, 90.0]):
            data = dict(SAMPLE_UPS_DATA, **{"battery.charge": str(charge)})
            store.buffer_sample(data, ts=ts + i)
        store.flush()
        store.aggregate()
        cur = store._conn.execute(
            "SELECT battery_charge_avg, battery_charge_min, "
            "battery_charge_max, samples_count FROM agg_5min"
        )
        avg, mn, mx, count = cur.fetchone()
        assert mn == 10.0 and mx == 90.0 and count == 3
        assert avg == pytest.approx(50.0)

    @pytest.mark.unit
    def test_aggregate_emits_v2_metric_columns(self, store):
        # I2 + S3: prove the agg path actually computes the new columns
        # rather than leaving them NULL.
        ts = 4_000_000
        for i, temp in enumerate([20.0, 25.0, 30.0]):
            store.buffer_sample({
                **SAMPLE_UPS_DATA,
                "ups.temperature": str(temp),
                "battery.voltage": "26.0",
                "input.frequency": "60.0",
                "output.frequency": "60.0",
            }, ts=ts + i)
        store.flush()
        store.aggregate()
        cur = store._conn.execute(
            "SELECT output_voltage_avg, battery_voltage_avg, "
            "ups_temperature_avg, ups_temperature_min, ups_temperature_max, "
            "input_frequency_avg, output_frequency_avg "
            "FROM agg_5min"
        )
        row = cur.fetchone()
        assert row[0] == pytest.approx(230.0)        # output_voltage_avg
        assert row[1] == pytest.approx(26.0)         # battery_voltage_avg
        assert row[2] == pytest.approx(25.0)         # ups_temperature_avg
        assert row[3] == 20.0 and row[4] == 30.0     # min/max
        assert row[5] == pytest.approx(60.0)         # input_frequency_avg
        assert row[6] == pytest.approx(60.0)         # output_frequency_avg

    @pytest.mark.unit
    def test_aggregate_rolls_5min_into_hourly(self, store):
        # Drop 12 5-min buckets across exactly one hour. Anchor the base to
        # an hour boundary so the buckets do not straddle two hourly rows.
        base = (4_000_000 // BUCKET_HOURLY) * BUCKET_HOURLY
        for bucket in range(12):
            ts = base + bucket * BUCKET_5MIN
            for i in range(3):
                store.buffer_sample(SAMPLE_UPS_DATA, ts=ts + i)
        store.flush()
        store.aggregate()
        cur = store._conn.execute("SELECT COUNT(*) FROM agg_5min")
        assert cur.fetchone()[0] == 12
        cur = store._conn.execute(
            "SELECT COUNT(*) FROM agg_hourly WHERE samples_count > 0"
        )
        # All 12 5-min buckets fall inside one hour -> one hourly row.
        assert cur.fetchone()[0] == 1

    @pytest.mark.unit
    def test_aggregate_swallows_sqlite_error(self, store):
        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")
        original = store._conn
        store._conn = _BoomConn()
        try:
            assert store.aggregate() == (0, 0)
        finally:
            store._conn = original


class TestPurge:

    @pytest.mark.unit
    def test_purge_removes_old_samples(self, tmp_path):
        s = StatsStore(tmp_path / "purge.db", retention_raw_hours=1)
        s.open()
        try:
            now = int(time.time())
            old = now - 7200  # 2 hours old, beyond 1h retention
            recent = now - 60
            s.buffer_sample(SAMPLE_UPS_DATA, ts=old)
            s.buffer_sample(SAMPLE_UPS_DATA, ts=recent)
            s.flush()
            s.purge()
            cur = s._conn.execute("SELECT COUNT(*) FROM samples")
            assert cur.fetchone()[0] == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_purge_swallows_sqlite_error(self, store):
        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")
        original = store._conn
        store._conn = _BoomConn()
        try:
            assert store.purge() == (0, 0, 0)
        finally:
            store._conn = original


# ===========================================================================
# query_range + tier selection
# ===========================================================================

class TestQueryRange:

    @pytest.mark.unit
    def test_pick_tier_by_window(self):
        now = 10_000_000
        assert StatsStore._pick_tier(now - 3600, now) == "samples"
        assert StatsStore._pick_tier(now - 7 * 86400, now) == "agg_5min"
        assert StatsStore._pick_tier(now - 365 * 86400, now) == "agg_hourly"

    @pytest.mark.unit
    def test_query_range_returns_samples_in_window(self, store):
        for i in range(10):
            data = dict(SAMPLE_UPS_DATA, **{"battery.charge": str(50 + i)})
            store.buffer_sample(data, ts=5_000_000 + i)
        store.flush()
        results = store.query_range(
            "battery_charge", 5_000_000, 5_000_009, prefer_tier="samples",
        )
        assert len(results) == 10
        assert results[0] == (5_000_000, 50.0)
        assert results[-1] == (5_000_009, 59.0)

    @pytest.mark.unit
    def test_query_range_skips_null_values(self, store):
        # Mix valid + null charges -- nulls should be filtered.
        store.buffer_sample(SAMPLE_UPS_DATA, ts=6_000_000)
        store.buffer_sample(
            dict(SAMPLE_UPS_DATA, **{"battery.charge": ""}),  # NULL
            ts=6_000_001,
        )
        store.buffer_sample(SAMPLE_UPS_DATA, ts=6_000_002)
        store.flush()
        results = store.query_range(
            "battery_charge", 6_000_000, 6_000_002, prefer_tier="samples",
        )
        assert len(results) == 2

    @pytest.mark.unit
    def test_query_range_picks_5min_tier_for_week(self, store):
        # 5-min agg is what we'd query for a 7-day window.
        # Buffer sparse samples then aggregate.
        base = 7_000_000
        for i in range(3):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=base + i * BUCKET_5MIN)
        store.flush()
        store.aggregate()
        # Default tier picker returns agg_5min for 7d windows.
        results = store.query_range("battery_charge", base, base + 7 * 86400)
        assert len(results) >= 1


# ===========================================================================
# events
# ===========================================================================

class TestEvents:

    @pytest.mark.unit
    def test_log_event_round_trip(self, store):
        store.log_event("ON_BATTERY", "Battery: 85%", ts=8_000_000)
        store.log_event("POWER_RESTORED", "Outage 30s", ts=8_000_010)
        rows = store.query_events(8_000_000, 8_000_100)
        assert len(rows) == 2
        assert rows[0] == (8_000_000, "ON_BATTERY", "Battery: 85%")
        assert rows[1] == (8_000_010, "POWER_RESTORED", "Outage 30s")

    @pytest.mark.unit
    def test_query_events_window_inclusive(self, store):
        store.log_event("X", "x", ts=9_000_000)
        rows = store.query_events(9_000_000, 9_000_000)
        assert len(rows) == 1

    @pytest.mark.unit
    def test_log_event_swallows_sqlite_error(self, store):
        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")
        original = store._conn
        store._conn = _BoomConn()
        try:
            store.log_event("X", "x")  # must not raise
        finally:
            store._conn = original


# ===========================================================================
# open_readonly
# ===========================================================================

class TestOpenReadonly:

    @pytest.mark.unit
    def test_returns_none_for_missing_db(self, tmp_path):
        assert StatsStore.open_readonly(tmp_path / "missing.db") is None

    @pytest.mark.unit
    def test_returns_readonly_connection(self, store):
        # Seed some data first.
        store.buffer_sample(SAMPLE_UPS_DATA, ts=12_000_000)
        store.flush()
        roconn = StatsStore.open_readonly(store.db_path)
        try:
            cur = roconn.execute("SELECT COUNT(*) FROM samples")
            assert cur.fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                roconn.execute("INSERT INTO samples (ts) VALUES (1)")
        finally:
            roconn.close()


# ===========================================================================
# Concurrent reader + writer (WAL mode)
# ===========================================================================

class TestConcurrentReaderWriter:

    @pytest.mark.unit
    def test_reader_can_query_during_writes(self, store):
        # Buffer + flush in a background thread; query in the main thread.
        stop = threading.Event()

        def writer_loop():
            i = 0
            while not stop.is_set():
                store.buffer_sample(SAMPLE_UPS_DATA, ts=20_000_000 + i)
                store.flush()
                i += 1
                if i >= 20:
                    return

        wt = threading.Thread(target=writer_loop)
        wt.start()
        try:
            # Reader iterates while writer writes; must not raise.
            ro = StatsStore.open_readonly(store.db_path)
            for _ in range(10):
                cur = ro.execute("SELECT COUNT(*) FROM samples")
                cur.fetchone()
                time.sleep(0.005)
            ro.close()
        finally:
            stop.set()
            wt.join()


# ===========================================================================
# StatsWriter thread (lifecycle)
# ===========================================================================

class TestStatsWriterThread:

    @pytest.mark.unit
    def test_writer_flushes_buffer_periodically(self, store):
        stop = threading.Event()
        w = StatsWriter(store, stop, flush_interval=0.05,
                        maintenance_interval=10.0)
        w.start()
        try:
            for i in range(5):
                store.buffer_sample(SAMPLE_UPS_DATA, ts=30_000_000 + i)
            time.sleep(0.2)  # let the writer drain the buffer
            cur = store._conn.execute("SELECT COUNT(*) FROM samples")
            assert cur.fetchone()[0] == 5
        finally:
            stop.set()
            w.join(timeout=2)

    @pytest.mark.unit
    def test_writer_shutdown_flushes_remaining(self, store):
        stop = threading.Event()
        w = StatsWriter(store, stop, flush_interval=10.0,
                        maintenance_interval=10.0)
        w.start()
        try:
            store.buffer_sample(SAMPLE_UPS_DATA, ts=40_000_000)
        finally:
            stop.set()
            w.join(timeout=2)
        cur = store._conn.execute("SELECT COUNT(*) FROM samples")
        assert cur.fetchone()[0] == 1


# ===========================================================================
# Failure isolation contract
# ===========================================================================

class TestFailureIsolation:

    @pytest.mark.unit
    def test_log_error_once_rate_limits(self, tmp_path):
        s = StatsStore(tmp_path / "x.db")
        # Don't actually open; we're testing the rate limit primitive.
        logs = []
        class FakeLogger:
            def log(self, msg):
                logs.append(msg)
        s._logger = FakeLogger()
        s._error_log_interval = 1.0
        s._log_error_once("disk I/O error")
        s._log_error_once("disk I/O error")
        s._log_error_once("disk I/O error")
        # Three rapid calls -> only one log line.
        assert len(logs) == 1

    @pytest.mark.unit
    def test_methods_no_op_when_unopened(self, tmp_path):
        s = StatsStore(tmp_path / "never-opened.db")
        # All these must return cleanly without raising.
        s.buffer_sample(SAMPLE_UPS_DATA)
        assert s.flush() == 0
        assert s.aggregate() == (0, 0)
        assert s.purge() == (0, 0, 0)
        assert s.query_range("battery_charge", 0, 1) == []
        assert s.query_events(0, 1) == []
        s.log_event("X", "x")  # just must not raise

    @pytest.mark.unit
    def test_close_is_idempotent(self, tmp_path):
        s = StatsStore(tmp_path / "x.db")
        s.open()
        s.close()
        s.close()  # second close must not raise


class TestEdgeCases:
    """Edge cases the contract implies but earlier tests do not pin."""

    @pytest.mark.unit
    def test_schema_version_persists_across_reopen(self, tmp_path):
        """The recorded ``schema_version`` survives close + reopen of the DB.

        Catches the "we accidentally reset the schema on reopen" regression.
        """
        path = tmp_path / "x.db"
        s1 = StatsStore(path)
        s1.open()
        # Bump the recorded version to make sure init does not stomp it.
        s1._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        s1.close()

        s2 = StatsStore(path)
        s2.open()
        try:
            cur = s2._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            )
            assert int(cur.fetchone()[0]) == SCHEMA_VERSION
        finally:
            s2.close()

    @pytest.mark.unit
    def test_text_fields_round_trip(self, store):
        """``status`` and ``connection_state`` survive flush -> read intact."""
        store.buffer_sample(
            dict(SAMPLE_UPS_DATA, **{"ups.status": "OB DISCHRG"}),
            connection_state="GRACE_PERIOD",
            ts=14_000_000,
        )
        store.flush()
        cur = store._conn.execute(
            "SELECT status, connection_state FROM samples WHERE ts = 14000000"
        )
        row = cur.fetchone()
        assert row == ("OB DISCHRG", "GRACE_PERIOD")

    @pytest.mark.unit
    def test_query_range_for_unaggregated_metric_at_agg_tier_returns_empty(
        self, store,
    ):
        """Eneru-derived state fields (``depletion_rate``,
        ``time_on_battery``, ``connection_state``) intentionally aren't
        carried into the agg tables -- they're state-shaped, not
        signal-shaped.

        ``_agg_column_for`` falls back to the raw column name; the SQL
        therefore references a column that does not exist in agg_5min.
        ``query_range`` must catch the SQLite error and return [], not
        propagate.
        """
        # Seed enough sample/agg data so the agg path actually runs.
        ts = 15_000_000
        for i in range(3):
            store.buffer_sample(SAMPLE_UPS_DATA, ts=ts + i)
        store.flush()
        store.aggregate()
        # Force the agg_5min tier (a 1-week window would also pick it).
        # depletion_rate is one of the 3 v5.1 derived fields that
        # deliberately have no aggregate column.
        results = store.query_range(
            "depletion_rate", ts, ts + 1, prefer_tier="agg_5min",
        )
        assert results == []

    @pytest.mark.unit
    def test_aggregate_single_sample_yields_min_eq_max_eq_avg(self, store):
        """Single-sample bucket: min, max, and avg all equal the input."""
        ts = 16_000_000
        store.buffer_sample(
            dict(SAMPLE_UPS_DATA, **{"battery.charge": "73"}),
            ts=ts,
        )
        store.flush()
        store.aggregate()
        cur = store._conn.execute(
            "SELECT battery_charge_avg, battery_charge_min, "
            "battery_charge_max, samples_count FROM agg_5min "
            "WHERE samples_count = 1"
        )
        avg, mn, mx, n = cur.fetchone()
        assert mn == mx == avg == 73.0
        assert n == 1

    @pytest.mark.unit
    def test_purge_keeps_row_at_exact_cutoff(self, tmp_path):
        """Purge SQL is ``ts < cutoff``; a row at cutoff exactly stays."""
        s = StatsStore(tmp_path / "boundary.db", retention_raw_hours=1)
        s.open()
        try:
            now = int(time.time())
            cutoff = now - 3600  # the rolling cutoff_raw
            # One row strictly older, one at the cutoff.
            s.buffer_sample(SAMPLE_UPS_DATA, ts=cutoff - 1)
            s.buffer_sample(SAMPLE_UPS_DATA, ts=cutoff)
            s.flush()
            s.purge()
            cur = s._conn.execute("SELECT ts FROM samples ORDER BY ts ASC")
            tss = [r[0] for r in cur.fetchall()]
            assert tss == [cutoff]
        finally:
            s.close()

    @pytest.mark.unit
    def test_query_range_empty_window_returns_empty_list(self, store):
        """No rows in window -> ``[]``, not an exception, not None."""
        # Seed unrelated samples far outside the queried range.
        store.buffer_sample(SAMPLE_UPS_DATA, ts=17_000_000)
        store.flush()
        # Window with no matching rows.
        out = store.query_range("battery_charge", 0, 100, prefer_tier="samples")
        assert out == []
        # Inverted window (start > end) is also empty, no error.
        out = store.query_range(
            "battery_charge", 18_000_000, 17_999_000, prefer_tier="samples",
        )
        assert out == []


# ===========================================================================
# StatsConfig dataclass
# ===========================================================================

class TestStatsConfig:

    @pytest.mark.unit
    def test_defaults(self):
        cfg = StatsConfig()
        assert cfg.db_directory == "/var/lib/eneru"
        assert cfg.retention.raw_hours == 24
        assert cfg.retention.agg_5min_days == 30
        assert cfg.retention.agg_hourly_days == 1825

    @pytest.mark.unit
    def test_retention_overrides(self):
        r = StatsRetentionConfig(raw_hours=48, agg_5min_days=7,
                                 agg_hourly_days=90)
        cfg = StatsConfig(db_directory="/srv/stats", retention=r)
        assert cfg.db_directory == "/srv/stats"
        assert cfg.retention.raw_hours == 48
        assert cfg.retention.agg_5min_days == 7
        assert cfg.retention.agg_hourly_days == 90

    @pytest.mark.unit
    def test_yaml_round_trip(self, tmp_path):
        from eneru import ConfigLoader
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("""
ups:
  name: "TestUPS@localhost"
statistics:
  db_directory: "/tmp/test-stats"
  retention:
    raw_hours: 12
    agg_5min_days: 7
    agg_hourly_days: 90
""")
        config = ConfigLoader.load(str(cfg_path))
        assert config.statistics.db_directory == "/tmp/test-stats"
        assert config.statistics.retention.raw_hours == 12
        assert config.statistics.retention.agg_5min_days == 7
        assert config.statistics.retention.agg_hourly_days == 90

    @pytest.mark.unit
    def test_default_yaml_omitted_section(self, tmp_path):
        from eneru import ConfigLoader
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("""
ups:
  name: "TestUPS@localhost"
""")
        config = ConfigLoader.load(str(cfg_path))
        assert config.statistics.db_directory == "/var/lib/eneru"
        assert config.statistics.retention.raw_hours == 24
