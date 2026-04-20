"""ASCII / Braille line-graph renderer for the TUI.

The Braille pattern block (U+2800-U+28FF) packs 2 columns × 4 rows of
binary dots into one character, so a single terminal cell encodes 8
on/off pixels. We use that to draw line graphs in tiny windows.

The character at code point 0x2800 + N has its dots set per the bitmask:

    0 3
    1 4
    2 5
    6 7

For each terminal cell we therefore have a 2-wide × 4-tall sub-grid
indexed by (col, row) where col ∈ {0,1} and row ∈ {0,1,2,3}.

If the terminal can't render Braille (LANG=C, very old fonts), the
fallback uses block characters from the standard ASCII set.
"""

import locale
import os
from typing import List, Optional, Sequence


# Bit positions for each (col, row) inside the 2x4 cell.
# Mirrors the Unicode Braille Pattern dot ordering above.
_DOT_BITS = {
    (0, 0): 0x01, (1, 0): 0x08,
    (0, 1): 0x02, (1, 1): 0x10,
    (0, 2): 0x04, (1, 2): 0x20,
    (0, 3): 0x40, (1, 3): 0x80,
}

_BRAILLE_BASE = 0x2800

# Used when the terminal is not Braille-capable. One char per cell, the
# row chosen by the topmost set dot.
_BLOCK_FALLBACK = (" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")


class BrailleGraph:
    """Render a series of numeric values as a 2x4-dot Braille line graph.

    Stateless. ``plot`` returns one string per terminal row and is the
    primary entry point. ``render_to_window`` is a small helper for
    curses callers that don't want to manage layout themselves.
    """

    @staticmethod
    def supported() -> bool:
        """True when the current locale + LANG suggest Braille will render.

        Braille pattern characters require a UTF-8 capable encoding and
        a font that includes them. We approximate "supported" as
        "LANG != C and the locale's preferred encoding is UTF-8".
        """
        if os.environ.get("LANG", "").upper() == "C":
            return False
        try:
            enc = locale.getpreferredencoding(False) or ""
        except Exception:
            enc = ""
        return "UTF-8" in enc.upper() or "UTF8" in enc.upper()

    @staticmethod
    def code_point(dots: int) -> str:
        """Return the Unicode character for a Braille dot bitmask 0..255."""
        if dots < 0 or dots > 0xFF:
            raise ValueError(f"dots must be 0..255, got {dots}")
        return chr(_BRAILLE_BASE + dots)

    @classmethod
    def cell(cls, on: Sequence[Sequence[bool]]) -> str:
        """Build a single Braille char from a 2-col × 4-row truth table.

        ``on`` is indexed as ``on[col][row]`` with col ∈ {0,1}, row ∈ {0,1,2,3}.
        """
        dots = 0
        for col in (0, 1):
            for row in range(4):
                if on[col][row]:
                    dots |= _DOT_BITS[(col, row)]
        return cls.code_point(dots)

    @classmethod
    def plot(
        cls,
        data: Sequence[float],
        *,
        width: int,
        height: int,
        y_min: Optional[float] = None,
        y_max: Optional[float] = None,
        force_fallback: bool = False,
    ) -> List[str]:
        """Return a ``height``-row × ``width``-cell rendering of ``data``.

        Args:
            data: numeric series to plot.
            width: number of terminal cells (each Braille cell holds 2 columns).
            height: number of terminal rows (each Braille cell holds 4 rows).
            y_min, y_max: explicit Y bounds; auto-scaled from ``data`` when omitted.
            force_fallback: skip Braille and use block characters even when
                ``supported()`` would return True.

        The output is empty (a list of empty strings) when ``data`` is empty
        or ``width``/``height`` <= 0.
        """
        if width <= 0 or height <= 0:
            return [""] * max(0, height)
        if not data:
            return [" " * width for _ in range(height)]

        if y_min is None:
            y_min = min(data)
        if y_max is None:
            y_max = max(data)
        # Avoid a zero range: pad symmetrically.
        if y_max <= y_min:
            pad = abs(y_min) * 0.05 if y_min else 1.0
            y_min -= pad
            y_max += pad
        y_range = y_max - y_min

        # Total horizontal/vertical resolution in dots.
        grid_w = width * 2
        grid_h = height * 4
        # Allocate the dot grid as bool[col][row] indexed by absolute pixel.
        grid = [[False] * grid_h for _ in range(grid_w)]

        # Project each input sample to a horizontal pixel column. We
        # *don't* interpolate -- the goal is fast, accurate plotting of
        # the latest N samples.
        n = len(data)
        for i, value in enumerate(data):
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            # Horizontal: spread samples across the full grid width.
            if n == 1:
                gx = grid_w - 1
            else:
                gx = int(round(i * (grid_w - 1) / (n - 1)))
            gx = max(0, min(grid_w - 1, gx))
            # Vertical: invert so high values appear at the top.
            norm = (v - y_min) / y_range
            norm = max(0.0, min(1.0, norm))
            gy = int(round((1 - norm) * (grid_h - 1)))
            gy = max(0, min(grid_h - 1, gy))
            grid[gx][gy] = True

        use_fallback = force_fallback or not cls.supported()

        rows: List[str] = []
        for cell_row in range(height):
            cells: List[str] = []
            for cell_col in range(width):
                # Slice the 2x4 region for this terminal cell.
                if use_fallback:
                    cells.append(cls._fallback_char(grid, cell_col, cell_row))
                else:
                    sub = [
                        [grid[cell_col * 2 + 0][cell_row * 4 + r] for r in range(4)],
                        [grid[cell_col * 2 + 1][cell_row * 4 + r] for r in range(4)],
                    ]
                    cells.append(cls.cell(sub))
            rows.append("".join(cells))
        return rows

    @staticmethod
    def _fallback_char(grid, cell_col: int, cell_row: int) -> str:
        """Pick a single block char for the 2x4 cell sub-grid."""
        # Count the highest "on" pixel within the cell across both columns.
        highest_on = -1
        for col_off in (0, 1):
            for r in range(4):
                if grid[cell_col * 2 + col_off][cell_row * 4 + r]:
                    if r > highest_on:
                        highest_on = r
        if highest_on < 0:
            return _BLOCK_FALLBACK[0]
        # row 0 (top dot) -> tall block; row 3 (bottom) -> tiny block.
        return _BLOCK_FALLBACK[8 - 2 * highest_on if highest_on > 0 else 8]

    @classmethod
    def render_to_window(
        cls,
        win,
        y: int,
        x: int,
        height: int,
        width: int,
        data: Sequence[float],
        *,
        title: str = "",
        y_axis_label: str = "",
        attr=0,
    ) -> None:
        """Convenience curses helper. Best-effort -- swallows curses errors.

        ``win`` must implement ``addnstr(y, x, text, n[, attr])``. The
        renderer also prints the title on the row above the graph and
        the y-axis label on the row below; both are optional.
        """
        if height <= 0 or width <= 0:
            return
        rows = cls.plot(data, width=width, height=height)
        for i, row in enumerate(rows):
            try:
                win.addnstr(y + i, x, row, width, attr)
            except Exception:
                pass
        if title:
            try:
                win.addnstr(y - 1, x, title, width, attr)
            except Exception:
                pass
        if y_axis_label:
            try:
                win.addnstr(y + height, x, y_axis_label, width, attr)
            except Exception:
                pass
