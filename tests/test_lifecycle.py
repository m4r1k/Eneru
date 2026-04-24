"""Tests for the v5.2 lifecycle classifier (Slice 3).

Covers the marker-file CRUD helpers and the pure
:func:`classify_startup` function across every branch:
- upgrade marker → 📦 Upgraded
- pip-path upgrade (last_seen vs current) → 📦 Upgraded
- shutdown marker reason=sequence_complete → 📊 Recovered
- shutdown marker reason=fatal → 🚀 Restarted (fatal)
- shutdown marker reason=signal + recent downtime → 🔄 Restarted
- shutdown marker reason=signal + old downtime → 🚀 Started (last seen)
- no marker, last_seen present → 🚀 Started (after crash)
- no marker, no last_seen → 🚀 Started (first install)
"""

import json
import time

import pytest

from eneru.lifecycle import (
    REASON_FATAL,
    REASON_SEQUENCE_COMPLETE,
    REASON_SIGNAL,
    RESTART_DOWNTIME_THRESHOLD_SECS,
    SHUTDOWN_MARKER_NAME,
    UPGRADE_MARKER_NAME,
    classify_startup,
    delete_shutdown_marker,
    delete_upgrade_marker,
    read_shutdown_marker,
    read_upgrade_marker,
    write_shutdown_marker,
)


# ==============================================================================
# Marker file CRUD
# ==============================================================================

class TestShutdownMarker:

    @pytest.mark.unit
    def test_write_then_read_round_trip(self, tmp_path):
        write_shutdown_marker(
            tmp_path, version="5.2.0", reason=REASON_SIGNAL,
            shutdown_at=1700000000,
        )
        marker = read_shutdown_marker(tmp_path)
        assert marker == {
            "shutdown_at": 1700000000,
            "version": "5.2.0",
            "reason": "signal",
        }

    @pytest.mark.unit
    def test_read_returns_none_when_absent(self, tmp_path):
        assert read_shutdown_marker(tmp_path) is None

    @pytest.mark.unit
    def test_read_returns_none_on_invalid_json(self, tmp_path):
        (tmp_path / SHUTDOWN_MARKER_NAME).write_text("{not valid json")
        assert read_shutdown_marker(tmp_path) is None

    @pytest.mark.unit
    def test_delete_idempotent_when_absent(self, tmp_path):
        # Must not raise even when the marker isn't there.
        delete_shutdown_marker(tmp_path)
        delete_shutdown_marker(tmp_path)

    @pytest.mark.unit
    def test_delete_removes_existing_marker(self, tmp_path):
        write_shutdown_marker(tmp_path, version="5.2.0")
        assert (tmp_path / SHUTDOWN_MARKER_NAME).exists()
        delete_shutdown_marker(tmp_path)
        assert not (tmp_path / SHUTDOWN_MARKER_NAME).exists()

    @pytest.mark.unit
    def test_write_creates_directory_if_missing(self, tmp_path):
        target = tmp_path / "nested" / "stats"
        write_shutdown_marker(target, version="5.2.0")
        assert (target / SHUTDOWN_MARKER_NAME).exists()

    @pytest.mark.unit
    def test_write_uses_now_when_shutdown_at_omitted(self, tmp_path):
        before = int(time.time())
        write_shutdown_marker(tmp_path, version="5.2.0")
        after = int(time.time())
        marker = read_shutdown_marker(tmp_path)
        assert before <= marker["shutdown_at"] <= after


class TestUpgradeMarker:

    @pytest.mark.unit
    def test_read_returns_dict_when_present(self, tmp_path):
        (tmp_path / UPGRADE_MARKER_NAME).write_text(
            json.dumps({"old_version": "5.1.2", "new_version": "5.2.0"})
        )
        assert read_upgrade_marker(tmp_path) == {
            "old_version": "5.1.2", "new_version": "5.2.0",
        }

    @pytest.mark.unit
    def test_read_returns_none_when_absent(self, tmp_path):
        assert read_upgrade_marker(tmp_path) is None

    @pytest.mark.unit
    def test_read_returns_none_on_invalid_json(self, tmp_path):
        (tmp_path / UPGRADE_MARKER_NAME).write_text("garbage")
        assert read_upgrade_marker(tmp_path) is None

    @pytest.mark.unit
    def test_delete_idempotent(self, tmp_path):
        delete_upgrade_marker(tmp_path)


# ==============================================================================
# classify_startup — each branch
# ==============================================================================

