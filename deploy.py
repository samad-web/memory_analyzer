r"""Builds the folder you hand a teammate.

The Deploy tab can show a one-line command that runs ``install/install.ps1``
straight from this directory, which is the quickest path when everyone can
reach the same share. A bundle is for when they cannot: a self-contained folder
carrying the agent files, the teammate's ``team.json`` already filled in, and
the installer, so the whole thing is a copy and a double-click.

Two install shapes, because asking a colleague to accept an always-on-top
window on their desktop is a different request from asking them to let a hook
log token counts:

**reporter** -- ``notify.py`` and the sync daemon. No Qt, no window, nothing
visible. Needs Python and ``psutil``. This is the default, and the one to offer
first.

**full** -- the tile and their own dashboard as well, so they get the same view
of their own work that you get of the team's.

The bundle contains the project's anon key, which is safe to hand over by
design: the RLS policies in ``schema.sql`` let that key insert and never
select. It is still a credential for *writing*, so a bundle belongs on a share
your team can reach and not on a public link.
"""

from __future__ import annotations

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything a reporting-only install needs. sensors.py and msi_ec.py are here
# because the health stream reads them; both degrade to "no reading" on a
# non-MSI laptop rather than failing, which is why they are safe to ship wide.
REPORTER_FILES = (
    "notify.py",
    "events.py",
    "agent_alert.py",
    "identity.py",
    "cloud.py",
    "sync.py",
    "stats.py",
    "hooks.py",
    "sensors.py",
    "msi_ec.py",
    # The memory and goal layers. These are not optional extras for a reporter:
    # hooks.py registers a SessionStart hook that runs `notify.py context`, and
    # notify.py imports `observe` when a turn ends. Leave them out and the hook
    # raises ImportError, which notify's top-level guard turns into a silent
    # exit(0) -- a hook that does nothing, invisibly, on someone else's laptop.
    # tests/test_deploy.py checks this list is import-closed so it cannot drift
    # again.
    "store.py",
    "observe.py",
    "goals.py",
    "context.py",
    "mcp_server.py",
)

# The tile and the dashboard on top of that.
FULL_EXTRA_FILES = (
    "widget.py",
    "dashboard.py",
    "charts.py",
    "viz.py",
    "webserver.py",
    "memory.py",
    "deploy.py",
    "run.vbs",
    "run.bat",
    "schema.sql",
)

FULL_EXTRA_DIRS = ("web",)

REPORTER_REQUIREMENTS = "psutil>=6.0\n"
FULL_REQUIREMENTS = "PySide6>=6.7\npsutil>=6.0\npywin32>=306\n"


