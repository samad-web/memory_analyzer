# Dashboard visual identity: match SirahOS

## Purpose

The tile's web dashboard is the first thing a teammate opens when they install
the exe and sign in to SirahOS. Today it carries its own palette, its own type
scale and its own card idioms, so sitting next to a tab of SirahOS it reads as
a different product. Fixing that is a visual-only pass -- no new features, no
schema change, no API change.

## What we are matching

Source of truth: SirahOS `src/index.css` plus the component idioms in
`src/components/*`. The pieces that carry the identity:

- **Palette (HSL CSS variables).** Light root + `.dark` overrides. Primary is
  violet `250 75% 58%` (light) / `250 75% 65%` (dark). Background is
  near-white `220 20% 97%` / near-black `225 25% 6%`. All chart colors stop
  being tile-palette and start reading from these.
- **Fonts.** Inter (body), Plus Jakarta Sans (display), IBM Plex Mono
  (numbers, codes), Bebas Neue (hero numerals). Loaded from Google Fonts over
  HTTPS -- not a runtime dep.
- **Radii.** `--radius: 0.75rem`. Cards use `rounded-2xl` (1rem).
- **Signature utility classes:**
  - `.surface-elevated` → `bg-card border border-border rounded-2xl shadow-sm`
  - `.gradient-primary` → violet-to-fuchsia 135° linear gradient (hero
    accents, primary buttons, chart marks)
  - `.gradient-text` → same gradient as text fill
- **Chip/pill idiom.** Small rounded-full chips at `text-[10px]`, tinted
  `bg-primary/10 text-primary` for project tags, `bg-violet-50 text-violet-700`
  (dark: `bg-violet-900/30 text-violet-300`) for tasks, `bg-sky-50 text-sky-700`
  for time windows, `bg-amber-50 text-amber-600` for XP.
- **Hero + stats pattern.** A `PageHero` with eyebrow + title + subtitle +
  chips sits above every page; a `StatsBanner` with four icon tiles sits
  below. The memory_analyzer "Overview" today has no hero -- it jumps straight
  to tiles.
- **Theme toggle.** SirahOS has light and dark, remembered per device; the
  tile is dark-only today.

## Scope

In scope:

- A new `web/theme.css` generated from the SirahOS CSS variables, replacing
  the one `viz.py` emits today.
- A rewritten `web/style.css` built on those variables and the signature
  utilities, keeping the current layout (topbar + filters + tabs + cards)
  but re-skinning it.
- Font loader in `web/index.html`.
- A `data-theme="light"` / `data-theme="dark"` toggle in the topbar, stored
  per browser.
- Chart colors (bars, donuts, lines in `app.js`) read from CSS variables
  rather than literals.

Out of scope (deliberately -- they are their own features):

- Rebuilding the dashboard structure (sidebar nav, routes, page components).
  The tile stays single-page; it is not a port of SirahOS.
- framer-motion animations. The dashboard uses lightweight CSS transitions;
  bringing a bundler + framer-motion in violates the no-new-runtime-deps rule.
- A port of SirahOS's `PageHero`, `StatsBanner`, `AppSidebar`. We adopt the
  visual language, not the React components.

## Approach

Three files change: `web/index.html`, `web/style.css`, `web/app.js`
(colors only). One new file: `web/theme.css` is regenerated. `viz.py` is
updated so its exported theme matches, since widget code still consumes
`viz.STATUS[...]` for Qt-side status colors.

### Palette mapping

Memory-analyzer calls → SirahOS variable

| Today's role | Today's value | New source |
| --- | --- | --- |
| page background | `#0e1119` | `hsl(var(--background))` |
| card background | `#141822` | `hsl(var(--card))` |
| primary text | `#e6eaf2` | `hsl(var(--foreground))` |
| muted text | `#8a93a6` | `hsl(var(--muted-foreground))` |
| accent | `#7aa7ff` | `hsl(var(--primary))` |
| success chip | `#4caf7a` | `hsl(var(--money-in))` |
| warn chip | `#d49a44` | `hsl(var(--warning))` |

Series colors for charts (bar, donut, line) pull from a short list derived
from the primary gradient plus neutrals, so a stack of five projects reads
as "variations on a theme" instead of a crayon box.

### Fonts

Loaded with `preconnect` to `fonts.gstatic.com` and one `<link>` to
`fonts.googleapis.com`. The whole set is ~90KB over the wire with
`display=swap`. Offline fallback: `system-ui, -apple-system, sans-serif`,
matching SirahOS.

## Impact map

- `web/index.html` — adds the font links, the theme toggle, a `PageHero`-shaped
  header block.
- `web/style.css` — rewritten in place; same selector surface so `app.js`
  does not need rewriting.
- `web/app.js` — chart colors read from `getComputedStyle(document.documentElement)`.
  Nothing structural.
- `viz.py` — regenerate `theme.css` to emit the SirahOS variables; the Qt
  tile keeps consuming `viz.STATUS` the same way, which stays mapped to the
  new palette.
- `tests/` — a visual regression is impractical here, so the regression we
  run is "the page still passes its structural assertions and `app.js`
  reads a known set of CSS vars without undefined values". One new test file:
  `tests/test_theme.py`.

Does NOT touch:

- Any Python outside `viz.py`. The dashboard web server serves whatever is
  in `web/`.
- The Qt tile's visual language. The tile is a widget on top of the OS; the
  palette already came from `viz.STATUS`, so a `viz.py` update propagates
  there automatically.
- Chart math. Only chart colors.

## Edge cases

- **System in light mode but user prefers dark on the dashboard.** The
  toggle wins; it is explicit.
- **High-contrast mode** on Windows. SirahOS does not special-case this; we
  honor the OS `prefers-contrast: more` by nudging border alpha and ring width.
- **Fonts fail to load** (no network). The system fallback is close enough
  in metrics that layout does not jump by more than a few pixels.
- **A chart with 12+ series.** We cycle the palette and dim the overflow.

## Acceptance criteria

1. Opening the dashboard next to a tab of SirahOS reads as the same product
   family: same primary violet, same radii, same card feel.
2. Theme toggle switches light/dark instantly, no reload, and the choice
   survives a browser refresh.
3. Every stat tile and every chart on Overview still renders correctly;
   `tests/test_theme.py` passes.
4. Opening the dashboard with JavaScript disabled still shows the brand
   colors (variables are CSS, not JS-set).
5. `viz.py` still emits a valid `theme.css` and the Qt tile still shows the
   right status colors.
