"""Layout and identity maths for the Dynamic Island view.

Kept free of Qt so it is testable without a display -- the widget does the
painting, this decides where things go and what a project's glyph is. All
sizes are logical pixels; the widget's painter applies the tile scale on top.
"""

from __future__ import annotations

import hashlib
import os
import re

# -- ring / pill geometry ---------------------------------------------------

RING_D = 34            # a session ring's diameter in the collapsed pill
RING_GAP = 8           # between rings
PILL_PAD = 11          # inside the pill, around the rings
MAX_RINGS = 5          # rings shown before the rest collapse into "+N"
BADGE_W = 20           # width reserved for the "+N" overflow badge
PILL_H = PILL_PAD * 2 + RING_D          # collapsed pill height
IDLE_W = PILL_H        # collapsed pill is a small circle when nothing is running

# -- expanded dropdown ------------------------------------------------------

PANEL_MIN_W = 264      # the dropdown wants room for a title + note
ROW_H = 30             # one session row in the dropdown
ROWS_TOP_PAD = 6       # between the pill and the first dropdown row
ROWS_BOT_PAD = 8
SMALL_RING_D = 20      # a ring inside a dropdown row

_WORD = re.compile(r"[A-Za-z0-9]+")


def shown(n: int) -> int:
    """How many rings are actually drawn (the rest become a +N badge)."""
    return max(0, min(int(n), MAX_RINGS))


def overflow(n: int) -> int:
    """How many sessions are hidden behind the +N badge."""
    return max(0, int(n) - MAX_RINGS)


def pill_width(n: int) -> int:
    k = shown(n)
    if k == 0:
        return IDLE_W
    w = PILL_PAD * 2 + k * RING_D + (k - 1) * RING_GAP
    if overflow(n):
        w += RING_GAP + BADGE_W
    return w


def pill_size(n: int) -> tuple[int, int]:
    return pill_width(n), PILL_H


def ring_rects(n: int) -> list[tuple[int, int, int]]:
    """(x, y, diameter) for each drawn ring, left to right."""
    rects = []
    x = PILL_PAD
    for _ in range(shown(n)):
        rects.append((x, PILL_PAD, RING_D))
        x += RING_D + RING_GAP
    return rects


def badge_pos(n: int) -> tuple[int, int, int] | None:
    """(x, y, width) of the +N badge, or None when nothing overflows."""
    if not overflow(n):
        return None
    x = PILL_PAD + shown(n) * (RING_D + RING_GAP)
    return x, PILL_PAD, BADGE_W


def ring_at(n: int, x: float, y: float) -> int:
    """Index of the ring under a collapsed-pill point, or -1."""
    for i, (rx, ry, d) in enumerate(ring_rects(n)):
        if rx <= x <= rx + d and ry <= y <= ry + d:
            return i
    return -1


# -- expanded dropdown ------------------------------------------------------

def rows_height(n_rows: int) -> int:
    """Height of the session-row block under the pill (0 when empty)."""
    if n_rows <= 0:
        return 0
    return ROWS_TOP_PAD + n_rows * ROW_H + ROWS_BOT_PAD


def rows_top() -> int:
    return PILL_H + ROWS_TOP_PAD


def row_at(y: float, n_rows: int) -> int:
    """Index of the dropdown row under a y, or -1 above/below the block."""
    top = rows_top()
    if y < top:
        return -1
    i = int((y - top) // ROW_H)
    return i if 0 <= i < n_rows else -1


def panel_width(n: int) -> int:
    return max(PANEL_MIN_W, pill_width(n))


# -- project identity -------------------------------------------------------

def glyph(source: str) -> str:
    """1-2 uppercase initials standing in for a project icon.

    Two words -> first letter of each ("WealthOS-Mobile" -> "WM"); one word ->
    its first two letters ("bizflow" -> "BI"). Empty -> "?".
    """
    words = _WORD.findall(source or "")
    if not words:
        return "?"
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return words[0][:2].upper()


# A project's own icon, looked for in its directory. Raster + .ico only: the
# PyInstaller build excludes QtSvg, so an .svg would not render in the frozen
# tile. Ordered by how likely each is to be the real app icon.
_LOGO_NAMES = (
    "logo.png", "icon.png", "app-icon.png", "appicon.png", "logo.jpg",
    "icon.jpg", "favicon.png", "favicon.ico", "icon.ico", "logo.ico",
)
_LOGO_DIRS = ("", "assets", "public", "static", "resources", "images", "img",
              "build-docs", "docs")


def logo_path(project_root: str) -> str | None:
    """A project's own logo/icon file, if one sits in a usual place, else None.

    Searched shallowly (a handful of names in a handful of conventional
    folders) -- the ring falls back to the initials glyph when nothing is
    found, so a miss is cheap and never wrong.
    """
    if not project_root:
        return None
    for folder in _LOGO_DIRS:
        for name in _LOGO_NAMES:
            candidate = os.path.join(project_root, folder, name)
            try:
                if os.path.isfile(candidate):
                    return candidate
            except OSError:
                continue
    return None


def hue(key: str) -> int:
    """A stable 0..359 hue for a project, from a hash of its key."""
    digest = hashlib.md5((key or "").encode("utf-8")).hexdigest()
    return int(digest[:6], 16) % 360


def _selfcheck() -> None:
    assert glyph("WealthOS-Mobile") == "WM", glyph("WealthOS-Mobile")
    assert glyph("memory_analyzer") == "MA", glyph("memory_analyzer")
    assert glyph("bizflow") == "BI", glyph("bizflow")
    assert glyph("") == "?"
    assert 0 <= hue("a") < 360 and hue("a") == hue("a")
    assert hue("a") != hue("bizflow-xyz")  # not a proof, just a smoke test
    assert ring_rects(0) == []
    assert len(ring_rects(3)) == 3
    assert len(ring_rects(99)) == MAX_RINGS
    assert pill_width(3) < pill_width(5) <= pill_width(99)
    assert overflow(99) == 99 - MAX_RINGS and badge_pos(2) is None
    assert row_at(rows_top() + 1, 3) == 0
    assert row_at(rows_top() + ROW_H + 1, 3) == 1
    assert row_at(0, 3) == -1 and row_at(rows_top() + 99 * ROW_H, 3) == -1
    assert ring_at(3, *ring_rects(3)[1][:2]) == 1
    print("island self-check ok")


if __name__ == "__main__":
    _selfcheck()
