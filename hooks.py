r"""Installs (and removes) the Claude Code hooks that feed the widget.

The five hooks are what make everything else work -- without them the tile has
no sessions to show and the dashboard has no tasks to count. Wiring them by
hand is fine for one laptop; it is the wrong thing to ask of a teammate running
an installer, so this does it programmatically.

Three rules it follows, all learned from what goes wrong otherwise:

* **Merge, never overwrite.** ``settings.json`` holds permissions, the model
  choice and the theme. It is read, amended and written back, so installing
  hooks cannot cost someone their settings.
* **Idempotent.** Entries pointing at this project's ``notify.py`` are replaced
  rather than appended, so running the installer twice does not fire every hook
  twice. Hooks belonging to anything else are left exactly as they are.
* **Back up first.** The previous file is copied beside itself before a write.

Hooks are registered ``async`` so they never hold up a turn, with a timeout
well above what the script needs -- ``notify.py`` writes two small files and
exits.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# (hook event, notify.py action, what it is for)
HOOKS = (
    ("UserPromptSubmit", "prompt", "a prompt was sent to the agent"),
    ("PreToolUse", "working", "the agent resumed after being blocked"),
    ("Notification", "needs-input", "the agent wants a permission or an answer"),
    ("Stop", "done", "the agent finished a turn"),
    ("SessionEnd", "end", "the session closed"),
)

TIMEOUT_SECONDS = 10


def settings_path(scope: str = "user") -> str:
    """Where the hooks go. ``user`` is global; ``project`` is this repo only."""
    if scope == "project":
        return os.path.join(HERE, ".claude", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _notify_command(action: str, python: str = "python", root: str = "") -> str:
    """The command string for one hook.

    Forward slashes even on Windows: the value is JSON, and a backslash path
    has to be escaped twice to survive the file, which is a reliable way to
    produce a hook that silently never runs.
    """
    script = os.path.join(root or HERE, "notify.py").replace("\\", "/")
    return f'{python} "{script}" {action}'


def _entry(action: str, python: str, root: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": _notify_command(action, python, root),
                "timeout": TIMEOUT_SECONDS,
                # Never make the user wait on telemetry.
                "async": True,
            }
        ]
    }


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_ours(group: dict, root: str) -> bool:
    """True when this hook group runs our ``notify.py``.

    Matched on the script path rather than the whole command so a teammate who
    edited the python executable or the action still gets their entry replaced
    instead of gaining a duplicate.
    """
    needle = os.path.join(root, "notify.py").replace("\\", "/").lower()
    for hook in group.get("hooks", []) if isinstance(group, dict) else []:
        command = str(hook.get("command", "")).replace("\\", "/").lower()
        if needle in command or "notify.py" in command:
            return True
    return False


def install(
    scope: str = "user", python: str = "python", root: str = "", backup: bool = True
) -> dict:
    """Write the five hooks. Returns a report; raises only on an unwritable file."""
    root = root or HERE
    path = settings_path(scope)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    settings = _read(path)
    if backup and os.path.exists(path):
        try:
            shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        except OSError:
            pass

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    report = {"path": path, "added": [], "replaced": [], "kept_foreign": 0}

    for event, action, _why in HOOKS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        ours = [g for g in groups if isinstance(g, dict) and _is_ours(g, root)]
        foreign = [g for g in groups if isinstance(g, dict) and not _is_ours(g, root)]
        report["kept_foreign"] += len(foreign)
        (report["replaced"] if ours else report["added"]).append(event)
        # Ours goes last, so anything the user already had still runs first.
        hooks[event] = foreign + [_entry(action, python, root)]

    settings["hooks"] = hooks
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)
    return report


def uninstall(scope: str = "user", root: str = "") -> dict:
    """Remove only our entries, leaving any other hooks on those events."""
    root = root or HERE
    path = settings_path(scope)
    settings = _read(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return {"path": path, "removed": 0}

    removed = 0
    for event, _action, _why in HOOKS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        keep = [g for g in groups if not (isinstance(g, dict) and _is_ours(g, root))]
        removed += len(groups) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)

    settings["hooks"] = hooks
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        return {"path": path, "removed": 0, "error": str(exc)}
    return {"path": path, "removed": removed}


def status(scope: str = "user", root: str = "") -> dict:
    """Which of the five are wired up, for the dashboard and the installer."""
    root = root or HERE
    path = settings_path(scope)
    hooks = _read(path).get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    present = {}
    for event, action, _why in HOOKS:
        groups = hooks.get(event) or []
        present[event] = any(
            isinstance(g, dict) and _is_ours(g, root) for g in groups
        )
    return {
        "path": path,
        "exists": os.path.exists(path),
        "hooks": present,
        "complete": all(present.values()),
    }


_USAGE = """usage: python hooks.py <install|uninstall|status> [--project] [--python EXE]

  install     wire the five hooks into Claude Code's settings.json
  uninstall   remove them again (other hooks on those events are kept)
  status      report which of the five are present

  --project   write to this project's .claude/settings.json instead of ~/.claude
  --python    the interpreter the hooks should call (default: python)
"""


def main(argv: list[str]) -> int:
    command = (argv[1] if len(argv) > 1 else "status").strip().lower()
    scope = "project" if "--project" in argv else "user"
    python = "python"
    if "--python" in argv:
        index = argv.index("--python")
        if index + 1 < len(argv):
            python = argv[index + 1]

    if command == "install":
        report = install(scope=scope, python=python)
        print(f"wrote {report['path']}")
        if report["added"]:
            print("  added:    " + ", ".join(report["added"]))
        if report["replaced"]:
            print("  replaced: " + ", ".join(report["replaced"]))
        if report["kept_foreign"]:
            print(f"  left {report['kept_foreign']} unrelated hook group(s) alone")
        return 0

    if command == "uninstall":
        report = uninstall(scope=scope)
        print(f"removed {report.get('removed', 0)} entries from {report['path']}")
        return 0

    if command == "status":
        report = status(scope=scope)
        print(f"{report['path']}  ({'exists' if report['exists'] else 'missing'})")
        for event, ok in report["hooks"].items():
            print(f"  {'ok ' if ok else '-- '} {event}")
        print("all five wired" if report["complete"] else "incomplete")
        return 0 if report["complete"] else 1

    print(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
