r"""A mailbox directory where coding agents report themselves to the widget.

Each *session* gets its own file, keyed by its working directory plus its
session id, so several agents can report at once: different projects, and also
several sessions inside the same project, each hold their own slot. With a
single shared file, a second session's update would erase the first one's
"waiting on you".

Files are used rather than a socket or a port because hooks are short-lived
processes that may run elevated or not, in any working directory, with no setup
and nothing listening.

Three states:

``working``      the agent is on a task (written when a prompt is submitted).
                 Shown so running sessions are visible at all, not just
                 finished ones; reaped after a few hours in case the session
                 crashed without saying goodbye.
``needs_input``  the agent is blocked and waiting on the user -- a permission
                 prompt, a question, a choice. Stays until dismissed or the
                 turn ends.
``done``         the agent finished its turn. Expires on its own.

Write one with ``notify.py``, which is what the hooks call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

ALERT_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "SystemMonitorWidget",
    "alerts",
)

WORKING = "working"
NEEDS_INPUT = "needs_input"
DONE = "done"

# "done" is informational and expires on its own. "needs_input" normally
# clears when the agent resumes (its PreToolUse hook flips it back to working),
# so its TTL is only a backstop for a session that died while waiting -- long
# enough not to hide a prompt the user has not seen yet. "working" is reaped
# only as a safety net for sessions that died without a SessionEnd.
DONE_TTL_SECONDS = 10 * 60
NEEDS_INPUT_TTL_SECONDS = 20 * 60
WORKING_TTL_SECONDS = 3 * 60 * 60

TTL_SECONDS = {
    DONE: DONE_TTL_SECONDS,
    NEEDS_INPUT: NEEDS_INPUT_TTL_SECONDS,
    WORKING: WORKING_TTL_SECONDS,
}

# Blocked sessions first, then finished work to look at, then ambient activity.
_SORT_ORDER = {NEEDS_INPUT: 0, DONE: 1, WORKING: 2}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# What marks the top of a project. `.git` is the one that matters; the others
# cost nothing to check and help the occasional older checkout.
_ROOT_MARKERS = (".git", ".hg", ".svn")


def project_root(path: str, limit: int = 16) -> str:
    """The repository root at or above ``path``, or ``path`` when there is none.

    An agent does not stay in the directory it started in. Working inside
    ``Voice_Transcriber/platform`` makes ``basename(cwd)`` read ``platform``,
    so the session appears on the board under the name of whichever
    subdirectory it happens to be standing in -- and moving between
    subdirectories mid-session splits one agent across two rows, each going
    stale in turn.

    Resolving to the repository root fixes both: one session keeps one row,
    under the name its human knows it by. A nested checkout still wins over its
    parent, because the nearest marker is found first, which is correct -- that
    inner repository really is the project.
    """
    path = os.path.abspath(path or os.getcwd())
    current = path
    for _ in range(limit):
        for marker in _ROOT_MARKERS:
            try:
                if os.path.exists(os.path.join(current, marker)):
                    return current
            except OSError:
                return path
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return path


def project_name(path: str) -> str:
    """How a project should be labelled on the board."""
    root = project_root(path)
    return os.path.basename(root.rstrip("\\/")) or os.path.basename(root) or "project"


def key_for(path: str, sid: str = "") -> str:
    """A stable, readable filename stem for one session of one project.

    Keyed on the *repository* root rather than the raw working directory, so a
    session that moves between subdirectories keeps one row instead of leaving
    an orphan behind at every ``cd``.

    The basename alone would collide between two checkouts of the same name in
    different parents, so a short digest of the full path is appended; the
    session id (when known) separates concurrent sessions of the same project.
    """
    path = project_root(path or os.getcwd())
    name = _UNSAFE.sub("_", os.path.basename(path.rstrip("\\/"))) or "project"
    digest = hashlib.md5(path.lower().encode("utf-8")).hexdigest()[:8]
    project = f"{name[:32]}-{digest}"
    sid = (sid or "").strip()
    if not sid:
        return project
    # Hash the whole session id: a prefix could collide between two sessions
    # whose ids share a leading run, which plain truncation would merge.
    sid_tag = hashlib.md5(sid.encode("utf-8")).hexdigest()[:8]
    return f"{project}.{sid_tag}"


@dataclass
class Alert:
    kind: str
    message: str
    source: str
    at: float
    key: str = ""
    sid: str = ""
    cwd: str = ""
    # Top-level window of the terminal/editor this session runs in, captured by
    # the hook. 0 when it could not be determined.
    hwnd: int = 0

    @property
    def project_key(self) -> str:
        """Stable identity of the project, used to group sessions together."""
        if self.cwd:
            return os.path.abspath(self.cwd).lower().replace("\\", "/").rstrip("/")
        # Entries written before cwd was recorded: drop the session suffix.
        if "." in self.key:
            head, tail = self.key.rsplit(".", 1)
            if len(tail) == 8 and all(c in "0123456789abcdef" for c in tail):
                return head
        return self.key

    @property
    def session_tag(self) -> str:
        """Short, stable label distinguishing sessions of the same project."""
        if "." in self.key:
            return self.key.rsplit(".", 1)[-1][:4]
        return (self.sid or "")[:4]

    @property
    def needs_input(self) -> bool:
        return self.kind == NEEDS_INPUT

    @property
    def attention(self) -> bool:
        """True for the states that should actively grab the user."""
        return self.kind in (NEEDS_INPUT, DONE)

    @property
    def expires_at(self) -> float:
        """When this entry ages out, or infinity if it never does.

        Exposed so a poller can wait until the soonest expiry instead of
        re-reading the whole mailbox every second to find out that nothing has
        changed yet.
        """
        ttl = TTL_SECONDS.get(self.kind)
        return self.at + ttl if ttl is not None else float("inf")

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def age_text(self) -> str:
        seconds = max(0, int(time.time() - self.at))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h"

    @property
    def headline(self) -> str:
        return "NEEDS YOU" if self.needs_input else "TASK DONE"


def write(
    kind: str,
    message: str = "",
    source: str = "",
    key: str = "",
    sid: str = "",
    cwd: str = "",
    hwnd: int = 0,
) -> str:
    """Record one session's state. Returns the path written, or "" on failure.

    Called from a hook, so it must not raise -- and until now it only said so.
    Every step here can fail in the ordinary course of events on Windows: the
    directory may be unwritable, the disk full, or the temporary file held open
    for a moment by a virus scanner, which makes ``os.replace`` fail. Any of
    those used to propagate out of a hook whose job was to be invisible.

    The caller logged its event first, so a failure here costs one row on the
    board and nothing else.
    """
    try:
        cwd = cwd or os.getcwd()
        key = key or key_for(cwd, sid)
        payload = {
            "kind": kind if kind in (WORKING, NEEDS_INPUT, DONE) else DONE,
            "message": (message or "").strip()[:120],
            "source": (source or os.path.basename(cwd) or "agent").strip()[:40],
            "at": time.time(),
            "sid": (sid or "").strip()[:36],
            "cwd": cwd,
            "hwnd": int(hwnd or 0),
        }
        os.makedirs(ALERT_DIR, exist_ok=True)
        path = os.path.join(ALERT_DIR, key + ".json")
        # Write then replace, so the widget never reads a half-written file.
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(temp, path)
        return path
    except Exception:  # noqa: BLE001 - a hook must never break its agent
        return ""


def _load(path: str, key: str) -> Alert | None:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        hwnd = int(raw.get("hwnd", 0) or 0)
    except (TypeError, ValueError):
        hwnd = 0
    return Alert(
        kind=str(raw.get("kind", DONE)),
        message=str(raw.get("message", "")),
        source=str(raw.get("source", "agent")),
        at=float(raw.get("at", 0.0)),
        key=key,
        sid=str(raw.get("sid", "")),
        cwd=str(raw.get("cwd", "")),
        hwnd=hwnd,
    )


def read_one(key: str) -> Alert | None:
    """The current entry for one session key, or None. Does not reap."""
    path = os.path.join(ALERT_DIR, key + ".json")
    return _load(path, key)


def read_all(reap: bool = True) -> list[Alert]:
    """Every live session, most urgent first.

    Blocked sessions sort above finished ones, which sort above running ones;
    within each group the newest comes first. Expired files are deleted as they
    are found, so the directory does not grow without bound.
    """
    try:
        names = os.listdir(ALERT_DIR)
    except OSError:
        return []

    alerts: list[Alert] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(ALERT_DIR, name)
        alert = _load(path, name[: -len(".json")])
        if alert is None:
            continue
        if alert.expired:
            if reap:
                try:
                    os.remove(path)
                except OSError:
                    pass
            continue
        alerts.append(alert)

    # One session is one row, even if its cwd was spelled differently between
    # hook firings (e.g. "B2-Consultants" vs "B2Consultants"), which would
    # otherwise produce two project keys for the same session. Keep the newest
    # entry per session id; stale siblings are deleted so they stop lingering.
    newest: dict[str, Alert] = {}
    for alert in alerts:
        if not alert.sid:
            newest[alert.key] = alert  # no sid: keyed by file, cannot dedup
            continue
        seen = newest.get(alert.sid)
        if seen is None or alert.at > seen.at:
            if seen is not None and reap:
                _remove(seen.key)
            newest[alert.sid] = alert
        elif reap:
            _remove(alert.key)

    result = list(newest.values())

    # Sessions of one project stay together as a block, so two agents on the
    # same repo read as one group rather than being scattered between other
    # projects. A project is ranked by its most urgent session, so a group with
    # a blocked agent still floats to the top; inside a group the same
    # urgency-then-newest order applies.
    rank: dict[str, tuple] = {}
    for alert in result:
        key = alert.project_key
        order = (_SORT_ORDER.get(alert.kind, 3), -alert.at)
        if key not in rank or order < rank[key]:
            rank[key] = order
    result.sort(
        key=lambda a: (
            rank[a.project_key],
            a.project_key,
            _SORT_ORDER.get(a.kind, 3),
            -a.at,
        )
    )
    return result


def _remove(key: str) -> None:
    try:
        os.remove(os.path.join(ALERT_DIR, key + ".json"))
    except OSError:
        pass


def clear(key: str = "") -> None:
    """Remove one session's entry, or every entry when no key is given."""
    if key:
        try:
            os.remove(os.path.join(ALERT_DIR, key + ".json"))
        except OSError:
            pass
        return
    for alert in read_all(reap=False):
        clear(alert.key)


def clear_attention() -> None:
    """Dismiss everything finished or waiting, but keep running sessions
    listed -- they describe reality, not something the user acknowledged."""
    for alert in read_all(reap=False):
        if alert.attention:
            clear(alert.key)


def clear_project(path: str) -> None:
    """Remove every session entry belonging to one project directory."""
    prefix = key_for(path)
    for alert in read_all(reap=False):
        if alert.key == prefix or alert.key.startswith(prefix + "."):
            clear(alert.key)


def stamp() -> tuple:
    """Cheap signature of the mailbox, for change detection while polling.

    Covers files appearing, being rewritten and being removed, without parsing
    any of them.
    """
    try:
        names = sorted(n for n in os.listdir(ALERT_DIR) if n.endswith(".json"))
    except OSError:
        return ()
    out = []
    for name in names:
        try:
            out.append((name, os.path.getmtime(os.path.join(ALERT_DIR, name))))
        except OSError:
            continue
    return tuple(out)
