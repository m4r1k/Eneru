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