class TestClassifyStartup:

    @pytest.mark.unit
    def test_upgrade_marker_takes_priority(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 1, "version": "5.1.2",
                             "reason": "signal"},
            upgrade_marker={"old_version": "5.1.2",
                            "new_version": "5.2.0"},
            last_seen_version="5.1.2",
            now_ts=100,
        )
        assert "📦" in body and "Upgraded" in body
        assert "5.1.2" in body and "5.2.0" in body
        assert ntype == "success"

    @pytest.mark.unit
    def test_upgrade_marker_falls_back_to_current_when_new_version_missing(self):
        body, _ = classify_startup(
            current_version="5.2.0",
            shutdown_marker=None,
            upgrade_marker={"old_version": "5.1.2"},  # no new_version
            last_seen_version=None,
            now_ts=100,
        )
        assert "5.1.2" in body and "5.2.0" in body

    @pytest.mark.unit
    def test_pip_path_upgrade_via_last_seen_version_diff(self):
        """No on-disk markers but last_seen_version differs from
        current_version (pip user upgraded between runs)."""
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker=None,
            upgrade_marker=None,
            last_seen_version="5.1.2",
            now_ts=100,
        )
        assert "📦" in body and "Upgraded" in body
        assert "5.1.2" in body and "5.2.0" in body
        assert ntype == "success"

    @pytest.mark.unit
    def test_shutdown_sequence_complete_emits_recovered(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 1000,
                             "version": "5.2.0",
                             "reason": REASON_SEQUENCE_COMPLETE},
            upgrade_marker=None,
            last_seen_version="5.2.0",
            now_ts=23000,  # 22000s downtime
        )
        assert "📊" in body and "Recovered" in body
        assert "power-loss" in body
        assert "5.2.0" in body
        assert ntype == "success"

    @pytest.mark.unit
    def test_shutdown_fatal_emits_restarted_after_fatal(self):
        # Same version on both sides — otherwise the pip-path upgrade
        # branch wins (covered separately in
        # test_pip_upgrade_during_shutdown_marker_combines_both).
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 100,
                             "version": "5.2.0",
                             "reason": REASON_FATAL},
            upgrade_marker=None,
            last_seen_version="5.2.0",
            now_ts=200,
        )
        assert "🚀" in body and "Restarted" in body
        assert "fatally" in body
        assert "5.2.0" in body
        assert ntype == "warning"

    @pytest.mark.unit
    def test_shutdown_signal_recent_downtime_emits_restarted(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 100,
                             "version": "5.2.0",
                             "reason": REASON_SIGNAL},
            upgrade_marker=None,
            last_seen_version="5.2.0",
            now_ts=100 + RESTART_DOWNTIME_THRESHOLD_SECS - 1,
        )
        assert "🔄" in body and "Restarted" in body
        assert ntype == "info"

    @pytest.mark.unit
    def test_shutdown_signal_old_downtime_emits_started_with_last_seen(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 100,
                             "version": "5.2.0",
                             "reason": REASON_SIGNAL},
            upgrade_marker=None,
            last_seen_version="5.2.0",
            now_ts=100 + RESTART_DOWNTIME_THRESHOLD_SECS + 60,
        )
        assert "🚀" in body and "Started" in body and "last seen" in body
        assert "🔄" not in body
        assert ntype == "info"

    @pytest.mark.unit
    def test_no_marker_with_last_seen_emits_after_crash(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker=None,
            upgrade_marker=None,
            last_seen_version="5.2.0",
            now_ts=100,
        )
        assert "🚀" in body and "after crash" in body
        assert ntype == "warning"

    @pytest.mark.unit
    def test_no_marker_no_last_seen_emits_first_start(self):
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker=None,
            upgrade_marker=None,
            last_seen_version=None,
            now_ts=100,
        )
        assert "🚀" in body and "Started" in body
        assert "after crash" not in body
        assert "last seen" not in body
        assert ntype == "info"

    @pytest.mark.unit
    def test_pip_upgrade_during_shutdown_marker_combines_both(self):
        """Edge case: pip user upgraded mid-cycle. Both shutdown marker
        AND a different last_seen_version are present. Should explain
        both via the upgrade phrasing, since the version change is the
        bigger story."""
        body, ntype = classify_startup(
            current_version="5.2.0",
            shutdown_marker={"shutdown_at": 100,
                             "version": "5.1.2",
                             "reason": REASON_SIGNAL},
            upgrade_marker=None,
            last_seen_version="5.1.2",
            now_ts=200,
        )
        assert "📦" in body and "Upgraded" in body
        assert "5.1.2" in body and "5.2.0" in body
        assert ntype == "success"
