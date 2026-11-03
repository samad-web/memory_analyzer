# Shareable `.exe` for the team

## Purpose

Teammates install the tile without needing Python, pip, PowerShell execution
policy changes, or anything beyond a double-click.

## Approach

Build a single-file Windows installer `MemoryAnalyzerSetup.exe` with
**Inno Setup** (free, standard tool, no runtime license). Inside the installer:

1. A frozen build of the tile and the bridge, produced by **PyInstaller** in
   `--onedir` mode. PyInstaller is a build-time dependency only; nothing new ships
   to the user beyond what the tile already ships (PySide6/psutil/pywin32,
   bundled into the frozen tree).
2. The existing `install/install.ps1` logic, rewritten as Inno Setup `[Code]`
   Pascal — reusing its exact steps (hooks registration, scheduled task, team
   file). Reason: Inno Setup is self-contained, so the user never sees a
   PowerShell window.

Why not PyInstaller `--onefile`:

- `--onefile` extracts to `%TEMP%` on every launch — 1-2 second startup, dropped
  files that AV scanners sometimes quarantine. `--onedir` + Inno Setup is faster
  and friendlier to corporate AV.

Why not Nuitka or py2exe:

- Nuitka builds take 10-20 minutes and the result is often larger than
  PyInstaller for GUI apps. py2exe is unmaintained for PySide6.

## What's in the installer

- The tile (`widget.py` + its dependencies, frozen).
- `webserver.py` + the dashboard web assets.
- `bridge.py` and `sirahos.py` (the WorkLog bridge).
- `sync.py` (the Supabase reporter, same as today).
- `hooks.py` and a one-shot step to register it into Claude Code's `settings.json`.
- Inno Setup wizard pages:
  - A page asking for the **SirahOS API URL** (prefilled with ours, hidden if the
    user clicks "custom").
  - A page asking for **Supabase team telemetry** (optional, same URL/anon key/team-id
    inputs as the current PowerShell flow).
  - A checkbox: **"Run the tile at logon"** (reporter mode leaves it off).
- A post-install page: a link to launch the tile. First launch pops the SirahOS
  sign-in page in the user's default browser.

## Code signing

**Decision needed from the user** (see PLAN.md). Unsigned exes trigger Windows
SmartScreen's "Unknown publisher" warning on first run — a teammate has to click
"More info → Run anyway". Options:

1. **Ship unsigned.** Zero cost. Teammates get the warning once per machine. For
   a small internal team this is usually fine.
2. **Buy an EV code-signing certificate** (~$300-400/year). SmartScreen clears
   immediately. Right call if we ever share outside the team.

Recommendation: start unsigned, revisit if we hit a team member whose IT
disables "run anyway".

## Build & release flow

- A new folder `build-exe/` at repo root with the Inno Setup script, the
  PyInstaller spec, and a `build.ps1` that chains them.
- A new file `build-docs/features/exe-distribution.md` (this file).
- A GitHub Action or a local `build.ps1` run produces `MemoryAnalyzerSetup.exe`
  (~200MB — PySide6 is heavy).
- The installer is checked into **GitHub Releases** (not committed to the repo —
  the repo keeps text).

## Acceptance criteria

1. A fresh Windows 11 VM with no Python installed can run `MemoryAnalyzerSetup.exe`
   and end up with a working tile, a registered Claude Code hook set, a scheduled
   push task, and a visible SirahOS sign-in page.
2. Uninstalling from "Add or remove programs" removes everything — the install
   dir, the scheduled task, the hook entries from Claude Code `settings.json`
   (restoring the backup), the DPAPI blob.
3. The installer runs without administrator rights (reporter mode).
4. Re-running the installer on an already-installed machine upgrades in place
   without losing the user's SirahOS sign-in or their folder→project mapping.
