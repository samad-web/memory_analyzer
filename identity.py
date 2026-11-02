r"""Who this laptop is, and which team it reports to.

Two separate things live here, deliberately kept apart:

* **Device identity** -- a random id minted once per machine and never changed,
  plus the display name to show it under. The id is what rows in the cloud are
  keyed by, so renaming yourself does not fork your history.
* **Team enrollment** -- the Supabase project to report to, the shared insert
  key, and what this member agreed to share. Absent on a solo install, which is
  the default: nothing leaves the machine until ``team.json`` exists.

``team.json`` is written by the installer (or the dashboard's Deploy tab) rather
than by hand. It is read on every sync, so revoking a laptop is a matter of
deleting the file -- no uninstall needed.
"""

from __future__ import annotations

import json
import os
import socket
import uuid

STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "SystemMonitorWidget",
)
DEVICE_PATH = os.path.join(STATE_DIR, "device.json")
TEAM_PATH = os.path.join(STATE_DIR, "team.json")

# Bumped when the reported row shape changes, so a dashboard can tell an old
# laptop from a new one instead of guessing from missing columns.
AGENT_VERSION = "1.0.0"

TEAM_DEFAULTS = {
    "enabled": False,
    # Supabase project REST root, e.g. https://abcd.supabase.co
    "url": "",
    # The project's anon/publishable key. Insert-only by RLS policy -- see
    # schema.sql. Safe to ship to every laptop; it cannot read anything back.
    "anon_key": "",
    "team_id": "",
    # How this member appears on the board. Blank falls back to the Windows
    # user name.
    "member": "",
    # Each toggle gates one table. A member who turns a stream off simply
    # stops reporting it; the dashboard shows the gap rather than a zero.
    "share_tasks": True,
    "share_sessions": True,
    "share_health": True,
    # Prompt text is the one stream that carries content rather than counts, so
    # it is opt-in separately even when tasks are shared.
    "share_prompts": True,
    "sync_seconds": 60,
}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> bool:
    """Atomic write, so a reader never sees half a config."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# -- device ----------------------------------------------------------------


def device_id() -> str:
    """This machine's id, minted on first call and stable thereafter.

    A random uuid rather than a hardware fingerprint: a MAC address or disk
    serial would be stable too, but it identifies the machine to anyone who
    reads the table, and nothing here needs that.
    """
    data = _read_json(DEVICE_PATH)
    existing = data.get("device_id")
    if isinstance(existing, str) and len(existing) >= 8:
        return existing
    fresh = uuid.uuid4().hex
    data["device_id"] = fresh
    _write_json(DEVICE_PATH, data)
    return fresh


def host_name() -> str:
    try:
        return socket.gethostname()[:60]
    except OSError:
        return "unknown"


def member_name() -> str:
    """The display name for this laptop on the team board."""
    configured = str(team_config().get("member") or "").strip()
    if configured:
        return configured[:60]
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return (user.strip() or host_name())[:60]


def device_row() -> dict:
    """The ``devices`` row describing this laptop."""
    cfg = team_config()
    return {
        "device_id": device_id(),
        "team_id": str(cfg.get("team_id") or ""),
        "member": member_name(),
        "host": host_name(),
        "platform": "windows",
        "agent_version": AGENT_VERSION,
    }


# -- team ------------------------------------------------------------------


def team_config() -> dict:
    cfg = dict(TEAM_DEFAULTS)
    stored = _read_json(TEAM_PATH)
    cfg.update({k: v for k, v in stored.items() if k in TEAM_DEFAULTS})
    return cfg


def save_team_config(cfg: dict) -> bool:
    keep = {k: v for k, v in cfg.items() if k in TEAM_DEFAULTS}
    return _write_json(TEAM_PATH, keep)


def enrolled() -> bool:
    """True when this laptop has somewhere to report to and is allowed to."""
    cfg = team_config()
    return bool(
        cfg.get("enabled")
        and str(cfg.get("url") or "").strip()
        and str(cfg.get("anon_key") or "").strip()
        and str(cfg.get("team_id") or "").strip()
    )


def unenroll() -> bool:
    """Stop reporting. Keeps the device id, so re-enrolling rejoins the same
    row rather than appearing as a new laptop."""
    try:
        os.remove(TEAM_PATH)
        return True
    except OSError:
        return False
