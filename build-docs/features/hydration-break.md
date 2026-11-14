# Water-break reminder

## Purpose

Make the team actually drink water: every ~75 minutes, block every screen for
15 seconds so they step away, then make them affirm they drank before the
screen is released.

## Behaviour

- A `QTimer` on the tile fires every `hydration_minutes` (default 75).
- `HydrationBlocker` opens a frameless, always-on-top window sized to the whole
  virtual desktop (covers both monitors), grabs the keyboard, and swallows all
  keys -- **no escape** during the countdown (user decision, 2026-11-14).
- Phase 1 "block": a liquid-glass water wash (deep-blue gradient, drawn
  droplet) with "Time to drink water" and a `Step away for Ns…` countdown.
- Phase 2 "ask" (after the countdown): "Did you drink water?" with two buttons.
  - **No** is a `DodgeButton`: it hops to a random spot on `enterEvent` and on
    `mousePressEvent`, so it can never be clicked.
  - **Yes** clicked within 2s (too fast to have actually drunk) → phase 3
    "confirm" ("Are you sure?"); a second Yes releases the screen. A Yes after
    2s releases immediately.
- Releasing = `releaseKeyboard()` + close; the tile clears its handle.

On by default (`hydration_enabled`). Toggle: tray/menu "Water reminder", plus
"Take a water break now" to preview. Also exposed via `apply_setting`
(dashboard/HTTP).

## Why an overlay, not input lockdown

`BlockInput`/low-level hooks would freeze the machine and need elevation; if the
process then hung, the user would be locked out (only Ctrl-Alt-Del frees it).
A top-most full-desktop window that eats clicks and grabs the keyboard is
enough to stop normal interaction, and it can never strand the user: closing
the tile, or the window, ends it. OS shortcuts (Win, Ctrl-Alt-Del) still work
by design.

## Impact map

- `widget.py`: `HydrationBlocker` + `DodgeButton` classes; a timer, three
  `_hydration_*` methods, two menu items, one `apply_setting` key, config
  defaults, and teardown in `closeEvent`. No other module touched.
- `DEFAULTS`: `hydration_enabled` (True), `hydration_minutes` (75),
  `hydration_block_seconds` (15). `load_config` filters unknown keys, so older
  config.json files just take the defaults.
- Ships inside the existing `widget.py` -- no new bundle file, no new
  dependency.

## Edge cases

- **Already blocking**: `_hydration_due` is a no-op while a blocker is live, so
  overlapping timers cannot stack windows.
- **Quit mid-block**: the tile's `closeEvent` closes the blocker (which
  releases the keyboard), so quitting is always a way out.
- **Monitor unplugged**: the window is sized once from the virtual geometry at
  open; a layout change mid-break is cosmetic for 15s. `shortcut: fixed
  geometry for the break's duration, revisit if breaks get long.`
- **Second screen**: one window spans the virtual desktop, so both screens are
  covered and darkened.

## Acceptance criteria

1. With `hydration_enabled` and a 15s block, a timer tick opens a full-desktop
   overlay that ignores keys and clicks for the countdown.
2. After the countdown the question appears; the No button moves away from the
   pointer and cannot be clicked.
3. A Yes within 2s shows "Are you sure?"; a second Yes (or a Yes after 2s)
   closes the overlay and releases input.
4. Quitting the tile while blocked closes the overlay.
5. Toggling "Water reminder" off stops the timer; "Take a water break now"
   opens it on demand.

1-3 are exercised headlessly in the offscreen flow check (phase transitions,
dodge-stays-in-bounds, instant-yes→confirm→resume). 4-5 by running the tile.
