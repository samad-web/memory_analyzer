"""The dashboard's visual tokens come from viz.theme_css.

The redesign aligns the dashboard with SirahOS, so the stylesheet has to
emit the SirahOS token names (--background, --primary, --card, ...) in both
light and dark variants, AND keep the legacy chart tokens that app.js
reads for its SVG colours.

If one of those tokens goes missing, the dashboard renders as unstyled
white-on-white or crashes a chart that reads an empty string into a stroke
attribute. So this test asserts the exact set.
"""

from __future__ import annotations

import re
import unittest

from ._env import Sandbox


SIRAH_LIGHT_TOKENS = (
    "background", "foreground", "card", "card-foreground",
    "primary", "primary-foreground", "muted", "muted-foreground",
    "accent", "accent-foreground", "destructive", "border",
    "input", "ring", "money-in", "money-out", "warning",
    "gradient-start", "gradient-end", "radius",
)

LEGACY_TOKENS = (
    "surface", "surface-card", "ink", "ink-dim", "ink-muted", "grid",
)

CHART_TOKENS = (
    "series-1", "series-2", "series-8",
    "status-done", "status-needs-input", "status-working", "status-offline",
    "health-cpu", "health-ram", "health-gpu", "health-fan",
    "seq-0", "seq-7",
)


class ThemeCSSTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.viz = self.sb.mod["events"]  # noqa -- real import below
        self.css = self.sb.mod["context"].__dict__  # placeholder to force reload
        from importlib import import_module
        self.viz = import_module("viz")

    def _declarations(self, css: str) -> set[str]:
        """Every ``--name`` ever declared in the stylesheet, from either block."""
        return set(re.findall(r"--([a-z0-9-]+)\s*:", css))

    def test_light_root_defines_sirahos_tokens(self):
        css = self.viz.theme_css()
        # The ``:root { ... }`` block is the light default; grab it alone so a
        # token that lives only in `.dark` does not satisfy the check.
        match = re.search(r":root\s*{([^}]*)}", css)
        self.assertIsNotNone(match, "no :root block in theme.css")
        root = match.group(1)
        declared = self._declarations(root)
        for token in SIRAH_LIGHT_TOKENS:
            self.assertIn(token, declared, f"missing --{token} in :root")

    def test_dark_override_block_exists(self):
        css = self.viz.theme_css()
        self.assertIn('[data-theme="dark"]', css)
        # And a system-preference fallback for a user who has not toggled.
        self.assertIn("prefers-color-scheme: dark", css)

    def test_legacy_tokens_remain_as_aliases(self):
        """app.js still reads --ink / --grid / --surface -- do not drop them."""
        css = self.viz.theme_css()
        declared = self._declarations(css)
        for token in LEGACY_TOKENS:
            self.assertIn(token, declared, f"missing legacy alias --{token}")

    def test_chart_tokens_present(self):
        """Chart colours are fixed across themes for colour-blind validation."""
        css = self.viz.theme_css()
        declared = self._declarations(css)
        for token in CHART_TOKENS:
            self.assertIn(token, declared, f"missing chart token --{token}")

    def test_tokens_use_hsl_triples(self):
        """SirahOS CSS vars store ``H S% L%`` so ``hsl(var(--name))`` works."""
        css = self.viz.theme_css()
        # Pick a representative token; a stray hex here would mean a call site
        # using `hsl(var(--background))` renders as nothing at all.
        match = re.search(r"--background:\s*([^;]+);", css)
        self.assertIsNotNone(match)
        value = match.group(1).strip()
        self.assertRegex(value, r"^\d+\s+\d+%\s+\d+%$",
                         f"--background should be HSL triple, got {value!r}")


if __name__ == "__main__":
    unittest.main()