def build_bundle(target_dir: str, team_cfg: dict, mode: str = "reporter") -> str:
    """Write a ready-to-run install folder. Returns its path."""
    mode = mode if mode in ("reporter", "full") else "reporter"
    out = os.path.join(target_dir, f"agent-monitor-{mode}")
    os.makedirs(out, exist_ok=True)

    missing = []
    for name in REPORTER_FILES + (FULL_EXTRA_FILES if mode == "full" else ()):
        source = os.path.join(HERE, name)
        if not os.path.exists(source):
            missing.append(name)
            continue
        shutil.copy2(source, os.path.join(out, name))
    if mode == "full":
        for folder in FULL_EXTRA_DIRS:
            source = os.path.join(HERE, folder)
            if os.path.isdir(source):
                shutil.copytree(
                    source, os.path.join(out, folder), dirs_exist_ok=True
                )

    install_dir = os.path.join(out, "install")
    os.makedirs(install_dir, exist_ok=True)
    script = os.path.join(HERE, "install", "install.ps1")
    if os.path.exists(script):
        shutil.copy2(script, os.path.join(install_dir, "install.ps1"))
    else:
        missing.append("install/install.ps1")

    with open(os.path.join(out, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write(FULL_REQUIREMENTS if mode == "full" else REPORTER_REQUIREMENTS)

    # The teammate's team.json, pre-filled. Deliberately not `enabled` until
    # the installer has actually wired the hooks -- a config that says it is
    # reporting while nothing is logging would be a lie on the dashboard.
    config = {
        "enabled": True,
        "url": str(team_cfg.get("url") or ""),
        "anon_key": str(team_cfg.get("anon_key") or ""),
        "team_id": str(team_cfg.get("team_id") or ""),
        "member": "",
        "share_tasks": bool(team_cfg.get("share_tasks", True)),
        "share_sessions": bool(team_cfg.get("share_sessions", True)),
        "share_health": bool(team_cfg.get("share_health", True)),
        "share_prompts": bool(team_cfg.get("share_prompts", True)),
        "sync_seconds": int(team_cfg.get("sync_seconds", 60) or 60),
    }
    with open(os.path.join(out, "team.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    with open(os.path.join(out, "INSTALL.md"), "w", encoding="utf-8") as fh:
        fh.write(_readme(mode, config, missing))

    return out


def _readme(mode: str, config: dict, missing: list[str]) -> str:
    shared = [
        name
        for name, key in (
            ("finished tasks and token counts", "share_tasks"),
            ("the text of each prompt", "share_prompts"),
            ("live session states", "share_sessions"),
            ("machine health (CPU, RAM, GPU, temperatures)", "share_health"),
        )
        if config.get(key)
    ]
    not_shared = [
        name
        for name, key in (
            ("finished tasks and token counts", "share_tasks"),
            ("the text of each prompt", "share_prompts"),
            ("live session states", "share_sessions"),
            ("machine health", "share_health"),
        )
        if not config.get(key)
    ]

    warning = ""
    if missing:
        warning = (
            "\n> **Incomplete bundle.** These files were not found when it was "
            "built, so the installer will fail until they are added:\n> "
            + ", ".join(f"`{m}`" for m in missing)
            + "\n"
        )

    tile_bits = (
        """
### What you get

A small always-on-top tile showing CPU, RAM, GPU and fan, plus a live board of
your coding-agent sessions, and a dashboard of your own work (tray icon →
Dashboard…). Drag the tile by the green dot; clicks pass straight through it
otherwise.
"""
        if mode == "full"
        else """
### What you get

Nothing on screen. Five Claude Code hooks log each turn locally, and a
scheduled task uploads the counts once a minute. To see what is being sent:

    python sync.py status
"""
    )

    return f"""# Agent monitor -- {mode} install

Installs into `%LOCALAPPDATA%\\SystemMonitorWidget\\agent` and reports to the
team project `{config['team_id']}`.

## Install

Right-click **PowerShell** → *Run as Administrator* is **not** required for a
reporter install. Open PowerShell in this folder and run:

    powershell -ExecutionPolicy Bypass -File .\\install\\install.ps1

It reads the `team.json` beside it, so there is nothing to type. Add
`-Member "Your Name"` to choose how you appear on the board, or
`-SharePrompts:$false` to report counts without the text of your prompts.

To undo everything:

    powershell -ExecutionPolicy Bypass -File .\\install\\install.ps1 -Uninstall
{warning}{tile_bits}
## What this laptop will send

{chr(10).join(f'- {item}' for item in shared) or '- nothing'}

{('Not sent: ' + ', '.join(not_shared) + '.') if not_shared else ''}

Nothing is sent until the installer runs, and deleting
`%LOCALAPPDATA%\\SystemMonitorWidget\\team.json` stops it again immediately --
the file is re-read on every sync, so there is no uninstall to wait for.

The key in `team.json` can **write** to the team project and cannot read it
back, so it does not let this laptop see anyone else's activity.

## Requirements

Python 3.10 or newer on `PATH`. The installer pip-installs
`requirements.txt` ({'PySide6, psutil, pywin32' if mode == 'full' else 'psutil'}).
"""
