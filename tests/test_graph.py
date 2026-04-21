"""Tests for the BrailleGraph renderer used by the TUI."""

import os
from unittest.mock import patch

import pytest

from eneru.graph import BrailleGraph


# ===========================================================================
# Code-point arithmetic against hand-computed glyphs
# ===========================================================================

class TestCodePoint:

    @pytest.mark.unit
    def test_zero_dots_is_blank_braille_pattern(self):
        # U+2800 is the "blank" Braille pattern.
        assert BrailleGraph.code_point(0) == "\u2800"

    @pytest.mark.unit
    def test_all_dots_set_is_full_braille(self):
        assert BrailleGraph.code_point(0xFF) == "\u28FF"

    @pytest.mark.unit
    def test_invalid_dots_raises(self):
        with pytest.raises(ValueError):
            BrailleGraph.code_point(-1)
        with pytest.raises(ValueError):
            BrailleGraph.code_point(0x100)

    @pytest.mark.unit
    def test_top_left_dot_only(self):
        # (col=0, row=0) maps to bit 0x01 -> U+2801
        on = [[True, False, False, False], [False, False, False, False]]
        assert BrailleGraph.cell(on) == "\u2801"

    @pytest.mark.unit
    def test_top_right_dot_only(self):
        # (col=1, row=0) maps to bit 0x08 -> U+2808
        on = [[False, False, False, False], [True, False, False, False]]
        assert BrailleGraph.cell(on) == "\u2808"

    @pytest.mark.unit
    def test_bottom_row_both_columns(self):
        # (0,3)=0x40, (1,3)=0x80 -> 0xC0 -> U+28C0
        on = [
            [False, False, False, True],
            [False, False, False, True],
        ]
        assert BrailleGraph.cell(on) == "\u28C0"


# ===========================================================================
# supported() detection
# ===========================================================================

