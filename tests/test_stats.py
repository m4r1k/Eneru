"""Tests for the per-UPS SQLite stats store + writer thread."""

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

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
    DELIVERING_RECOVERY_GRACE_SECONDS,
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


def open_and_close_store(path):
    """Exercise the open lifecycle without leaking the SQLite handle."""
    s = StatsStore(path)
    try:
        s.open()
    finally:
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
    def test_open_failure_clears_connection(self, tmp_path):
        s = StatsStore(tmp_path / "broken.db")
        with patch.object(s, "_init_schema", side_effect=sqlite3.Error("bad schema")):
            with pytest.raises(sqlite3.Error):
                s.open()
        assert s._conn is None

    @pytest.mark.unit
    def test_open_failure_on_delivery_recovery_clears_connection(self, tmp_path):
        s = StatsStore(tmp_path / "broken-recovery.db")
        with patch.object(
            s,
            "_recover_delivering_notifications",
            side_effect=sqlite3.Error("recovery failed"),
        ):
            with pytest.raises(sqlite3.Error):
                s.open()
        assert s._conn is None

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
    def test_safe_alter_only_swallows_duplicate_column(self, tmp_path):
        class _Conn:
            def __init__(self, exc):
                self.exc = exc

            def execute(self, *_a, **_kw):
                raise self.exc

        s = StatsStore(tmp_path / "x.db")
        s._conn = _Conn(sqlite3.OperationalError("duplicate column name: status"))
        s._safe_alter("samples", "status TEXT")

        s._conn = _Conn(sqlite3.OperationalError("database is locked"))
        with pytest.raises(sqlite3.OperationalError):
            s._safe_alter("samples", "status TEXT")

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
    def test_versionless_populated_db_is_recovered_not_treated_as_fresh(
        self, tmp_path
    ):
        # F-027: an older binary created the tables (v1 shape) but crashed before
        # stamping meta.schema_version. A newer binary opening this versionless-
        # but-populated DB must run the full migration chain and stamp the current
        # version -- NOT mistake it for brand-new (which would skip migrations and
        # leave the newer columns missing = silent stats loss).
        path = tmp_path / "crashed.db"
        self._build_v1_db(path)
        # Simulate crash-before-stamp: drop the schema_version row.
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("DELETE FROM meta WHERE key='schema_version'")
        c.close()

        s = StatsStore(path)
        s.open()
        try:
            # Version stamped to current.
            row = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(row[0]) == SCHEMA_VERSION
            # Migrations actually ran: a v7 column exists on samples.
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(samples)")}
            assert {"battery_voltage", "real_power", "power_nominal"} <= cols
            # Existing v1 row preserved (not wiped as if fresh).
            n = s._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            assert n == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_created_then_reopened_reports_current_version(self, tmp_path):
        # F-027: a normally-created DB reopened reports the correct version.
        path = tmp_path / "roundtrip.db"
        s1 = StatsStore(path)
        s1.open()
        s1.close()
        s2 = StatsStore(path)
        s2.open()
        try:
            row = s2._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(row[0]) == SCHEMA_VERSION
        finally:
            s2.close()

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
        open_and_close_store(path)
        s2 = StatsStore(path)
        s2.open()
        try:
            cols = {r[1] for r in s2._conn.execute("PRAGMA table_info(samples)")}
            assert "battery_voltage" in cols
        finally:
            s2.close()

    @staticmethod
    def _build_v2_db(path: Path) -> None:
        """Synthesize a v2-shaped DB (post-rc6 / pre-issue-#27)."""
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE samples (ts INTEGER NOT NULL, status TEXT, "
            "battery_charge REAL, battery_runtime REAL, ups_load REAL, "
            "input_voltage REAL, output_voltage REAL, depletion_rate REAL, "
            "time_on_battery INTEGER, connection_state TEXT, "
            "battery_voltage REAL, ups_temperature REAL, "
            "input_frequency REAL, output_frequency REAL)"
        )
        for table in ("agg_5min", "agg_hourly"):
            c.execute(
                f"CREATE TABLE {table} (ts INTEGER PRIMARY KEY, "
                "battery_charge_avg REAL, samples_count INTEGER)"
            )
        # v2 events table: NO notification_sent column.
        c.execute(
            "CREATE TABLE events (ts INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, detail TEXT)"
        )
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute(
            "INSERT INTO meta(key,value) VALUES (?, ?)",
            ("schema_version", "2"),
        )
        # Seed a v2 event row to prove preservation.
        c.execute(
            "INSERT INTO events(ts, event_type, detail) "
            "VALUES (?, ?, ?)", (1500, "ON_BATTERY", "pre-rc7 row"),
        )
        c.close()

    @pytest.mark.unit
    def test_v2_db_migrates_events_to_v3(self, tmp_path):
        # B4: ALTER TABLE events ADD COLUMN notification_sent
        path = tmp_path / "v2.db"
        self._build_v2_db(path)
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(events)")}
            assert "notification_sent" in cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v2_to_v3_migration_bumps_meta_to_3(self, tmp_path):
        path = tmp_path / "v2.db"
        self._build_v2_db(path)
        s = StatsStore(path)
        s.open()
        try:
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION  # currently 3
        finally:
            s.close()

    @pytest.mark.unit
    def test_v2_events_preserved_with_default_notification_sent(
        self, tmp_path,
    ):
        path = tmp_path / "v2.db"
        self._build_v2_db(path)
        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT ts, event_type, notification_sent FROM events"
            ).fetchone()
            # Pre-existing row defaults notification_sent to 1 (the
            # event WAS notified back when v2 ran -- there was no way
            # to suppress).
            assert row == (1500, "ON_BATTERY", 1)
        finally:
            s.close()

    @pytest.mark.unit
    def test_v3_migration_idempotent(self, tmp_path):
        path = tmp_path / "v2.db"
        self._build_v2_db(path)
        # Open + close + reopen + reopen must all succeed.
        open_and_close_store(path)
        open_and_close_store(path)
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(events)")}
            assert "notification_sent" in cols
        finally:
            s.close()

    @staticmethod
    def _build_v3_db(path: Path) -> None:
        """Synthesize a v3-shaped DB (v5.1.x) — no notifications table."""
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE samples (ts INTEGER NOT NULL, status TEXT, "
            "battery_charge REAL, battery_runtime REAL, ups_load REAL, "
            "input_voltage REAL, output_voltage REAL, depletion_rate REAL, "
            "time_on_battery INTEGER, connection_state TEXT, "
            "battery_voltage REAL, ups_temperature REAL, "
            "input_frequency REAL, output_frequency REAL)"
        )
        for table in ("agg_5min", "agg_hourly"):
            c.execute(
                f"CREATE TABLE {table} (ts INTEGER PRIMARY KEY, "
                "battery_charge_avg REAL, samples_count INTEGER)"
            )
        c.execute(
            "CREATE TABLE events (ts INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, detail TEXT, "
            "notification_sent INTEGER DEFAULT 1)"
        )
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute(
            "INSERT INTO meta(key,value) VALUES (?, ?)",
            ("schema_version", "3"),
        )
        # Seed an event row to prove preservation across migration.
        c.execute(
            "INSERT INTO events(ts, event_type, detail) "
            "VALUES (?, ?, ?)", (2000, "ON_BATTERY", "v3 row"),
        )
        c.close()

    @pytest.mark.unit
    def test_v3_db_migrates_to_v4_creates_notifications_table(self, tmp_path):
        # v4: new notifications table for the persistent queue.
        path = tmp_path / "v3.db"
        self._build_v3_db(path)
        s = StatsStore(path)
        s.open()
        try:
            tables = {
                r[0] for r in s._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "notifications" in tables
            cols = {
                r[1] for r in s._conn.execute(
                    "PRAGMA table_info(notifications)"
                )
            }
            assert {"id", "ts", "body", "notify_type", "category",
                    "status", "attempts", "sent_at", "delivering_at",
                    "cancel_reason"} <= cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v3_to_v4_migration_bumps_meta_to_4(self, tmp_path):
        path = tmp_path / "v3.db"
        self._build_v3_db(path)
        s = StatsStore(path)
        s.open()
        try:
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION  # currently 4
        finally:
            s.close()

    @pytest.mark.unit
    def test_v3_events_preserved_after_v4_migration(self, tmp_path):
        path = tmp_path / "v3.db"
        self._build_v3_db(path)
        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT ts, event_type, detail FROM events"
            ).fetchone()
            assert row == (2000, "ON_BATTERY", "v3 row")
        finally:
            s.close()

    @pytest.mark.unit
    def test_v4_migration_idempotent(self, tmp_path):
        path = tmp_path / "v3.db"
        self._build_v3_db(path)
        # Open + close + reopen + reopen must all succeed; a partially-
        # migrated DB heals via CREATE TABLE IF NOT EXISTS.
        open_and_close_store(path)
        open_and_close_store(path)
        s = StatsStore(path)
        s.open()
        try:
            tables = {
                r[0] for r in s._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "notifications" in tables
        finally:
            s.close()

    @staticmethod
    def _build_v4_db(path: Path) -> None:
        """Synthesize a real v4-shaped DB: full samples + both aggregate tables +
        events (NO id column) + notifications + meta, so the v4->v5 rebuild is
        exercised against a database that matches what a real v4 install holds."""
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE samples (ts INTEGER NOT NULL, status TEXT, "
            "battery_charge REAL, battery_runtime REAL, ups_load REAL, "
            "input_voltage REAL, output_voltage REAL, depletion_rate REAL, "
            "time_on_battery INTEGER, connection_state TEXT, "
            "battery_voltage REAL, ups_temperature REAL, "
            "input_frequency REAL, output_frequency REAL)"
        )
        for table in ("agg_5min", "agg_hourly"):
            c.execute(
                f"CREATE TABLE {table} (ts INTEGER PRIMARY KEY, "
                "battery_charge_avg REAL, battery_charge_min REAL, "
                "battery_charge_max REAL, battery_runtime_avg REAL, "
                "ups_load_avg REAL, ups_load_max REAL, input_voltage_avg REAL, "
                "input_voltage_min REAL, input_voltage_max REAL, "
                "samples_count INTEGER, output_voltage_avg REAL, "
                "battery_voltage_avg REAL, ups_temperature_avg REAL, "
                "ups_temperature_min REAL, ups_temperature_max REAL, "
                "input_frequency_avg REAL, output_frequency_avg REAL)"
            )
        c.execute(
            "CREATE TABLE events (ts INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, detail TEXT, "
            "notification_sent INTEGER DEFAULT 1)"
        )
        c.executescript(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, body TEXT NOT NULL, notify_type TEXT NOT NULL, "
            "category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, "
            "cancel_reason TEXT);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        c.execute("INSERT INTO meta(key,value) VALUES ('schema_version','4')")
        # Two events sharing one second + one later — proves id preserves order
        # (id = old rowid) and survives the table rebuild.
        c.executemany(
            "INSERT INTO events(ts, event_type, detail) VALUES (?, ?, ?)",
            [(1000, "ON_BATTERY", "a"), (1000, "LOW_BATTERY", "b"),
             (2000, "POWER_RESTORED", "c")],
        )
        c.close()

    @pytest.mark.unit
    def test_v4_db_migrates_events_to_v5_adds_id(self, tmp_path):
        path = tmp_path / "v4.db"
        self._build_v4_db(path)
        s = StatsStore(path)
        s.open()
        try:
            info = list(s._conn.execute("PRAGMA table_info(events)"))
            cols = {r[1] for r in info}
            assert "id" in cols
            # id is the INTEGER PRIMARY KEY (pk flag set).
            assert any(r[1] == "id" and r[5] == 1 for r in info)
        finally:
            s.close()

    @pytest.mark.unit
    def test_v4_to_v5_bumps_meta_and_preserves_rows(self, tmp_path):
        path = tmp_path / "v4.db"
        self._build_v4_db(path)
        s = StatsStore(path)
        s.open()
        try:
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION  # 5
            rows = s._conn.execute(
                "SELECT id, ts, event_type, detail FROM events ORDER BY id"
            ).fetchall()
            # id == old rowid (insertion order), all fields intact.
            assert rows == [(1, 1000, "ON_BATTERY", "a"),
                            (2, 1000, "LOW_BATTERY", "b"),
                            (3, 2000, "POWER_RESTORED", "c")]
        finally:
            s.close()

    @pytest.mark.unit
    def test_v5_id_is_not_reused_after_delete(self, tmp_path):
        # The safety guarantee behind delete-by-id: AUTOINCREMENT never hands a
        # deleted event's id to a later one.
        path = tmp_path / "v4.db"
        self._build_v4_db(path)
        s = StatsStore(path)
        s.open()
        try:
            s._conn.execute("DELETE FROM events WHERE id=3")
            s._conn.commit()
            s.log_event("NEW", "x", ts=3000)
            new_id = s._conn.execute(
                "SELECT id FROM events WHERE event_type='NEW'"
            ).fetchone()[0]
            assert new_id == 4  # not 3
        finally:
            s.close()

    @pytest.mark.unit
    def test_v5_migration_idempotent(self, tmp_path):
        path = tmp_path / "v4.db"
        self._build_v4_db(path)
        open_and_close_store(path)
        open_and_close_store(path)
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(events)")}
            assert "id" in cols
            # No leftover scratch table from the rebuild.
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "events_v5_new" not in tables
            # Rows still intact (rebuild ran exactly once).
            assert s._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
        finally:
            s.close()

    @pytest.mark.unit
    def test_v5_migration_recovers_orphaned_rebuild_table(self, tmp_path):
        # Simulate an older unsafe rebuild that copied rows into the scratch
        # table and dropped events, then died before the rename.
        path = tmp_path / "v4-partial.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript("""
                CREATE TABLE events_v5_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    notification_sent INTEGER DEFAULT 1
                );
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta(key, value) VALUES ('schema_version', '4');
            """)
            conn.executemany(
                "INSERT INTO events_v5_new"
                "(id, ts, event_type, detail, notification_sent) "
                "VALUES (?, ?, ?, ?, ?)",
                [(1, 1000, "ON_BATTERY", "a", 1),
                 (2, 1000, "LOW_BATTERY", "b", 1)],
            )
            conn.commit()
        finally:
            conn.close()

        s = StatsStore(path)
        s.open()
        try:
            rows = s._conn.execute(
                "SELECT id, ts, event_type, detail FROM events ORDER BY id"
            ).fetchall()
            assert rows == [(1, 1000, "ON_BATTERY", "a"),
                            (2, 1000, "LOW_BATTERY", "b")]
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "events_v5_new" not in tables
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION
        finally:
            s.close()


class TestSchemaMigrationV7:
    """v6 -> v7: energy columns + battery_health / self_tests tables."""

    @staticmethod
    def _build_v6_db(path: Path) -> None:
        """Synthesize a v6-shaped DB by hand (no v7 energy cols/tables)."""
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        # samples: v1 base + v2 additions == the full v6 column set.
        c.execute(
            "CREATE TABLE samples ("
            "ts INTEGER NOT NULL, status TEXT, battery_charge REAL, "
            "battery_runtime REAL, ups_load REAL, input_voltage REAL, "
            "output_voltage REAL, depletion_rate REAL, time_on_battery INTEGER, "
            "connection_state TEXT, battery_voltage REAL, ups_temperature REAL, "
            "input_frequency REAL, output_frequency REAL)"
        )
        c.execute("CREATE INDEX idx_samples_ts ON samples(ts)")
        for table in ("agg_5min", "agg_hourly"):
            c.execute(
                f"CREATE TABLE {table} ("
                "ts INTEGER PRIMARY KEY, battery_charge_avg REAL, "
                "battery_charge_min REAL, battery_charge_max REAL, "
                "battery_runtime_avg REAL, ups_load_avg REAL, ups_load_max REAL, "
                "input_voltage_avg REAL, input_voltage_min REAL, "
                "input_voltage_max REAL, samples_count INTEGER, "
                "output_voltage_avg REAL, battery_voltage_avg REAL, "
                "ups_temperature_avg REAL, ups_temperature_min REAL, "
                "ups_temperature_max REAL, input_frequency_avg REAL, "
                "output_frequency_avg REAL)"
            )
        c.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, event_type TEXT NOT NULL, detail TEXT, "
            "notification_sent INTEGER DEFAULT 1)"
        )
        c.execute(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts INTEGER NOT NULL, body TEXT NOT NULL, notify_type TEXT NOT NULL, "
            "category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, "
            "delivering_at INTEGER, cancel_reason TEXT)"
        )
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO meta(key,value) VALUES ('schema_version','6')")
        # Seed a v6 sample so we can prove preservation across the v7 migration.
        c.execute(
            "INSERT INTO samples (ts, status, battery_charge, battery_runtime, "
            "ups_load, input_voltage, output_voltage, depletion_rate, "
            "time_on_battery, connection_state, battery_voltage, ups_temperature, "
            "input_frequency, output_frequency) VALUES "
            "(1000,'OL',95.0,1800.0,15.0,230.0,231.0,0.0,0,'OK',54.0,35.0,50.0,50.0)"
        )
        c.close()

    @pytest.mark.unit
    def test_v6_db_adds_energy_columns_to_samples(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(samples)")}
            assert {"real_power", "power_nominal"} <= cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v6_db_adds_energy_avg_to_agg_tables(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        s = StatsStore(path)
        s.open()
        try:
            for table in ("agg_5min", "agg_hourly"):
                cols = {r[1] for r in s._conn.execute(
                    f"PRAGMA table_info({table})")}
                assert {"real_power_avg", "power_nominal_avg"} <= cols
        finally:
            s.close()

    @pytest.mark.unit
    def test_v6_db_creates_v7_tables(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        s = StatsStore(path)
        s.open()
        try:
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"battery_health", "self_tests"} <= tables
        finally:
            s.close()

    @pytest.mark.unit
    def test_v6_data_preserved_after_v7_migration(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT ts, status, battery_charge, real_power, power_nominal "
                "FROM samples"
            ).fetchone()
            # Pre-existing row preserved; new energy columns NULL until next sample.
            assert row == (1000, "OL", 95.0, None, None)
        finally:
            s.close()

    @pytest.mark.unit
    def test_v6_to_v7_bumps_meta(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        s = StatsStore(path)
        s.open()
        try:
            sv = s._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert int(sv[0]) == SCHEMA_VERSION == 7
        finally:
            s.close()

    @pytest.mark.unit
    def test_v7_migration_idempotent_on_reopen(self, tmp_path):
        path = tmp_path / "v6.db"
        self._build_v6_db(path)
        open_and_close_store(path)
        # Second open must not raise (ALTERs wrapped, tables IF NOT EXISTS).
        s = StatsStore(path)
        s.open()
        try:
            cols = {r[1] for r in s._conn.execute("PRAGMA table_info(samples)")}
            assert {"real_power", "power_nominal"} <= cols
            assert s._conn.execute("SELECT ts FROM samples").fetchone() == (1000,)
        finally:
            s.close()


class TestV7StoreMethods:
    """v7: battery_health / self_tests / power_samples store API."""

    @staticmethod
    def _open(tmp_path):
        s = StatsStore(tmp_path / "v7.db")
        s.open()
        return s

    @pytest.mark.unit
    def test_record_and_query_battery_health(self, tmp_path):
        s = self._open(tmp_path)
        try:
            s.record_battery_health(
                82.5,
                {"capacity": 80.0, "runtime": 90.0, "self_test": None,
                 "anomaly": 100.0, "age": 70.0},
                detail={"confidence": 0.8, "weights": {"age": 0.2}},
                ts=1000,
            )
            s.record_battery_health(None, {"capacity": None}, ts=2000)
            rows = s.query_battery_health(0, 9999)
            assert [r["ts"] for r in rows] == [1000, 2000]
            assert rows[0]["score"] == 82.5
            assert rows[0]["self_test"] is None      # unavailable kept as NULL
            assert rows[0]["anomaly"] == 100.0
            assert rows[0]["detail"]["confidence"] == 0.8
            assert rows[1]["score"] is None          # unknown score kept as NULL
        finally:
            s.close()

    @pytest.mark.unit
    def test_query_battery_health_empty_range(self, tmp_path):
        s = self._open(tmp_path)
        try:
            assert s.query_battery_health(0, 100) == []
        finally:
            s.close()

    @pytest.mark.unit
    def test_record_battery_health_unserializable_detail(self, tmp_path):
        s = self._open(tmp_path)
        try:
            s.record_battery_health(50.0, {"capacity": 50.0},
                                    detail={"bad": object()}, ts=1000)
            assert s.query_battery_health(0, 9999)[0]["detail"] is None
        finally:
            s.close()

    @pytest.mark.unit
    def test_query_battery_health_tolerates_corrupt_detail(self, tmp_path):
        s = self._open(tmp_path)
        try:
            with s._conn:
                s._conn.execute(
                    "INSERT INTO battery_health (ts, score, detail) VALUES (?,?,?)",
                    (1000, 50.0, "{not valid json"),
                )
            assert s.query_battery_health(0, 9999)[0]["detail"] is None
        finally:
            s.close()

    @pytest.mark.unit
    def test_record_self_test_and_latest(self, tmp_path):
        s = self._open(tmp_path)
        try:
            tid = s.record_self_test("test.battery.start", "scheduler",
                                     started_ts=1000)
            assert isinstance(tid, int)
            latest = s.latest_self_test()
            assert latest["id"] == tid
            assert latest["command"] == "test.battery.start"
            assert latest["result_enum"] == "running"
            assert latest["source"] == "scheduler"
        finally:
            s.close()

    @pytest.mark.unit
    def test_update_self_test_result(self, tmp_path):
        s = self._open(tmp_path)
        try:
            tid = s.record_self_test("test.battery.start", "api", started_ts=1000)
            s.update_self_test_result(
                tid, result_raw="Done and passed",
                result_enum="passed", result_date="2026-06-28",
            )
            latest = s.latest_self_test()
            assert latest["result_enum"] == "passed"
            assert latest["result_raw"] == "Done and passed"
            assert latest["result_date"] == "2026-06-28"
        finally:
            s.close()

    @pytest.mark.unit
    def test_latest_running_self_test_ignores_finished_rows(self, tmp_path):
        s = self._open(tmp_path)
        try:
            old = s.record_self_test("test.battery.start", "scheduler",
                                     started_ts=1000)
            s.update_self_test_result(
                old, result_raw="Done and passed", result_enum="passed")
            running = s.record_self_test("test.battery.start", "api",
                                         started_ts=2000)
            latest = s.latest_running_self_test()
            assert latest["id"] == running
            assert latest["source"] == "api"
            assert latest["result_enum"] == "running"
        finally:
            s.close()

    @pytest.mark.unit
    def test_latest_self_test_none_when_empty(self, tmp_path):
        s = self._open(tmp_path)
        try:
            assert s.latest_self_test() is None
            assert s.latest_running_self_test() is None
        finally:
            s.close()

    @pytest.mark.unit
    def test_power_samples_reads_energy_columns(self, tmp_path):
        s = self._open(tmp_path)
        try:
            s.buffer_sample(
                {"ups.status": "OL", "ups.load": "40",
                 "ups.realpower": "120", "ups.power.nominal": "300"},
                ts=1000,
            )
            s.buffer_sample(
                {"ups.status": "OL", "ups.load": "50"},  # no realpower/nominal
                ts=1001,
            )
            s.flush()
            rows = s.power_samples(0, 9999, prefer_tier="samples")
            assert rows[0] == (1000, 120.0, 40.0, 300.0)
            assert rows[1] == (1001, None, 50.0, None)  # NULL cells preserved
        finally:
            s.close()

    @pytest.mark.unit
    def test_v7_methods_safe_on_closed_store(self, tmp_path):
        # Never opened -> _conn is None. Every v7 method must no-op / return a
        # safe default, never raise (failure-isolation contract).
        s = StatsStore(tmp_path / "closed.db")
        s.record_battery_health(50.0, {"capacity": 50.0})
        assert s.query_battery_health(0, 100) == []
        assert s.record_self_test("test.battery.start", "cli") is None
        s.update_self_test_result(1, result_raw="x", result_enum="passed")
        assert s.latest_self_test() is None
        assert s.latest_running_self_test() is None
        assert s.power_samples(0, 100) == []

    @pytest.mark.unit
    def test_v7_methods_swallow_db_errors(self, tmp_path):
        # _conn is set but the underlying connection is closed, so execute
        # raises sqlite3.ProgrammingError. The failure-isolation contract says
        # swallow + log + return the safe default, never raise into the daemon.
        s = self._open(tmp_path)
        s._conn.close()
        s.record_battery_health(50.0, {"capacity": 50.0}, ts=1000)
        assert s.query_battery_health(0, 9999) == []
        assert s.record_self_test("test.battery.start", "cli") is None
        s.update_self_test_result(1, result_raw="x", result_enum="passed")
        assert s.latest_self_test() is None
        assert s.latest_running_self_test() is None
        assert s.power_samples(0, 9999) == []


class TestNotificationQueue:
    """v4: persistent notification queue CRUD + TTL/cap/age rules."""

    def _open(self, tmp_path):
        s = StatsStore(tmp_path / "n.db")
        s.open()
        return s

    @pytest.mark.unit
    def test_enqueue_returns_monotonic_id(self, tmp_path):
        s = self._open(tmp_path)
        try:
            id1 = s.enqueue_notification("first", "info", "lifecycle")
            id2 = s.enqueue_notification("second", "info", "lifecycle")
            assert id1 is not None and id2 is not None
            assert id2 > id1
        finally:
            s.close()

    @pytest.mark.unit
    def test_next_pending_returns_oldest_first(self, tmp_path):
        s = self._open(tmp_path)
        try:
            s.enqueue_notification("older", "info", "lifecycle", ts=1000)
            s.enqueue_notification("newer", "info", "lifecycle", ts=2000)
            rows = s.next_pending_notifications(limit=10)
            assert [r[0] for r in rows] == [1000, 2000]
            assert rows[0][2] == "older"
        finally:
            s.close()

    @pytest.mark.unit
    def test_claim_read_revert_and_marksent_delivering(self, tmp_path):
        # F-058: deferred-delivery's SQL is now on the store API. Exercise the
        # claim -> read -> mark-sent(require_delivering) and claim -> revert paths.
        s = self._open(tmp_path)
        try:
            nid = s.enqueue_notification("body!", "failure", "lifecycle")
            assert s.read_notification(nid) == ("body!", "failure", "pending")

            # First claim wins; second (row now 'delivering') loses.
            assert s.claim_notification(nid, now=999) is True
            assert s.claim_notification(nid, now=1000) is False
            row = s._conn.execute(
                "SELECT status, delivering_at, attempts FROM notifications "
                "WHERE id=?", (nid,)).fetchone()
            assert row[0] == "delivering" and row[1] == 999 and row[2] == 1

            # mark-sent requires the row to still be 'delivering'.
            assert s.mark_notification_sent(nid, require_delivering=True) is True
            assert s.read_notification(nid)[2] == "sent"

            # revert_claim takes a delivering row back to pending.
            nid2 = s.enqueue_notification("b2", "info", "lifecycle")
            assert s.claim_notification(nid2) is True
            s.revert_claim(nid2)
            assert s.read_notification(nid2)[2] == "pending"

            # read_notification on a missing id is None.
            assert s.read_notification(999999) is None
        finally:
            s.close()

    @pytest.mark.unit
    def test_marksent_require_delivering_no_op_when_cancelled(self, tmp_path):
        # F-058: a classifier that cancels the row mid-send must not be clobbered
        # back to 'sent' by require_delivering=True.
        s = self._open(tmp_path)
        try:
            nid = s.enqueue_notification("x", "info", "lifecycle")
            s.claim_notification(nid)
            s.cancel_notification(nid, "superseded")   # classifier wins
            s.mark_notification_sent(nid, require_delivering=True)
            assert s.read_notification(nid)[2] == "cancelled"
        finally:
            s.close()

    @pytest.mark.unit
    def test_mark_sent_removes_row_from_pending(self, tmp_path):
        s = self._open(tmp_path)
        try:
            id1 = s.enqueue_notification("x", "info", "lifecycle")
            s.mark_notification_sent(id1, sent_at=12345)
            assert s.pending_notification_count() == 0
            row = s._conn.execute(
                "SELECT status, sent_at FROM notifications WHERE id=?",
                (id1,),
            ).fetchone()
            assert row == ("sent", 12345)
        finally:
            s.close()

    @pytest.mark.unit
    def test_mark_attempt_keeps_row_pending_and_counts(self, tmp_path):
        s = self._open(tmp_path)
        try:
            id1 = s.enqueue_notification("x", "info", "lifecycle")
            s.mark_notification_attempt(id1)
            s.mark_notification_attempt(id1)
            row = s._conn.execute(
                "SELECT status, attempts FROM notifications WHERE id=?",
                (id1,),
            ).fetchone()
            assert row == ("pending", 2)
            assert s.pending_notification_count() == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_claimed_notification_still_counts_as_outstanding(self, tmp_path):
        """F-079: flush cannot declare an in-flight delivery drained."""
        s = self._open(tmp_path)
        try:
            notification_id = s.enqueue_notification("x", "info", "lifecycle")
            assert s.claim_notification(notification_id) is True
            assert s.pending_notification_count() == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_cancel_marks_with_reason(self, tmp_path):
        s = self._open(tmp_path)
        try:
            id1 = s.enqueue_notification("x", "info", "lifecycle")
            s.cancel_notification(id1, "max_attempts")
            row = s._conn.execute(
                "SELECT status, cancel_reason FROM notifications WHERE id=?",
                (id1,),
            ).fetchone()
            assert row == ("cancelled", "max_attempts")
            assert s.pending_notification_count() == 0
        finally:
            s.close()

    @pytest.mark.unit
    def test_pending_count_excludes_sent_and_cancelled(self, tmp_path):
        s = self._open(tmp_path)
        try:
            id1 = s.enqueue_notification("a", "info", "x")
            id2 = s.enqueue_notification("b", "info", "x")
            s.enqueue_notification("c", "info", "x")  # stays pending
            s.mark_notification_sent(id1)
            s.cancel_notification(id2, "too_old")
            assert s.pending_notification_count() == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_cap_pending_cancels_oldest_excess(self, tmp_path):
        s = self._open(tmp_path)
        try:
            for i in range(5):
                s.enqueue_notification(f"m{i}", "info", "x", ts=1000 + i)
            cancelled = s.cap_pending_notifications(max_pending=2)
            assert cancelled == 3
            assert s.pending_notification_count() == 2
            # The remaining pending rows must be the two NEWEST.
            rows = s.next_pending_notifications(limit=10)
            assert [r[2] for r in rows] == ["m3", "m4"]
            # Cancelled rows carry the overflow reason.
            cur = s._conn.execute(
                "SELECT body, cancel_reason FROM notifications "
                "WHERE status='cancelled' ORDER BY ts ASC"
            )
            cancelled_rows = cur.fetchall()
            assert [r[0] for r in cancelled_rows] == ["m0", "m1", "m2"]
            assert all(r[1] == "backlog_overflow" for r in cancelled_rows)
        finally:
            s.close()

    @pytest.mark.unit
    def test_cap_pending_zero_or_negative_disables_cap(self, tmp_path):
        s = self._open(tmp_path)
        try:
            for i in range(5):
                s.enqueue_notification(f"m{i}", "info", "x")
            assert s.cap_pending_notifications(max_pending=0) == 0
            assert s.cap_pending_notifications(max_pending=-1) == 0
            assert s.pending_notification_count() == 5
        finally:
            s.close()

    @pytest.mark.unit
    def test_prune_deletes_sent_older_than_retention(self, tmp_path):
        s = self._open(tmp_path)
        try:
            now = int(time.time())
            old = s.enqueue_notification("old", "info", "x",
                                         ts=now - 10 * 86400)
            recent = s.enqueue_notification("recent", "info", "x",
                                            ts=now - 1 * 86400)
            s.mark_notification_sent(old, sent_at=now - 10 * 86400)
            s.mark_notification_sent(recent, sent_at=now - 1 * 86400)
            deleted, expired = s.prune_old_notifications(
                retention_days=7, max_age_days=0,
            )
            assert deleted == 1
            assert expired == 0
            ids = {r[0] for r in s._conn.execute(
                "SELECT id FROM notifications"
            )}
            assert ids == {recent}
        finally:
            s.close()

    @pytest.mark.unit
    def test_prune_keeps_pending_within_max_age(self, tmp_path):
        s = self._open(tmp_path)
        try:
            now = int(time.time())
            id1 = s.enqueue_notification("recent", "info", "x",
                                         ts=now - 5 * 86400)
            deleted, expired = s.prune_old_notifications(
                retention_days=7, max_age_days=30,
            )
            assert deleted == 0
            assert expired == 0
            assert s.pending_notification_count() == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_prune_cancels_pending_beyond_max_age(self, tmp_path):
        s = self._open(tmp_path)
        try:
            now = int(time.time())
            ancient = s.enqueue_notification("ancient", "info", "x",
                                             ts=now - 60 * 86400)
            recent = s.enqueue_notification("recent", "info", "x",
                                            ts=now - 5 * 86400)
            deleted, expired = s.prune_old_notifications(
                retention_days=7, max_age_days=30,
            )
            assert deleted == 0
            assert expired == 1
            row = s._conn.execute(
                "SELECT status, cancel_reason FROM notifications "
                "WHERE id=?", (ancient,),
            ).fetchone()
            assert row == ("cancelled", "too_old")
            # Recent pending row untouched.
            row2 = s._conn.execute(
                "SELECT status FROM notifications WHERE id=?",
                (recent,),
            ).fetchone()
            assert row2 == ("pending",)
        finally:
            s.close()

    @pytest.mark.unit
    def test_prune_max_age_zero_means_pending_lives_forever(self, tmp_path):
        """The panic-attack guarantee — pending rows never get dropped
        by TTL when max_age_days <= 0. Only successful delivery (or an
        explicit cancel) removes them."""
        s = self._open(tmp_path)
        try:
            now = int(time.time())
            s.enqueue_notification("ancient", "info", "x",
                                   ts=now - 365 * 86400)
            deleted, expired = s.prune_old_notifications(
                retention_days=7, max_age_days=0,
            )
            assert (deleted, expired) == (0, 0)
            assert s.pending_notification_count() == 1
        finally:
            s.close()

    @pytest.mark.unit
    def test_find_pending_by_category_filters(self, tmp_path):
        s = self._open(tmp_path)
        try:
            s.enqueue_notification("ob", "info", "power_event", ts=1000)
            s.enqueue_notification("ol", "info", "power_event", ts=1100)
            s.enqueue_notification("up", "info", "lifecycle", ts=1050)
            rows = s.find_pending_by_category("power_event")
            assert [r[2] for r in rows] == ["ob", "ol"]
            rows = s.find_pending_by_category("power_event", since_ts=1050)
            assert [r[2] for r in rows] == ["ol"]
        finally:
            s.close()

    @pytest.mark.unit
    def test_open_recovers_inflight_delivering_notifications(self, tmp_path):
        path = tmp_path / "n.db"
        s = StatsStore(path)
        s.open()
        try:
            row_id = s.enqueue_notification("body", "warning", "lifecycle", ts=10)
            stale_claim = int(time.time()) - DELIVERING_RECOVERY_GRACE_SECONDS - 1
            s._conn.execute(
                "UPDATE notifications SET status='delivering', delivering_at=? "
                "WHERE id=?",
                (stale_claim, row_id),
            )
            s._conn.commit()
        finally:
            s.close()

        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT status, sent_at FROM notifications WHERE id=?",
                (row_id,),
            ).fetchone()
        finally:
            s.close()
        assert row == ("pending", None)

    @pytest.mark.unit
    def test_open_does_not_recover_active_delivery_claims(self, tmp_path):
        path = tmp_path / "n.db"
        s = StatsStore(path)
        s.open()
        try:
            row_id = s.enqueue_notification("body", "warning", "lifecycle", ts=10)
            active_claim = int(time.time())
            s._conn.execute(
                "UPDATE notifications SET status='delivering', delivering_at=? "
                "WHERE id=?",
                (active_claim, row_id),
            )
            s._conn.commit()
        finally:
            s.close()

        s = StatsStore(path)
        s.open()
        try:
            row = s._conn.execute(
                "SELECT status, delivering_at FROM notifications WHERE id=?",
                (row_id,),
            ).fetchone()
        finally:
            s.close()
        assert row == ("delivering", active_claim)

    @pytest.mark.unit
    def test_recovery_failure_is_logged_and_propagated(self, tmp_path):
        s = StatsStore(tmp_path / "n.db")
        s.open()
        original = s._conn

        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

        s._conn = _BoomConn()
        try:
            with patch.object(s, "_log_error_once") as log_error:
                with pytest.raises(sqlite3.OperationalError):
                    s._recover_delivering_notifications()
            log_error.assert_called_once()
        finally:
            s._conn = original
            s.close()


class TestMetaKV:
    """Generic meta key/value store helpers (for last_seen_version etc)."""

    @pytest.mark.unit
    def test_get_meta_missing_key_returns_none(self, tmp_path):
        s = StatsStore(tmp_path / "m.db")
        s.open()
        try:
            assert s.get_meta("nonexistent") is None
        finally:
            s.close()

    @pytest.mark.unit
    def test_set_get_meta_round_trip(self, tmp_path):
        s = StatsStore(tmp_path / "m.db")
        s.open()
        try:
            s.set_meta("last_seen_version", "5.1.2")
            assert s.get_meta("last_seen_version") == "5.1.2"
        finally:
            s.close()

    @pytest.mark.unit
    def test_set_meta_overwrites_existing_value(self, tmp_path):
        s = StatsStore(tmp_path / "m.db")
        s.open()
        try:
            s.set_meta("last_seen_version", "5.1.2")
            s.set_meta("last_seen_version", "5.2.0")
            assert s.get_meta("last_seen_version") == "5.2.0"
        finally:
            s.close()


class TestLogEventNotificationSent:
    """B4: log_event records the notification_sent flag (v3+)."""

    @pytest.mark.unit
    def test_default_is_one(self, store):
        store.log_event("ON_BATTERY", "default", ts=42)
        cur = store._conn.execute(
            "SELECT notification_sent FROM events WHERE ts=42"
        )
        assert cur.fetchone()[0] == 1

    @pytest.mark.unit
    def test_explicit_true_is_one(self, store):
        store.log_event("POWER_RESTORED", "ok", ts=100, notification_sent=True)
        assert store._conn.execute(
            "SELECT notification_sent FROM events WHERE ts=100"
        ).fetchone()[0] == 1

    @pytest.mark.unit
    def test_explicit_false_is_zero(self, store):
        store.log_event("VOLTAGE_FLAP_SUPPRESSED", "state=HIGH duration=2s",
                        ts=200, notification_sent=False)
        assert store._conn.execute(
            "SELECT notification_sent FROM events WHERE ts=200"
        ).fetchone()[0] == 0

    @pytest.mark.unit
    def test_audit_query_works(self, store):
        # The user-facing analytical query the column exists for.
        store.log_event("AVR_BOOST_ACTIVE", "...", ts=1, notification_sent=True)
        store.log_event("AVR_BOOST_ACTIVE", "...", ts=2, notification_sent=False)
        store.log_event("AVR_BOOST_ACTIVE", "...", ts=3, notification_sent=False)
        store.log_event("OVER_VOLTAGE_DETECTED", "...", ts=4, notification_sent=True)
        muted = store._conn.execute(
            "SELECT event_type, COUNT(*) FROM events "
            "WHERE notification_sent = 0 GROUP BY event_type"
        ).fetchall()
        assert muted == [("AVR_BOOST_ACTIVE", 2)]


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
# Voltage contextual filter (v5.2.2: drop on-line phantom 0V samples)
# ===========================================================================

class TestInputVoltageContextualFilter:
    """``_to_input_voltage`` drops 0V (or negative) input.voltage readings
    only when ups.status indicates the mains is supposed to be present.
    On battery / FSD, 0V is a real measurement and must survive.
    """

    @pytest.mark.unit
    def test_zero_voltage_on_line_dropped(self):
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("0", ups_status="OL") is None
        assert _to_input_voltage("0.0", ups_status="OL CHRG") is None
        assert _to_input_voltage(0, ups_status="OL") is None

    @pytest.mark.unit
    def test_zero_voltage_on_battery_kept(self):
        # Real outage: input is 0V, output held by inverter. Must persist
        # so the graph + aggregates show the dip.
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("0", ups_status="OB DISCHRG") == 0.0
        assert _to_input_voltage("0.0", ups_status="OB") == 0.0
        assert _to_input_voltage("0", ups_status="OB LB") == 0.0

    @pytest.mark.unit
    def test_zero_voltage_on_fsd_kept(self):
        # Forced-shutdown state: mains is gone, 0V is the truth.
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("0", ups_status="OB FSD") == 0.0
        assert _to_input_voltage("0", ups_status="FSD") == 0.0

    @pytest.mark.unit
    def test_normal_voltage_passes_through(self):
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("230.5", ups_status="OL") == 230.5
        assert _to_input_voltage("120.0", ups_status="OL CHRG") == 120.0
        assert _to_input_voltage("100", ups_status="OB") == 100.0

    @pytest.mark.unit
    def test_empty_and_non_numeric_return_none(self):
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("", ups_status="OL") is None
        assert _to_input_voltage(None, ups_status="OL") is None
        assert _to_input_voltage("abc", ups_status="OL") is None

    @pytest.mark.unit
    def test_negative_voltage_on_line_dropped(self):
        # Some buggy drivers report -1.0 to signal "no reading". Treat
        # the same as 0 -- a sensor signal, not a real measurement.
        from eneru.stats import _to_input_voltage
        assert _to_input_voltage("-1.0", ups_status="OL") is None


class TestBatteryChargeContextualFilter:
    """``_to_battery_charge`` drops a phantom 0% (or negative) battery.charge
    only while on line power; on battery / FSD a low reading is real depletion
    history and must survive (mirrors the input-voltage filter, v6.1.4)."""

    @pytest.mark.unit
    def test_zero_charge_on_line_dropped(self):
        # The monitoring-only-UPS partial-poll case behind the "spurious 0%
        # spikes" bug: a transient 0 while OL is not a real deep discharge.
        from eneru.stats import _to_battery_charge
        assert _to_battery_charge("0", ups_status="OL") is None
        assert _to_battery_charge("0.0", ups_status="OL CHRG") is None
        assert _to_battery_charge(0, ups_status="ALARM OL") is None
        assert _to_battery_charge("-1", ups_status="OL") is None

    @pytest.mark.unit
    def test_zero_charge_on_battery_or_fsd_kept(self):
        # Deep outage: a genuine low/zero charge must persist so real depletion
        # shows on the graph + aggregates.
        from eneru.stats import _to_battery_charge
        assert _to_battery_charge("0", ups_status="OB DISCHRG") == 0.0
        assert _to_battery_charge("0", ups_status="OL OB") == 0.0   # transient flap
        assert _to_battery_charge("0", ups_status="OB FSD") == 0.0
        assert _to_battery_charge("0", ups_status="FSD") == 0.0

    @pytest.mark.unit
    def test_normal_charge_passes_through(self):
        from eneru.stats import _to_battery_charge
        assert _to_battery_charge("100", ups_status="OL") == 100.0
        assert _to_battery_charge("42.5", ups_status="OB DISCHRG") == 42.5

    @pytest.mark.unit
    def test_empty_and_non_numeric_return_none(self):
        from eneru.stats import _to_battery_charge
        assert _to_battery_charge("", ups_status="OL") is None
        assert _to_battery_charge(None, ups_status="OB") is None
        assert _to_battery_charge("abc", ups_status="OL") is None

    @pytest.mark.unit
    def test_sample_roundtrip_filters_on_line_zero(self, store):
        """End-to-end: buffer_sample with status=OL + input.voltage=0
        results in a NULL input_voltage row in SQLite."""
        store.buffer_sample({
            "ups.status": "OL CHRG",
            "battery.charge": "100",
            "input.voltage": "0",
            "output.voltage": "230.0",
        })
        store.flush()
        row = store._conn.execute(
            "SELECT input_voltage FROM samples"
        ).fetchone()
        assert row[0] is None

    @pytest.mark.unit
    def test_sample_roundtrip_keeps_on_battery_zero(self, store):
        """End-to-end: buffer_sample with status=OB + input.voltage=0
        keeps the 0.0 in SQLite (real outage signal)."""
        store.buffer_sample({
            "ups.status": "OB DISCHRG",
            "battery.charge": "85",
            "input.voltage": "0",
            "output.voltage": "230.0",
        })
        store.flush()
        row = store._conn.execute(
            "SELECT input_voltage, status FROM samples"
        ).fetchone()
        assert row[0] == 0.0
        assert row[1] == "OB DISCHRG"


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
            with store._buffer_lock:
                assert len(store._buffer) == 1
        finally:
            store._conn = original

    @pytest.mark.unit
    def test_flush_failure_preserves_newer_samples_when_buffer_is_full(self, tmp_path):
        store = StatsStore(tmp_path / "x.db", buffer_maxlen=2)
        store.open()
        store.buffer_sample(SAMPLE_UPS_DATA, ts=1)
        store.buffer_sample(SAMPLE_UPS_DATA, ts=2)

        class _BoomConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def executemany(self, *a, **k):
                store.buffer_sample(SAMPLE_UPS_DATA, ts=3)
                raise sqlite3.OperationalError("disk I/O error")

        original = store._conn
        store._conn = _BoomConn()
        try:
            assert store.flush() == 0
            with store._buffer_lock:
                assert [row[0] for row in store._buffer] == [2, 3]
        finally:
            store._conn = original
            store.close()


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

    @pytest.mark.unit
    def test_aggregate_watermark_incremental_matches_full(self, tmp_path):
        """ISS-037: incremental (watermarked) aggregation must produce the
        byte-identical agg_5min / agg_hourly tables as a single full-table
        re-aggregation. One store inserts everything then aggregates once;
        another inserts + aggregates in three time-ordered chunks (which
        advances the watermark between runs). The finalized-bucket rows
        must match exactly, proving the watermark never skips or corrupts a
        bucket."""
        base = (20_000_000 // BUCKET_HOURLY) * BUCKET_HOURLY
        # 15 five-minute buckets (>1 hour, so the hourly tier rolls too),
        # 4 samples each.
        all_ts = [
            base + b * BUCKET_5MIN + i
            for b in range(15)
            for i in range(4)
        ]

        full = StatsStore(tmp_path / "full.db")
        full.open()
        inc = StatsStore(tmp_path / "inc.db")
        inc.open()
        try:
            for ts in all_ts:
                full.buffer_sample(SAMPLE_UPS_DATA, ts=ts)
            full.flush()
            full.aggregate()

            chunk = len(all_ts) // 3
            for c in range(3):
                part = (all_ts[c * chunk:(c + 1) * chunk]
                        if c < 2 else all_ts[c * chunk:])
                for ts in part:
                    inc.buffer_sample(SAMPLE_UPS_DATA, ts=ts)
                inc.flush()
                inc.aggregate()

            cols5 = ("ts, battery_charge_avg, battery_charge_min, "
                     "battery_charge_max, samples_count, ups_load_avg, "
                     "input_voltage_avg")
            f5 = full._conn.execute(
                f"SELECT {cols5} FROM agg_5min ORDER BY ts").fetchall()
            i5 = inc._conn.execute(
                f"SELECT {cols5} FROM agg_5min ORDER BY ts").fetchall()
            assert f5 == i5
            assert len(f5) == 15

            colsh = "ts, battery_charge_avg, samples_count, ups_load_avg"
            fh = full._conn.execute(
                f"SELECT {colsh} FROM agg_hourly ORDER BY ts").fetchall()
            ih = inc._conn.execute(
                f"SELECT {colsh} FROM agg_hourly ORDER BY ts").fetchall()
            assert fh == ih

            # Watermark persists in the key/value meta table (no schema
            # column change → no SCHEMA_VERSION bump) and has advanced.
            assert inc.get_meta("agg_5min_watermark") is not None
            assert inc.get_meta("agg_hourly_watermark") is not None
        finally:
            full.close()
            inc.close()

    @pytest.mark.unit
    def test_aggregate_watermark_does_not_bump_schema_version(self, store):
        """ISS-037 stores the watermark as meta rows, not columns, so the
        on-disk SCHEMA_VERSION is unchanged."""
        from eneru.stats import SCHEMA_VERSION
        store.buffer_sample(SAMPLE_UPS_DATA, ts=21_000_000)
        store.flush()
        store.aggregate()
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


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
    def test_purge_trims_old_battery_health_and_self_tests(self, tmp_path):
        # battery_health/self_tests grow unbounded at a small update_interval;
        # purge() must trim rows older than the hourly cutoff (mirroring events).
        s = StatsStore(tmp_path / "purge_v7.db",
                       retention_hourly_days=1)
        s.open()
        try:
            now = int(time.time())
            old = now - 2 * 86400      # 2 days old -> beyond 1-day hourly window
            recent = now - 60
            # battery_health is keyed on ts.
            s.record_battery_health(50.0, {"capacity": 50.0}, ts=old)
            s.record_battery_health(60.0, {"capacity": 60.0}, ts=recent)
            # self_tests is keyed on started_ts.
            s.record_self_test("test.battery.start", "cli", started_ts=old)
            s.record_self_test("test.battery.start", "cli", started_ts=recent)

            s.purge()

            bh = s._conn.execute("SELECT COUNT(*) FROM battery_health").fetchone()[0]
            stc = s._conn.execute("SELECT COUNT(*) FROM self_tests").fetchone()[0]
            assert bh == 1     # only the recent battery_health row survives
            assert stc == 1    # only the recent self_test row survives
            # And the survivor is the recent one in each table.
            assert s.query_battery_health(0, now * 2)[0]["ts"] == recent
            assert s.latest_self_test()["started_ts"] == recent
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
    def test_query_recent_events_include_id_and_start_ts(self, store):
        store.log_event("A", "a", ts=1000)
        store.log_event("B", "b", ts=2000)
        store.log_event("C", "c", ts=3000)
        # Default shape stays a 3-tuple (TUI/legacy callers unaffected).
        assert store.query_recent_events(end_ts=9999, limit=10)[0] == \
            (1000, "A", "a")
        # include_id prepends the row id; start_ts bounds the lower edge.
        rows = store.query_recent_events(
            end_ts=9999, limit=10, start_ts=2000, include_id=True)
        assert [(r[1], r[2]) for r in rows] == [(2000, "B"), (3000, "C")]
        assert all(isinstance(r[0], int) for r in rows)

    @pytest.mark.unit
    def test_delete_events_by_id_with_guard(self, store):
        store.log_event("A", "a", ts=1000)
        store.log_event("B", "b", ts=2000)
        store.log_event("C", "c", ts=3000)
        rows = store.query_recent_events(end_ts=9999, limit=10, include_id=True)
        by_type = {r[2]: r for r in rows}
        # Delete A and C by exact (id, ts, type).
        n = store.delete_events([
            (by_type["A"][0], 1000, "A"), (by_type["C"][0], 3000, "C")])
        assert n == 2
        left = store.query_recent_events(end_ts=9999, limit=10)
        assert [r[1] for r in left] == ["B"]

    @pytest.mark.unit
    def test_delete_events_guard_mismatch_deletes_nothing(self, store):
        store.log_event("A", "a", ts=1000)
        rid = store.query_recent_events(end_ts=9999, limit=1, include_id=True)[0][0]
        # Right id, wrong ts -> 0 (a stale client can't delete the wrong row).
        assert store.delete_events([(rid, 9999, "A")]) == 0
        # Right id, wrong type -> 0.
        assert store.delete_events([(rid, 1000, "WRONG")]) == 0
        assert len(store.query_recent_events(end_ts=9999, limit=10)) == 1

    @pytest.mark.unit
    def test_delete_events_dedups_and_handles_empty(self, store):
        store.log_event("A", "a", ts=1000)
        rid = store.query_recent_events(end_ts=9999, limit=1, include_id=True)[0][0]
        assert store.delete_events([]) == 0
        # Duplicate of the same row counts once.
        assert store.delete_events([(rid, 1000, "A"), (rid, 1000, "A")]) == 1

    @pytest.mark.unit
    def test_delete_events_isolated_per_db(self, tmp_path):
        # The per-DB id is not globally unique: deleting id=1 from one UPS DB must
        # not touch id=1 in another.
        a = StatsStore(tmp_path / "a.db")
        a.open()
        b = StatsStore(tmp_path / "b.db")
        b.open()
        try:
            a.log_event("X", "ax", ts=100)
            b.log_event("X", "bx", ts=100)
            aid = a.query_recent_events(end_ts=9999, limit=1, include_id=True)[0][0]
            assert a.delete_events([(aid, 100, "X")]) == 1
            assert len(a.query_recent_events(end_ts=9999, limit=10)) == 0
            assert len(b.query_recent_events(end_ts=9999, limit=10)) == 1  # untouched
        finally:
            a.close()
            b.close()

    @pytest.mark.unit
    def test_query_recent_events_paging_inclusive_end_ts(self, store):
        # "Load older" pages by lowering end_ts (inclusive) to the oldest ts shown.
        # The one-row overlap at the boundary is intentional — the dashboard
        # de-dups by (source, id) — and paging still reaches every event.
        for i, et in enumerate(("A", "B", "C", "D")):
            store.log_event(et, et.lower(), ts=1000 * (i + 1))   # 1000..4000
        page1 = store.query_recent_events(end_ts=9999, limit=2, include_id=True)
        assert [r[2] for r in page1] == ["C", "D"]
        page2 = store.query_recent_events(end_ts=page1[0][1], limit=2, include_id=True)
        assert [r[2] for r in page2] == ["B", "C"]    # inclusive overlap on C
        page3 = store.query_recent_events(end_ts=page2[0][1], limit=2, include_id=True)
        assert [r[2] for r in page3] == ["A", "B"]
        assert {r[2] for r in page1 + page2 + page3} == {"A", "B", "C", "D"}

    @pytest.mark.unit
    def test_query_recent_events_same_second_not_split_within_limit(self, store):
        # Events sharing one second are returned together (ordered by id) when the
        # limit covers them — so a same-second cluster is never half-returned.
        for et in ("A", "B", "C"):
            store.log_event(et, et.lower(), ts=5000)
        rows = store.query_recent_events(end_ts=9999, limit=10, include_id=True)
        assert [r[2] for r in rows] == ["A", "B", "C"]

    @pytest.mark.unit
    def test_query_recent_events_before_id_advances_within_same_second(self, store):
        for et in ("A", "B", "C"):
            store.log_event(et, et.lower(), ts=5000)
        page1 = store.query_recent_events(end_ts=9999, limit=2, include_id=True)
        assert [r[2] for r in page1] == ["B", "C"]
        page2 = store.query_recent_events(
            end_ts=page1[0][1],
            before_id=page1[0][0],
            limit=2,
            include_id=True,
        )
        assert [r[2] for r in page2] == ["A"]

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

    @pytest.mark.unit
    def test_returns_none_when_sqlite_open_fails(self, tmp_path):
        path = tmp_path / "broken.db"
        path.write_text("not sqlite")
        assert StatsStore.open_readonly(path) is None


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
            # ISS-054: poll for the drain instead of a fixed sleep. The old
            # `sleep(0.2); assert count == 5` could flip on a stalled runner
            # where the writer hadn't flushed yet. Bounded so it can't hang.
            deadline = time.monotonic() + 2.0
            count = 0
            while time.monotonic() < deadline:
                count = store._conn.execute(
                    "SELECT COUNT(*) FROM samples").fetchone()[0]
                if count == 5:
                    break
                time.sleep(0.01)
            assert count == 5
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
    def test_purge_aligns_raw_cutoff_to_bucket_boundary(self, tmp_path):
        """M5: the raw cutoff is aligned DOWN to a 5-min bucket boundary so purge
        deletes only WHOLE buckets. It must NOT trim the early samples of the
        bucket straddling the rolling cutoff -- doing so would let the next
        aggregate() re-derive that finalized bucket from a reduced sample set
        and corrupt its avg/min/max (which then propagates to the hourly tier)."""
        from eneru.stats import BUCKET_5MIN
        s = StatsStore(tmp_path / "boundary.db", retention_raw_hours=1)
        s.open()
        try:
            now = int(time.time())
            aligned = (now - 3600) // BUCKET_5MIN * BUCKET_5MIN
            older_ts = aligned - 1     # in the previous, fully-expired bucket
            straddle_ts = aligned + 1  # in the bucket straddling the rolling cutoff
            s.buffer_sample(SAMPLE_UPS_DATA, ts=older_ts)
            s.buffer_sample(SAMPLE_UPS_DATA, ts=straddle_ts)
            s.flush()
            # Pin time so the aligned cutoff is deterministic.
            with patch("eneru.stats.time.time", return_value=now):
                s.purge()
            tss = [r[0] for r in s._conn.execute(
                "SELECT ts FROM samples ORDER BY ts ASC").fetchall()]
            assert older_ts not in tss       # whole expired bucket deleted
            assert straddle_ts in tss        # straddling bucket's sample kept
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
    @pytest.mark.no_stats_isolation
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
    @pytest.mark.no_stats_isolation
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


# ===========================================================================
# 5.1.1 (CodeRabbit): isolate_stats_db_directory fixture must tolerate
# StatsConfig signature growth. The fixture is the user-facing guard
# against test runs writing into /var/lib/eneru, so its `*args/**kw`
# wrapper has to be exercised across the supported call shapes.
# ===========================================================================


class TestIsolateStatsDbDirectoryFixture:
    """The autouse fixture wraps StatsConfig.__init__ with a *args/**kw
    shim. Verify it injects the isolated default ONLY when the caller
    didn't supply one — keyword and positional callers must keep their
    explicit value."""

    @pytest.mark.unit
    def test_default_construction_uses_isolated_path(self, tmp_path):
        # No args, no kwargs → the autouse fixture must inject its
        # tmp_path-rooted default; the production "/var/lib/eneru" must
        # never appear here.
        cfg = StatsConfig()
        assert cfg.db_directory != "/var/lib/eneru"
        assert "stats" in cfg.db_directory  # fixture writes under tmp_path/stats

    @pytest.mark.unit
    def test_explicit_keyword_db_directory_is_preserved(self):
        # Caller passed db_directory by keyword → fixture must NOT
        # overwrite it.
        cfg = StatsConfig(db_directory="/srv/explicit")
        assert cfg.db_directory == "/srv/explicit"

    @pytest.mark.unit
    def test_explicit_positional_db_directory_is_preserved(self):
        # Caller passed db_directory positionally → fixture must NOT
        # overwrite it. Today db_directory is the first dataclass
        # field; if a future field is added before it, this test will
        # fail loud and the fixture wrapper needs another look.
        cfg = StatsConfig("/srv/positional")
        assert cfg.db_directory == "/srv/positional"

    @pytest.mark.unit
    def test_other_kwargs_pass_through(self):
        # The wrapper must forward unrelated kwargs untouched.
        from eneru.config import StatsRetentionConfig
        retention = StatsRetentionConfig(raw_hours=48)
        cfg = StatsConfig(retention=retention)
        # db_directory still gets the isolated default…
        assert cfg.db_directory != "/var/lib/eneru"
        # …and the explicit retention came through.
        assert cfg.retention.raw_hours == 48


class TestStatsStoreErrorIsolation:
    """Stats are diagnostic only — every write/read path swallows
    sqlite3.Error and OSError, logs once, and returns a safe value.
    A broken DB must never crash the daemon or block the shutdown
    path. This locks the contract for every API."""

    def _broken_store(self, tmp_path):
        # Construct without open() so no real SQLite connection is allocated.
        # __init__ already initializes _conn=None and _db_lock; we substitute
        # a sabotaged connection that raises on every execute() so each
        # public API hits its sqlite3.Error fallback path.
        store = StatsStore(tmp_path / "default.db")
        broken_conn = type("BrokenConn", (), {
            "execute": staticmethod(lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("db locked"))),
            "close": staticmethod(lambda: None),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        store._conn = broken_conn
        return store

    @pytest.mark.unit
    def test_query_recent_events_returns_empty_on_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        rows = store.query_recent_events(end_ts=1000, limit=10)
        assert rows == []

    @pytest.mark.unit
    def test_query_events_returns_empty_on_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        rows = store.query_events(start_ts=0, end_ts=1000)
        assert rows == []

    @pytest.mark.unit
    def test_find_pending_by_category_returns_empty_on_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        rows = store.find_pending_by_category("lifecycle")
        assert rows == []

    @pytest.mark.unit
    def test_pending_notification_count_returns_zero_on_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        assert store.pending_notification_count() == 0

    @pytest.mark.unit
    def test_mark_notification_sent_swallows_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        store.mark_notification_sent(notification_id=1)  # Must not raise

    @pytest.mark.unit
    def test_mark_notification_attempt_swallows_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        store.mark_notification_attempt(notification_id=1)  # Must not raise

    @pytest.mark.unit
    def test_cancel_notification_swallows_sqlite_error(self, tmp_path):
        store = self._broken_store(tmp_path)
        store.cancel_notification(notification_id=1, reason="superseded")  # Must not raise

    @pytest.mark.unit
    def test_pending_notification_count_with_no_conn_returns_zero(self, tmp_path):
        """When _conn is None the count must short-circuit to 0 without
        even trying to access the connection."""
        store = StatsStore(tmp_path / "default.db")
        # Never open() — _conn stays None
        assert store.pending_notification_count() == 0

    @pytest.mark.unit
    def test_find_pending_by_category_with_no_conn_returns_empty(self, tmp_path):
        store = StatsStore(tmp_path / "default.db")
        assert store.find_pending_by_category("lifecycle") == []

    @pytest.mark.unit
    def test_mark_notification_sent_with_no_conn_is_noop(self, tmp_path):
        store = StatsStore(tmp_path / "default.db")
        store.mark_notification_sent(notification_id=1)  # Must not raise


class TestStatsHelpers:
    """Module-level helpers for type coercion."""

    @pytest.mark.unit
    def test_to_int_returns_int_for_numeric_string(self):
        from eneru.stats import _to_int
        assert _to_int("42") == 42
        assert _to_int("42.7") == 42  # Float gets truncated by int()

    @pytest.mark.unit
    def test_to_int_returns_none_for_non_numeric(self):
        from eneru.stats import _to_int
        assert _to_int("not-a-number") is None
        assert _to_int(None) is None
        assert _to_int("") is None


# ====================================================================
# Defensive branches: closed-connection guards + sqlite error handling
# ====================================================================


class TestClosedConnectionGuards:
    """Every public method must handle ``_conn is None`` without raising.

    A common operational case: the daemon shuts down, but a background
    thread fires one last call into the store. These guards keep the
    daemon's shutdown clean (lines 607, 649, 677, 747, 766, 849, 884,
    899)."""

    def _closed(self, tmp_path):
        s = StatsStore(tmp_path / "n.db")
        # Never opened -- ``_conn`` is None.
        return s

    @pytest.mark.unit
    def test_query_recent_events_returns_empty_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        assert s.query_recent_events(end_ts=0, limit=10) == []

    @pytest.mark.unit
    def test_enqueue_notification_returns_none_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        assert s.enqueue_notification("b", "info", "x") is None

    @pytest.mark.unit
    def test_next_pending_notifications_returns_empty_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        assert s.next_pending_notifications() == []

    @pytest.mark.unit
    def test_mark_notification_attempt_no_ops_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        # Must not raise.
        s.mark_notification_attempt(1)

    @pytest.mark.unit
    def test_cancel_notification_no_ops_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        s.cancel_notification(1, "any")

    @pytest.mark.unit
    def test_prune_returns_zero_zero_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        assert s.prune_old_notifications(7, 30) == (0, 0)

    @pytest.mark.unit
    def test_get_meta_returns_none_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        assert s.get_meta("k") is None

    @pytest.mark.unit
    def test_set_meta_no_ops_when_closed(self, tmp_path):
        s = self._closed(tmp_path)
        s.set_meta("k", "v")


class TestV7ConnRaceUnderLock:
    """The v7 store APIs must re-check ``_conn`` AFTER acquiring ``_db_lock``.

    ``close()`` nulls ``_conn`` while holding the lock, so a pre-lock-only check
    races a concurrent close. We simulate the race by nulling ``_conn`` from
    inside the lock (so the pre-lock check passed but the in-lock body sees
    None) and asserting every v7 method returns safely instead of crashing on
    ``None.execute``.
    """

    def _store_that_nulls_conn_under_lock(self, tmp_path):
        s = StatsStore(tmp_path / "race.db")
        s.open()
        real_conn = s._conn

        class _RaceLock:
            def __enter__(self_lock):
                # Pre-lock check already saw a live _conn; the acquirer now
                # races a close() that nulls it under the lock.
                s._conn = None
                return True

            def __exit__(self_lock, *exc):
                # Restore so the NEXT method's pre-lock check passes and we
                # exercise ITS post-lock re-check too (not just the first call's).
                s._conn = real_conn
                return False

        s._db_lock = _RaceLock()
        return s, real_conn

    @pytest.mark.unit
    def test_all_v7_methods_survive_conn_race(self, tmp_path):
        s, real_conn = self._store_that_nulls_conn_under_lock(tmp_path)
        try:
            # None of these may raise; they return the safe empty/None sentinel.
            assert s.record_battery_health(50.0, {"capacity": 50.0}) is None
            assert s.query_battery_health(0, 9999) == []
            assert s.record_self_test("test.battery.start", "scheduler") is None
            assert s.update_self_test_result(
                1, result_raw="x", result_enum="passed") is None
            assert s.latest_self_test() is None
            assert s.latest_running_self_test() is None
            assert s.power_samples(0, 9999) == []
            assert s.query_range("battery_charge", 0, 9999) == []
        finally:
            real_conn.close()


class TestPreExistingConnRaceUnderLock:
    """The pre-existing read methods must ALSO re-check ``_conn`` AFTER acquiring
    ``_db_lock`` (the same TOCTOU window the v7 methods already guarded).

    ``close()`` nulls ``_conn`` under the lock, so a pre-lock-only check races a
    concurrent close. We simulate it by nulling ``_conn`` from inside the lock
    and asserting each method returns its safe empty default instead of crashing
    on ``None.execute``.
    """

    def _store_that_nulls_conn_under_lock(self, tmp_path):
        s = StatsStore(tmp_path / "race_pre.db")
        s.open()
        real_conn = s._conn

        class _RaceLock:
            def __enter__(self_lock):
                s._conn = None
                return True

            def __exit__(self_lock, *exc):
                # Restore so the NEXT method's pre-lock check passes and we
                # exercise ITS post-lock re-check too (not just the first call's).
                s._conn = real_conn
                return False

        s._db_lock = _RaceLock()
        return s, real_conn

    @pytest.mark.unit
    def test_pre_existing_read_methods_survive_conn_race(self, tmp_path):
        s, real_conn = self._store_that_nulls_conn_under_lock(tmp_path)
        try:
            # Each must return its method-specific safe empty value, not raise.
            assert s.query_events(0, 9999) == []
            assert s.query_recent_events(end_ts=9999, limit=10) == []
            assert s.next_pending_notifications() == []
            assert s.find_pending_by_category("health") == []
            assert s.find_pending_by_category("health", since_ts=0) == []
            assert s.pending_notification_count() == 0
            assert s.get_meta("schema_version") is None
        finally:
            real_conn.close()


class TestWriteHelperConnRaceUnderLock:
    """The write methods routed through ``_write()`` must re-check ``_conn``
    AFTER acquiring ``_db_lock``.

    ``_write()`` is the single place that takes the lock, re-binds the
    connection, and opens the transaction. ``close()`` nulls ``_conn`` under the
    lock, so a caller's pre-lock check races a concurrent close. We simulate it
    by nulling ``_conn`` from inside the lock (pre-lock check passed; the helper
    then sees None and yields None) and assert every write returns its safe
    sentinel instead of crashing on ``None.execute``.
    """

    def _store_that_nulls_conn_under_lock(self, tmp_path):
        s = StatsStore(tmp_path / "race_write.db")
        s.open()
        real_conn = s._conn

        class _RaceLock:
            def __enter__(self_lock):
                s._conn = None
                return True

            def __exit__(self_lock, *exc):
                # Restore so the NEXT method's pre-lock check passes and we
                # exercise ITS post-lock re-check too (not just the first call's).
                s._conn = real_conn
                return False

        s._db_lock = _RaceLock()
        return s, real_conn

    @pytest.mark.unit
    def test_write_methods_survive_conn_race(self, tmp_path):
        s, real_conn = self._store_that_nulls_conn_under_lock(tmp_path)
        try:
            # Each must return its method-specific safe value, not raise.
            assert s.aggregate() == (0, 0)
            assert s.purge() == (0, 0, 0)
            s.log_event("DAEMON_START")                     # no return
            assert s.delete_events([(1, 1, "DAEMON_START")]) == 0
            assert s.enqueue_notification("b", "info", "cat") is None
            s.mark_notification_sent(1)                      # no return
            s.mark_notification_attempt(1)                   # no return
            s.cancel_notification(1, "too_old")              # no return
            assert s.cap_pending_notifications(5) == 0
            assert s.prune_old_notifications(7, 30) == (0, 0)
            s.set_meta("k", "v")                             # no return
        finally:
            real_conn.close()


class TestStatsOpenErrors:
    """``open()`` must surface a clear log when the parent mkdir fails
    (stats.py lines 184-186)."""

    @pytest.mark.unit
    def test_open_logs_and_raises_when_mkdir_fails(self, monkeypatch, tmp_path):
        target = tmp_path / "nested" / "n.db"
        store = StatsStore(target)
        logged = []
        store._log_error_once = lambda msg: logged.append(msg)

        from pathlib import Path as _P
        real_mkdir = _P.mkdir

        def boom(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(_P, "mkdir", boom)
        try:
            with pytest.raises(OSError):
                store.open()
        finally:
            monkeypatch.setattr(_P, "mkdir", real_mkdir)
        assert any("stats: mkdir" in m for m in logged)


class TestSafeAlterIdempotent:
    """``_safe_alter`` swallows duplicate-column OperationalError
    (stats.py lines 399-401)."""

    @pytest.mark.unit
    def test_safe_alter_swallows_duplicate_column(self, tmp_path):
        s = StatsStore(tmp_path / "n.db")
        s.open()
        try:
            # First ALTER should succeed (or be a no-op if already there);
            # the second ALTER definitely raises OperationalError, which
            # _safe_alter must swallow.
            s._safe_alter("notifications", "new_col_a TEXT")
            s._safe_alter("notifications", "new_col_a TEXT")  # duplicate
        finally:
            s.close()


class TestMigrationCorruptSchemaVersion:
    """When the ``schema_version`` meta value is corrupt, migrations must
    fall back to v1 (stats.py lines 335-336)."""

    @pytest.mark.unit
    def test_corrupt_schema_version_falls_back_to_v1(self, tmp_path):
        import sqlite3 as _sql
        path = tmp_path / "broken.db"
        # Hand-craft a v1-shaped DB with a non-numeric schema_version so
        # `int(cur[0])` raises ValueError and the guard sets current=1,
        # which causes every later migration to run.
        conn = _sql.connect(str(path))
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO meta(key, value) VALUES ('schema_version', 'oops');"
            "CREATE TABLE samples (ts INTEGER);"
            # Real v1 events shape (event_type/detail present) so the v5
            # table-rebuild can copy columns; the test's point is the corrupt
            # schema_version falling back to v1, not a malformed events table.
            "CREATE TABLE events (ts INTEGER NOT NULL, event_type TEXT NOT NULL, "
            "detail TEXT);"
            "CREATE TABLE agg_5min (ts INTEGER);"
            "CREATE TABLE agg_hourly (ts INTEGER);"
        )
        conn.commit()
        conn.close()

        store = StatsStore(path)
        store.open()
        try:
            # Migration must have proceeded; events table should now have
            # the v3-added column and notifications table must exist.
            cur = store._conn.execute("PRAGMA table_info(events)")
            cols = {row[1] for row in cur.fetchall()}
            assert "notification_sent" in cols
            assert "id" in cols  # v5 rebuild ran too
            cur = store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='notifications'"
            )
            assert cur.fetchone() is not None
        finally:
            store.close()


class _BrokenConn:
    """A sqlite3.Connection-shaped stand-in whose ``execute`` always raises.

    The real ``sqlite3.Connection`` has read-only attributes so we can't
    monkeypatch ``.execute`` on a live connection. Swapping the whole
    ``_conn`` for this stub exercises the broad ``except sqlite3.Error``
    branches without needing a real broken DB.
    """
    def __init__(self, exc):
        self._exc = exc

    def execute(self, *a, **kw):
        raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        return None

    def close(self):
        return None


class TestNotificationSQLiteErrorPaths:
    """SQLite errors during notification CRUD must be logged once + return
    a safe default (lines 660-662, 687-691, 827-831, 872-873, 892-894,
    906-907)."""

    def _open(self, tmp_path):
        s = StatsStore(tmp_path / "n.db")
        s.open()
        return s

    def _swap_conn(self, store):
        real = store._conn
        store._conn = _BrokenConn(sqlite3.Error("simulated disk error"))
        return real

    @pytest.mark.unit
    def test_enqueue_notification_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            assert s.enqueue_notification("x", "info", "lifecycle") is None
            assert any("enqueue_notification failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()

    @pytest.mark.unit
    def test_next_pending_notifications_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            assert s.next_pending_notifications() == []
            assert any("next_pending_notifications failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()

    @pytest.mark.unit
    def test_cap_pending_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            assert s.cap_pending_notifications(max_pending=5) == 0
            assert any("cap_pending_notifications failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()

    @pytest.mark.unit
    def test_prune_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            deleted, expired = s.prune_old_notifications(7, 30)
            assert (deleted, expired) == (0, 0)
            assert any("prune_old_notifications failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()

    @pytest.mark.unit
    def test_get_meta_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            assert s.get_meta("k") is None
            assert any("get_meta failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()

    @pytest.mark.unit
    def test_set_meta_swallows_sqlite_error(self, tmp_path):
        s = self._open(tmp_path)
        logged = []
        s._log_error_once = lambda m: logged.append(m)
        real = self._swap_conn(s)
        try:
            s.set_meta("k", "v")  # must not raise
            assert any("set_meta failed" in m for m in logged)
        finally:
            s._conn = real
            s.close()


class TestAggregateLockSplit:
    """F-044: aggregate() must release _db_lock between the 5-min and hourly
    tiers so the monitor thread isn't starved re-scanning ~86k rows under one
    long transaction; correctness/idempotency must be preserved."""

    def _seed(self, store, count):
        now = int(time.time())
        for i in range(count):
            store.buffer_sample(
                {"ups.status": "OL", "battery.charge": "90", "ups.load": "20"},
                ts=now - count * 10 + i * 10)
        store.flush()

    @pytest.mark.unit
    def test_lock_released_between_tiers(self, tmp_path):
        store = StatsStore(tmp_path / "s.db")
        store.open()
        try:
            self._seed(store, 400)   # ~66 min -> multiple 5-min + hourly buckets

            # The hook runs BETWEEN the two per-tier transactions. A non-reentrant
            # Lock can only be acquired here if aggregate() genuinely released it
            # between tiers (the whole point of F-044).
            free_between = []
            def hook():
                got = store._db_lock.acquire(blocking=False)
                free_between.append(got)
                if got:
                    store._db_lock.release()
            store._aggregate_between_tiers = hook

            store.aggregate()
            assert free_between == [True]  # hook ran once; lock was free
        finally:
            store.close()

    @pytest.mark.unit
    def test_split_aggregation_is_idempotent(self, tmp_path):
        store = StatsStore(tmp_path / "s.db")
        store.open()
        try:
            self._seed(store, 400)
            store.aggregate()
            rows_5 = store._conn.execute(
                "SELECT ts, samples_count FROM agg_5min ORDER BY ts").fetchall()
            rows_h = store._conn.execute(
                "SELECT ts, samples_count FROM agg_hourly ORDER BY ts").fetchall()
            assert rows_5 and rows_h  # sanity: work actually happened
            # Re-running must reproduce the exact same tier rows.
            store.aggregate()
            assert store._conn.execute(
                "SELECT ts, samples_count FROM agg_5min ORDER BY ts"
            ).fetchall() == rows_5
            assert store._conn.execute(
                "SELECT ts, samples_count FROM agg_hourly ORDER BY ts"
            ).fetchall() == rows_h
        finally:
            store.close()

    @pytest.mark.unit
    def test_aggregate_empty_store_is_noop(self, tmp_path):
        """With no samples, the 5-min tier writes no watermark (new_lo_5 is None)
        and the hourly tier finds nothing — the split path still returns (0, 0)."""
        store = StatsStore(tmp_path / "s.db")
        store.open()
        try:
            assert store.aggregate() == (0, 0)
        finally:
            store.close()

    @pytest.mark.unit
    def test_hourly_tier_skipped_when_store_closes_between_tiers(self, tmp_path):
        """If the store is closed by the between-tiers hook (simulating a
        concurrent close winning the re-acquire), the hourly tier no-ops and the
        method returns the 5-min count with a zero hourly count."""
        store = StatsStore(tmp_path / "s.db")
        store.open()
        self._seed(store, 60)
        store._aggregate_between_tiers = store.close
        result = store.aggregate()
        assert result[1] == 0   # hourly tier saw conn is None -> 0


class TestNotificationStoreMethodsDegradeSafely:
    """The promoted notification-store methods (F-058) and the pending-id
    reconciliation query must degrade safely: return their neutral value when
    the store isn't open, and swallow a transient SQLite error rather than
    crashing the caller. These are the defensive branches the coverage bar
    exists to keep exercised on every Python version."""

    @pytest.mark.unit
    def test_return_safe_defaults_when_store_closed(self, tmp_path):
        s = StatsStore(tmp_path / "closed.db")
        s.open()
        s.close()                      # _conn -> None
        assert s._conn is None
        assert s.read_notification(1) is None
        assert s.claim_notification(1) is False
        assert s.revert_claim(1) is None            # returns without raising
        assert s.pending_notification_ids() is None

    @pytest.mark.unit
    def test_swallow_sqlite_errors(self, tmp_path):
        s = StatsStore(tmp_path / "err.db")
        s.open()
        try:
            # Drop the table out from under the methods so their queries raise a
            # real sqlite3.OperationalError ("no such table"), exercising the
            # (sqlite3.Error, OSError) except branch without mocking a read-only
            # connection attribute.
            with s._db_lock:
                s._conn.execute("DROP TABLE IF EXISTS notifications")
                s._conn.commit()
            assert s.read_notification(1) is None
            assert s.claim_notification(1) is False
            s.revert_claim(1)                       # swallowed, no raise
            assert s.pending_notification_ids() is None
        finally:
            s.close()
