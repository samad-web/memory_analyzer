# Dynamic Island view

## Purpose

A compact, macOS-Dynamic-Island-style view of live Claude Code sessions: a
small pill of status rings that expands into a clickable dropdown when a
session needs you or finishes. Clicking a session raises its terminal so you
can answer it. It is a second display mode of the existing tile, not a new
window and not a replacement for the system monitor.

## What the ring can and cannot show

A session alert carries only `kind` (working / needs_input / done), `source`,
`at`, `cwd`, `hwnd`. There is **no percent-complete** from Claude Code, so the
ring is state-driven, not a progress gauge:

- `working`  -> a rotating arc (blue), the universal "busy" affordance.
- `needs_input` -> a full ring (orange), breathing, same pulse as today's board.
- `done`     -> a full ring (green).

The "project icon in the middle" is a generated glyph: 1-2 uppercase initials
derived from the project name, on a colour whose hue is hashed from the
project key. No project-icon source exists in the repo, so this is the
substitute. `shortcut: initials glyph stands in for a real icon; upgrade when
a project-icon source (e.g. SirahOS identity) exists.`

## Architecture

New pure module `island.py` holds all layout/identity maths, Qt-free so it is
testable without a display (the repo's tests never construct the widget):

- `glyph(source) -> str`        1-2 uppercase initials.
- `hue(key) -> int`             deterministic 0-359.
- `ring_rects(n) -> [(x,y,d)]`  ring positions in the collapsed pill (capped).
- `pill_size(n) -> (w, h)`      collapsed pill logical size.
- `panel_size(n, rows) -> (w,h)` expanded size (pill + dropdown rows).
- `row_at(y, n_rows) -> int`    dropdown row under a y, or -1.

`widget.py` gains an `island` mode (off by default) that reuses everything it
already has:

- Data: `self._alerts` / `self._rows`, maintained by the existing
  `_poll_alert` / `_adopt`. No new polling.
- Paint: `paintEvent` branches to `_paint_island(p)` and returns, so the
  monitor rows are skipped entirely in island mode.
- Click: a ring (collapsed) or a dropdown row (expanded) resolves to an alert
  via `island.row_at` / `ring_rects`, then calls the existing `focus_agent`.
- Expand rule: `expanded = cursor_over_pill OR any(a.attention)`. A new
  needs_input/done therefore auto-opens the dropdown; it collapses again once
  dismissed and the mouse leaves. Computed in the existing 150 ms hover poll
  (`_update_hover_region`) and after `_adopt`.
- Animation: the spinner reuses the existing `_anim_timer` / clock-sampled
  phase; `_sync_anim` also wants the timer when island mode has a working
  session. One new `_spin_angle()` off `time.monotonic()`, like `_pulse_level`.
- Size: `_apply_size` / `_resize_for_alerts` branch to the island sizes when
  the mode is on. The resize corner is hidden in island mode (the pill width is
  derived from session count, not dragged); the green grip and drag still work.
- Toggle: a checkable "Dynamic Island" menu item + `cfg["island"]` + an
  `apply_setting` entry (so the dashboard/web can flip it too).

## Media now-playing (audio/video control)

The island also shows the system's current media session -- title, artist,
album art -- with transport controls, like the real Dynamic Island. This
reverses the no-new-dependency constraint by explicit user decision (see
MEMORY.md, 2026-11-14): reading now-playing needs WinRT `Windows.Media.Control`.

New module `media.py` isolates every WinRT call:

- `MediaWatcher(QThread)` runs an asyncio loop, polls the current session on a
  ~1 s timer, and emits `changed(NowPlaying)` only when something differs.
  `NowPlaying` is a plain dataclass: `title, artist, status (playing/paused),
  art (PNG bytes | None), can_next, can_prev, can_play_pause`.
- Controls (`toggle_play_pause`, `next`, `previous`) schedule the matching
  `try_*_async` coroutine onto the watcher's loop with
  `asyncio.run_coroutine_threadsafe`, so the UI thread never blocks.
- Album art comes from `media_properties.thumbnail` -> `open_read_async` ->
  `DataReader`, decoded to bytes and handed to `QPixmap` on the UI thread.
- **Graceful absence**: if `import winrt...` fails (dev checkout without the
  packages, or an old frozen build), `MediaWatcher.available` is False, the
  thread never starts, and the island simply omits the media strip. The tile
  never crashes for want of WinRT.

In the island, the media strip sits under the session rings (collapsed: art +
a compact title; expanded: art + title/artist + the three buttons). Buttons
hit-test like the session rings and call the `media` controls. The strip is
hidden when there is no current session.

Acceptance (media):

- M1. With nothing playing, no media strip is shown and no error is raised.
- M2. With a player active, the strip shows its title/artist and art; the
  play/pause glyph matches the real status.
- M3. Clicking next/prev/play-pause changes the real player within ~1 s and the
  glyph updates.
- M4. With the `winrt-*` packages uninstalled, the tile still starts and the
  island shows sessions with no media strip (`MediaWatcher.available` False).

M1-M3 are verified by running against a real player; M4 by importing `media`
with the dependency masked (unit test).

## Refinements (2026-11-14, after first build)

Driven by follow-up requests and the reference repo
`github.com/HeyyCzer/windows-dynamic-island`:

- **Drops from the top edge.** The island hangs flush against the top of the
  screen (square top corners, rounded bottom -- drawn by pushing the card's top
  above the window) and the window itself grows downward as it opens. The open
  is a `QVariantAnimation`, 240 ms, OutCubic in / InCubic out; size and opacity
  interpolate on `_island_anim_t`. Black (`#000`) background, like the
  reference. It is top-centred per screen and not dragged.
- **Present on both screens.** `IslandClone` is a stateless mirror window on
  every screen other than the tile's own: it paints the tile's island and
  forwards clicks back to it, a 50 ms timer keeps it in step, and hover/expand
  is global so hovering either screen opens both. Built/torn down with island
  mode. `shortcut: clones are fixed at island-enable time; a monitor plugged in
  mid-session is picked up on the next toggle.`
- **Project logo in the ring.** `island.logo_path` looks for a real icon
  (logo/icon/favicon .png/.jpg/.ico) in the project directory and a few
  conventional subfolders; the ring draws it clipped to the disc, falling back
  to the initials glyph when none is found. Cached per project. (.svg excluded:
  the frozen build drops QtSvg.)
- **Memory analysis in the island.** The open panel carries a CPU / RAM / GPU
  readout and the free-RAM button (same `self.sample` and `free_memory` as the
  monitor), so the island is the agent board *and* the system monitor.

## Controls (2026-11-14)

- **Lock / free (`island_locked`, default on).** Locked: pinned top-centre of
  every screen, mirrored on both, not draggable. Unlocked: one draggable
  island, no mirror; the dropped position (`island_cx`, `island_y`) persists
  and it still grows downward from there. Toggle in the menu ("Lock island to
  top") or over the API.
- **Quit the box.** A small red x (top-right, shown when open) closes the tile.
- **Resize.** A grip in the bottom-right (shown when open) scales the island
  continuously; the Size menu still applies its presets. Scale is shared with
  the monitor.

## Smoothness and shape (2026-11-14)

- **The window never resizes mid-drop.** Resizing a translucent always-on-top
  window every frame is what tore ("breaking"). The window is fixed at the
  expanded extent; the drop is painted inside it -- an eased card
  (`_island_card_size`) grows downward, centred (`_island_card_left`), with
  content clipped to the card so a half-dropped row is a clean reveal. The
  per-frame tick is now a repaint, not a resize. The fixed window is mostly
  transparent, so hover and click-through test the card rectangle
  (`_island_card_rect_global`), not the window, and clicks pass through the
  empty margin.
- **Hangs like a water drop.** `_island_card_path` draws a pinched neck at the
  top edge that flares out through concave curves to the full-width body, with
  convex rounded bottoms -- the island clinging to the top of the screen. The
  neck sits inside the pill's top padding so rings never poke out of it.
- **Logos look right.** `SmoothPixmapTransform` is on for scaled icons, and
  `island.logo_path` also searches nested asset dirs (`assets/images`,
  `src/assets`, `public/images`, `src`, `app`) -- which is where Expo/React and
  Vite/Next projects keep their icon, so e.g. WealthOS-Mobile shows
  `assets/icon.png` and bizflow-suite `public/logo.png`.

## Impact map

- `island.py` (new): pure, no imports beyond stdlib. Self-check at bottom.
- `media.py` (new): all WinRT isolated here; a `QThread` watcher + controls,
  degrades to unavailable if the `winrt-*` import fails.
- `requirements.txt`: `winrt-*` packages added (optional at runtime).
- `widget.py`: new paint branch, hit-test branch, size branch, one menu item,
  one `apply_setting` row, `_sync_anim` clause, `cfg["island"]` default.
  Risk: the size branch is the sharp edge -- `_place()`, the resize corner and
  the alert-row hit-test (`_alert_at`) all assume the monitor `WIDTH`/height.
  Island mode must not reach those paths. Mitigated by early `return` in the
  branching methods.
- `DEFAULTS` dict: `+"island": False`. `load_config` already filters to known
  keys, so old config.json files simply default it off -- no migration.
- No change to hooks, store, webserver routes, sync, or the dashboard window.
  `apply_setting` gains a key, which the web API exposes for free.
- Tests: `tests/test_island.py` (new) covers the pure module.

## Edge cases and failure modes

- **0 sessions**: collapsed pill shows a single dim dot; expanded shows
  "no agents". No rings, no crash on empty `ring_rects(0)`.
- **More sessions than fit**: `ring_rects` caps at 5 rings and the pill shows
  a "+N" badge; the dropdown reuses the existing `MAX_ALERT_ROWS` + "+N more".
- **Two sessions, one project**: two rings share a glyph (correct -- same
  project); the dropdown tells them apart by `session_tag`, as today.
- **Click-through on**: the pill and the dropdown rows are added to the
  clickable hover region, so clicks land; everything else stays pass-through.
- **Scale**: unchanged -- the painter scales logical pixels, island sizes are
  logical too.
- **No terminal window for a session** (`hwnd` 0/stale): `focus_agent` already
  falls back to title matching and flashes "no window for X". Reused as-is.
- **Window never shrinks past the taskbar / off-screen**: island sizes are
  small, and `_place` already pulls the window back inside a screen.

## Acceptance criteria

1. `cfg["island"]` is absent by default and the tile opens in monitor mode;
   an old config.json without the key still opens in monitor mode.
2. `island.glyph("WealthOS-Mobile") == "WM"`; `glyph("memory_analyzer")` is 1-2
   uppercase chars; deterministic.
3. `island.hue(k)` is in `0..359`, deterministic, and differs for two unlike
   keys.
4. `pill_size(n).w` is non-decreasing in `n`; `len(ring_rects(n)) == min(n, 5)`;
   `ring_rects(0) == []`.
5. `row_at` maps a y inside row `i` to `i`, and a y above the first row to -1.
6. With island mode on: a `working` alert runs the spin animation
   (`_anim_timer` active); `needs_input` draws orange, `done` green.
7. A new `needs_input`/`done` alert auto-expands the dropdown; clicking a
   session row calls `focus_agent` for that exact session.
8. Toggling the menu item switches modes live and resizes the window; the
   monitor view is byte-identical to before when island mode is off.

Criteria 2-5 are unit-tested in `tests/test_island.py`. 1,6,7,8 are verified by
running the tile (GUI).