class TestSupported:

    @pytest.mark.unit
    def test_lang_c_returns_false(self, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        assert BrailleGraph.supported() is False

    @pytest.mark.unit
    def test_utf8_locale_returns_true(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        with patch("eneru.graph.locale.getpreferredencoding",
                   return_value="UTF-8"):
            assert BrailleGraph.supported() is True

    @pytest.mark.unit
    def test_non_utf8_returns_false(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.ISO-8859-1")
        with patch("eneru.graph.locale.getpreferredencoding",
                   return_value="ISO-8859-1"):
            assert BrailleGraph.supported() is False


# ===========================================================================
# plot(): geometry, auto-scale, bounds clipping
# ===========================================================================

class TestPlotGeometry:

    @pytest.mark.unit
    def test_empty_data_returns_blank_rows(self):
        rows = BrailleGraph.plot([], width=10, height=2)
        assert len(rows) == 2
        assert all(r == " " * 10 for r in rows)

    @pytest.mark.unit
    def test_zero_dimensions_return_empty(self):
        assert BrailleGraph.plot([1, 2, 3], width=0, height=3) == ["", "", ""]
        assert BrailleGraph.plot([1, 2, 3], width=10, height=0) == []

    @pytest.mark.unit
    def test_output_height_matches_request(self):
        rows = BrailleGraph.plot([1, 2, 3, 4], width=8, height=5)
        assert len(rows) == 5

    @pytest.mark.unit
    def test_output_width_matches_request(self):
        rows = BrailleGraph.plot([1, 2, 3, 4], width=8, height=2,
                                 force_fallback=True)
        # Each cell is one character wide.
        assert all(len(r) == 8 for r in rows)


class TestPlotAutoScale:

    @pytest.mark.unit
    def test_max_value_is_at_top(self):
        # Plot a single high value at the right edge; it must light up
        # near the top (row 0 in the topmost cell).
        rows = BrailleGraph.plot(
            [10.0], width=4, height=4, y_min=0.0, y_max=10.0,
            force_fallback=True,
        )
        # Single sample -> rightmost cell. Top cell row should not be blank.
        assert rows[0][-1] != " "
        # Bottom cell row should be blank.
        assert rows[-1][-1] == " "

    @pytest.mark.unit
    def test_min_value_is_at_bottom(self):
        rows = BrailleGraph.plot(
            [0.0], width=4, height=4, y_min=0.0, y_max=10.0,
            force_fallback=True,
        )
        # Bottom cell row populated, top cell row blank.
        assert rows[-1][-1] != " "
        assert rows[0][-1] == " "

    @pytest.mark.unit
    def test_zero_range_does_not_divide_by_zero(self):
        # All identical values: the renderer pads y_min/y_max around them.
        rows = BrailleGraph.plot([5.0, 5.0, 5.0], width=4, height=2)
        assert len(rows) == 2  # no exception

    @pytest.mark.unit
    def test_explicit_bounds_override_auto(self):
        # Auto would scale to the data; explicit bounds clip below.
        rows = BrailleGraph.plot(
            [50.0, 50.0], width=2, height=2, y_min=0.0, y_max=100.0,
            force_fallback=True,
        )
        # 50% of the way up in a 2-row (8-pixel-tall) grid -> mid.
        assert any(any(c != " " for c in r) for r in rows)


class TestPlotClipping:

    @pytest.mark.unit
    def test_value_above_y_max_is_clipped_to_top(self):
        rows = BrailleGraph.plot(
            [200.0], width=4, height=4, y_min=0.0, y_max=100.0,
            force_fallback=True,
        )
        assert rows[0][-1] != " "  # clipped to topmost row

    @pytest.mark.unit
    def test_value_below_y_min_is_clipped_to_bottom(self):
        rows = BrailleGraph.plot(
            [-50.0], width=4, height=4, y_min=0.0, y_max=100.0,
            force_fallback=True,
        )
        assert rows[-1][-1] != " "

    @pytest.mark.unit
    def test_none_and_invalid_values_skipped(self):
        # None / non-numeric must not raise; they are simply omitted.
        rows = BrailleGraph.plot(
            [None, "x", 50.0], width=4, height=2,
            y_min=0.0, y_max=100.0,
        )
        assert len(rows) == 2  # didn't crash


# ===========================================================================
# Fallback (block characters)
# ===========================================================================

class TestFallback:

    @pytest.mark.unit
    def test_force_fallback_uses_block_chars(self):
        rows = BrailleGraph.plot(
            list(range(10)), width=4, height=2, force_fallback=True,
        )
        # No braille code points present.
        for r in rows:
            for c in r:
                if c != " ":
                    assert ord(c) < 0x2800 or ord(c) > 0x28FF

    @pytest.mark.unit
    def test_supported_false_uses_fallback(self, monkeypatch):
        monkeypatch.setenv("LANG", "C")
        rows = BrailleGraph.plot([0, 1, 2, 3], width=4, height=2)
        for r in rows:
            for c in r:
                if c != " ":
                    assert ord(c) < 0x2800 or ord(c) > 0x28FF


# ===========================================================================
# render_to_window helper
# ===========================================================================

class TestRenderToWindow:

    @pytest.mark.unit
    def test_calls_addnstr_for_each_row(self):
        captured = []

        class FakeWin:
            def addnstr(self, y, x, text, n, attr=0):
                captured.append((y, x, text[:n], attr))

        BrailleGraph.render_to_window(
            FakeWin(), 5, 3, height=3, width=8,
            data=[1, 2, 3], title="Battery", y_axis_label="0-100%",
        )
        # One row per data row + one title + one label
        ys = sorted({c[0] for c in captured})
        assert 5 in ys and 6 in ys and 7 in ys  # graph rows
        assert 4 in ys  # title row above
        assert 8 in ys  # y-axis label row below

    @pytest.mark.unit
    def test_swallows_window_errors(self):
        class BoomWin:
            def addnstr(self, *a, **k):
                raise RuntimeError("boom")

        # Must not raise -- best-effort renderer.
        BrailleGraph.render_to_window(
            BoomWin(), 0, 0, height=2, width=4,
            data=[1, 2, 3], title="t",
        )


# ===========================================================================
# Time-windowed X positioning (item 5: no phantom dots on long timescales)
# ===========================================================================

# The blank cell is U+2800 in Braille mode and " " in block-fallback mode.
# Detect dynamically so the suite passes under both LANG=C and UTF-8.
_BLANK = BrailleGraph.code_point(0) if BrailleGraph.supported() else " "


class TestPlotXWindow:
    """When x_values + x_min/x_max are provided, samples must land at their
    actual time position within the requested window -- not get spread evenly
    across the full chart width."""

    @pytest.mark.unit
    def test_sparse_data_stays_in_left_portion(self):
        """10 samples spanning t=0..600 inside a t=0..6000 window should
        only paint dots in the leftmost ~10% of a 100-cell row.

        Regression: the previous implementation projected sample index
        onto chart width via ``i * (grid_w - 1) / (n - 1)``, which made
        12h of data look like a full 30d chart.
        """
        n = 10
        x_values = [i * 60 for i in range(n)]  # 0, 60, 120, ..., 540 seconds
        data = list(range(n))
        rows = BrailleGraph.plot(
            data,
            width=100, height=2,
            y_min=0, y_max=n,
            x_values=x_values,
            x_min=0, x_max=6000,   # 100x wider than the actual data span
        )
        # The first row's leftmost 10 cells should contain at least one
        # non-blank Braille char; the right 80% must be entirely blank.
        for row in rows:
            assert len(row) == 100
            right_portion = row[20:]   # cells 20..99 (right 80%)
            assert all(ch == _BLANK for ch in right_portion), (
                "right portion of chart should be empty when data is "
                "confined to the left of the window"
            )

    @pytest.mark.unit
    def test_full_window_data_uses_full_width(self):
        """Sanity: when data does span the full window, dots reach both edges."""
        n = 10
        x_values = [i * 100 for i in range(n)]   # 0, 100, ..., 900
        data = [50 for _ in range(n)]
        rows = BrailleGraph.plot(
            data,
            width=20, height=2,
            y_min=0, y_max=100,
            x_values=x_values,
            x_min=0, x_max=900,   # data spans the whole window
        )
        flat = "".join(rows)
        # At least one non-blank cell in each half of the row.
        left_half = "".join(row[:10] for row in rows)
        right_half = "".join(row[10:] for row in rows)
        assert any(ch != _BLANK for ch in left_half)
        assert any(ch != _BLANK for ch in right_half)

    @pytest.mark.unit
    def test_x_window_skips_out_of_range_samples(self):
        """Samples outside [x_min, x_max] must be silently dropped, not clipped
        to the nearest edge (which would create a misleading dot pile-up)."""
        rows = BrailleGraph.plot(
            [10.0, 20.0, 30.0],
            width=20, height=2,
            y_min=0, y_max=50,
            x_values=[100, 200, 5000],   # third sample is outside [0, 1000]
            x_min=0, x_max=1000,
        )
        # If the third sample were clipped to the right edge, the rightmost
        # cell would have a dot. Verify that's NOT the case.
        for row in rows:
            assert row[-1] == _BLANK, (
                "out-of-range sample must be dropped, not clipped to edge"
            )

    @pytest.mark.unit
    def test_legacy_mode_still_spreads_evenly(self):
        """Backward compat: callers that don't pass x_values keep the old
        behavior (spread samples evenly). --once mode relies on this."""
        rows = BrailleGraph.plot(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            width=20, height=2,
            y_min=0, y_max=10,
        )
        flat = "".join(rows)
        # Without x_values, 5 samples spread across 20 cells should
        # touch both the left and right portions of the row.
        left = "".join(row[:5] for row in rows)
        right = "".join(row[15:] for row in rows)
        assert any(ch != _BLANK for ch in left)
        assert any(ch != _BLANK for ch in right)
