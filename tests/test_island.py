"""Dynamic Island layout and identity maths (island.py), and the media
now-playing change signature (media.NowPlaying).

These are the Qt-free parts the widget relies on to place rings, pick a
project's glyph/colour, hit-test the dropdown, and decide when now-playing has
actually changed. Named to map onto the acceptance criteria in
build-docs/features/dynamic-island.md.
"""

from __future__ import annotations

import unittest

import island
from media import NowPlaying


class GlyphAndHueTest(unittest.TestCase):
    # Criterion 2
    def test_glyph_two_words_is_initials(self):
        self.assertEqual(island.glyph("WealthOS-Mobile"), "WM")
        self.assertEqual(island.glyph("memory_analyzer"), "MA")

    def test_glyph_single_word_is_first_two(self):
        self.assertEqual(island.glyph("bizflow"), "BI")

    def test_glyph_empty_is_placeholder(self):
        self.assertEqual(island.glyph(""), "?")
        self.assertEqual(island.glyph("   "), "?")

    # Criterion 3
    def test_hue_in_range_and_deterministic(self):
        self.assertTrue(0 <= island.hue("WealthOS") < 360)
        self.assertEqual(island.hue("WealthOS"), island.hue("WealthOS"))

    def test_hue_differs_for_unlike_keys(self):
        self.assertNotEqual(island.hue("alpha"), island.hue("something-else"))


class RingLayoutTest(unittest.TestCase):
    # Criterion 4
    def test_ring_count_caps_at_max(self):
        self.assertEqual(island.ring_rects(0), [])
        self.assertEqual(len(island.ring_rects(3)), 3)
        self.assertEqual(len(island.ring_rects(99)), island.MAX_RINGS)

    def test_pill_width_non_decreasing(self):
        widths = [island.pill_width(n) for n in range(0, 8)]
        for a, b in zip(widths, widths[1:]):
            self.assertLessEqual(a, b)

    def test_overflow_and_badge(self):
        self.assertIsNone(island.badge_pos(island.MAX_RINGS))
        self.assertEqual(island.overflow(island.MAX_RINGS + 4), 4)
        self.assertIsNotNone(island.badge_pos(island.MAX_RINGS + 1))

    def test_ring_at_resolves_the_right_ring(self):
        rects = island.ring_rects(3)
        for i, (x, y, d) in enumerate(rects):
            self.assertEqual(island.ring_at(3, x + d / 2, y + d / 2), i)
        # A gap between rings is a miss.
        self.assertEqual(island.ring_at(3, -5, -5), -1)


class RingsStayCentredTest(unittest.TestCase):
    """While the card widens from pill to panel, the rings must not slide."""

    def test_ring_screen_x_constant_through_the_drop(self):
        from types import SimpleNamespace

        import widget

        fake = SimpleNamespace(_alerts=[object()] * 3)
        pill = island.pill_width(3)
        win = island.panel_width(3) + 40   # window is fixed through the drop
        xs = set()
        for cardw in (pill, pill + 37.5, island.panel_width(3)):
            card_left = (win - cardw) / 2.0
            xs.add(card_left + widget.MonitorWidget._island_pill_offset(fake, cardw))
        self.assertEqual(len(xs), 1)


class DropdownHitTest(unittest.TestCase):
    # Criterion 5
    def test_row_at_maps_y_to_index(self):
        top = island.rows_top()
        self.assertEqual(island.row_at(top + 1, 3), 0)
        self.assertEqual(island.row_at(top + island.ROW_H + 1, 3), 1)
        self.assertEqual(island.row_at(top + 2 * island.ROW_H + 1, 3), 2)

    def test_row_at_misses_outside_block(self):
        top = island.rows_top()
        self.assertEqual(island.row_at(0, 3), -1)          # above the rows
        self.assertEqual(island.row_at(top - 1, 3), -1)    # still in the pill
        self.assertEqual(island.row_at(top + 99 * island.ROW_H, 3), -1)  # below
        self.assertEqual(island.row_at(top + 1, 0), -1)    # no rows at all


class LogoPathTest(unittest.TestCase):
    def test_finds_a_logo_in_a_conventional_place(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            assets = os.path.join(root, "assets")
            os.makedirs(assets)
            logo = os.path.join(assets, "logo.png")
            open(logo, "wb").close()
            self.assertEqual(island.logo_path(root), logo)

    def test_returns_none_when_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(island.logo_path(root))
        self.assertIsNone(island.logo_path(""))

    def test_shallow_walk_finds_a_nested_icon(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, "app", "branding")
            os.makedirs(nested)
            logo = os.path.join(nested, "app-icon.png")
            open(logo, "wb").close()
            self.assertEqual(island.logo_path(root), logo)

    def test_walk_skips_dependency_dirs(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            buried = os.path.join(root, "node_modules", "pkg")
            os.makedirs(buried)
            open(os.path.join(buried, "logo.png"), "wb").close()
            self.assertIsNone(island.logo_path(root))


class NowPlayingTest(unittest.TestCase):
    def test_ident_excludes_art(self):
        """Art is refetched only when the track changes, so it must not be part
        of the change signature -- else every poll with identical metadata but a
        re-read thumbnail would look like a change."""
        a = NowPlaying(title="T", artist="A", playing=True, art=b"one")
        b = NowPlaying(title="T", artist="A", playing=True, art=b"two")
        self.assertEqual(a.ident, b.ident)

    def test_ident_changes_on_pause(self):
        a = NowPlaying(title="T", artist="A", playing=True)
        b = NowPlaying(title="T", artist="A", playing=False)
        self.assertNotEqual(a.ident, b.ident)


if __name__ == "__main__":
    unittest.main()
