r"""Entry point for agent hooks: reports a session's state to the desktop widget.

Usage:
    python notify.py prompt      ["what the user asked"]   # UserPromptSubmit
    python notify.py working     # PreToolUse -- resume after a block
    python notify.py needs-input ["why it stopped"]        # Notification
    python notify.py done        ["what it finished"]      # Stop
    python notify.py end          # SessionEnd -- remove its row
    python notify.py clear        # dismiss every session's entry
    python notify.py clear .      # dismiss this project's entries only

Claude Code calls this from hooks in settings.json. Hook input arrives as JSON
on stdin; its ``session_id``, ``cwd`` and ``transcript_path`` identify which
session's row to update and where to read its token usage.

Two things happen on most calls: the live mailbox (``agent_alert``) is updated
so the tile's board reflects the session's state, and one line is appended to
the activity log (``events``) so the Scorecard can measure response time,
throughput, spend and effort over time. Token and tool counts are read from the
transcript only when a turn ends, never per tool call.

It must never fail loudly: a hook that exits non-zero or writes to stderr can
disrupt the agent it is reporting on. Every path exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_alert
import events

# Payload keys worth showing on the tile, best first. "hook_event_name" is
# deliberately absent: on a Stop hook it is the only key present, and a banner
# reading "Stop" says less than the project name the widget falls back to.
MESSAGE_KEYS = ("message", "prompt", "reason", "tool_name")

# Within this many seconds, a fresh "working" state is left alone so the
# per-tool PreToolUse hook does not rewrite the mailbox dozens of times a turn.
# How long to wait for a payload on stdin before giving up on it. The
# payload is already buffered by the time a hook runs, so this only ever
# elapses when the invoker left the pipe open.
PAYLOAD_TIMEOUT_SECONDS = 2.0

WORKING_FRESH_SECONDS = 8

# The Notification hook covers two unrelated things: a real question or
# permission prompt, and an idle reminder that fires a minute or so after a
# turn has already ended. Only the first is someone being blocked. Treating
# the reminder as "needs you" turned a finished session from green to orange
# on its own, with nothing actually waiting behind it.
IDLE_NOTICES = (
    "waiting for your input",
    "is idle",
    "has been idle",
    "idle for",
)


def read_payload() -> dict:
    """The hook's JSON payload from stdin, or an empty dict.

    The read is bounded, which matters more than it looks. ``stdin.read()``
    returns when the writer closes the pipe, and Claude Code does close it --
    but anything else that invokes this (a wrapper, a scheduled task, a human
    at a shell) may not, and then the read blocks until the hook's own timeout
    kills it. For the asynchronous hooks that would waste a process; for
    ``SessionStart``, which has to be synchronous to have its output read at
    all, it would add that delay to the start of every session.

    So the read happens on a daemon thread and is given
    ``PAYLOAD_TIMEOUT_SECONDS``. If nothing arrives the payload is simply
    empty, and the caller falls back to the working directory -- which for a
    hook is the session's own directory anyway, so the degradation is mild.
    """
    if sys.stdin is None:
        return {}
    try:
        if sys.stdin.isatty():
            return {}
    except (OSError, ValueError):
        return {}

    box: list[str] = []

    def _slurp() -> None:
        try:
            box.append(sys.stdin.read(65536))
        except Exception:  # noqa: BLE001 - closed, detached, or decoding oddly
            pass

    reader = threading.Thread(target=_slurp, daemon=True)
    reader.start()
    reader.join(PAYLOAD_TIMEOUT_SECONDS)
    if not box:
        return {}
    raw = box[0]
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"message": raw.strip().splitlines()[0][:120]}
    return data if isinstance(data, dict) else {}


def is_idle_notice(message: str) -> bool:
    """Whether a Notification is just the idle reminder, not a real block."""
    text = (message or "").lower()
    return any(phrase in text for phrase in IDLE_NOTICES)


def message_from(payload: dict) -> str:
    for key in MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# -- which window is this session running in? ------------------------------
#
# A hook has no handle to its own terminal, but it *is* a descendant of it:
# python <- claude <- shell <- terminal/editor. Walking up the process tree and
# looking for a visible top-level window owned by one of those ancestors finds
# the window this session lives in. Recording it per session is what lets the
# widget raise one specific agent rather than just "something in that folder".
#
# Caveat: two sessions in two *tabs* of one window share that window, so they
# resolve to the same handle. Windows exposes no way to focus a single tab.


def _parent_map() -> dict:
    """{pid: parent_pid} for every process, from a single snapshot."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    parents: dict = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return parents


def _ancestry(limit: int = 14) -> dict:
    """{pid: distance} for this process and its ancestors, nearest first."""
    parents = _parent_map()
    chain = [os.getpid()]
    seen = {os.getpid()}
    current = os.getpid()
    for _ in range(limit):
        parent = parents.get(current)
        if not parent or parent in seen:
            break
        seen.add(parent)
        chain.append(parent)
        current = parent
    return {pid: depth for depth, pid in enumerate(chain)}


def window_alive(hwnd: int) -> bool:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return bool(hwnd) and bool(user32.IsWindow(hwnd)) and bool(
            user32.IsWindowVisible(hwnd)
        )
    except Exception:  # noqa: BLE001
        return False


def owner_window() -> int:
    """Handle of the nearest ancestor's visible top-level window, or 0."""
    try:
        import ctypes
        from ctypes import wintypes

        depths = _ancestry()
        user32 = ctypes.windll.user32
        best = [10**9, 0]

        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _collect(hwnd, _lparam):
            if not hwnd or not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            depth = depths.get(int(pid.value))
            if depth is not None and depth < best[0]:
                best[0], best[1] = depth, int(hwnd)
            return True

        user32.EnumWindows(EnumProc(_collect), 0)
        return best[1]
    except Exception:  # noqa: BLE001 - a hook must never break its agent
        return 0


def window_for(key: str, current=None) -> int:
    """This session's window, reusing the recorded one while it is still open."""
    if current is None:
        current = agent_alert.read_one(key)
    if current is not None and window_alive(current.hwnd):
        return current.hwnd
    return owner_window()


def main(argv: list[str]) -> int:
    action = (argv[1] if len(argv) > 1 else "done").strip().lower()
    payload = read_payload()
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    sid = str(payload.get("session_id") or "")
    transcript = str(payload.get("transcript_path") or "")
    # Named for the repository, not for whichever subdirectory the agent is
    # standing in -- see agent_alert.project_root.
    proj = agent_alert.project_name(cwd) or "Claude Code"

    if action == "clear":
        # "clear" alone dismisses everything; "clear ." just this project.
        if " ".join(argv[2:]).strip():
            agent_alert.clear_project(cwd)
        else:
            agent_alert.clear()
        return 0

    key = agent_alert.key_for(cwd, sid)
    message = " ".join(argv[2:]).strip() or message_from(payload)

    if action == "context":
        # SessionStart. Prints the orientation block for this project and
        # nothing else. Read-only: it never runs a check command and never
        # touches the network, so the worst it can cost a starting session is
        # the time to read a few rows.
        import context

        return context.emit(proj)

    if action in ("end", "session-end"):
        # Capture any final token usage before the session's row disappears.
        tok, model, tools = events.tokens_since(sid, transcript)
        events.log("end", sid, cwd, proj, tok=tok, model=model, tools=tools)
        agent_alert.clear(key)
        return 0

    if action in ("prompt", "start"):
        # The prompt line is logged here and nowhere else: it is what the
        # dashboard shows as the task's name, and by the time the turn ends the
        # payload no longer carries it.
        events.log("prompt", sid, cwd, proj, msg=message)
        agent_alert.write(
            agent_alert.WORKING, message, proj, key=key, sid=sid,
            cwd=cwd, hwnd=window_for(key),
        )
        return 0

    if action == "working":
        # PreToolUse. Its real job is to flip a blocked session back to working
        # once the agent resumes, and to record *when* you unblocked it, which
        # is what the Scorecard measures your reply time from.
        #
        # The mailbox and the event log want different things here, so they are
        # decided separately:
        #
        # * The mailbox is skipped while the row already says working and is
        #   fresh -- the no-churn case this hook hits dozens of times a turn.
        # * The event is only logged when a block is actually open. A `resume`
        #   exists to pair with a `wait`, and `stats._pairings` discards one
        #   that pairs with nothing, so an unpaired resume was never read by
        #   anything. Writing them anyway made up 87% of the activity log
        #   (4,606 of 5,279 events, against 62 waits), because a tool call
        #   lasting over WORKING_FRESH_SECONDS slips past the freshness check.
        #   That was affordable while the log only fed counters and is not now
        #   that it is the substrate for memory and milestone inference.
        current = agent_alert.read_one(key)
        blocked = current is not None and current.kind == agent_alert.NEEDS_INPUT

        if (
            not blocked
            and current is not None
            and current.kind == agent_alert.WORKING
            and (time.time() - current.at) < WORKING_FRESH_SECONDS
        ):
            return 0

        if blocked:
            events.log("resume", sid, cwd, proj)

        agent_alert.write(
            agent_alert.WORKING, message, proj, key=key, sid=sid,
            cwd=cwd, hwnd=window_for(key, current),
        )
        return 0

    if action in ("needs-input", "needs_input"):
        current = agent_alert.read_one(key)
        if (
            current is not None
            and current.kind == agent_alert.DONE
            and is_idle_notice(message)
        ):
            # The turn is already finished and shown as such. Re-colouring it
            # as "needs you" would be a false alarm, and logging a wait that
            # nobody is blocked on would inflate the dashboard's blocked time.
            return 0
        events.log("wait", sid, cwd, proj)
        agent_alert.write(
            agent_alert.NEEDS_INPUT, message, proj, key=key, sid=sid,
            cwd=cwd, hwnd=window_for(key, current),
        )
        return 0

    if action == "done":
        tok, model, tools = events.tokens_since(sid, transcript)
        events.log("done", sid, cwd, proj, tok=tok, model=model, tools=tools)
        agent_alert.write(
            agent_alert.DONE, message, proj, key=key, sid=sid,
            cwd=cwd, hwnd=window_for(key),
        )
        # Record what the turn did, not just what it cost. Deterministic
        # parsing of the transcript Claude Code already wrote, so this spends
        # no tokens and makes no network call; it swallows its own errors, so a
        # failure here costs an observation and never the turn.
        try:
            import observe

            observe.record(
                sid, transcript, cwd, proj,
                ended_at=time.time(), tok=tok, model=model,
            )
        except Exception:
            pass
        return 0

    # Unknown action: record it as a finished turn rather than failing.
    agent_alert.write(
        agent_alert.DONE, message, proj, key=key, sid=sid,
        cwd=cwd, hwnd=window_for(key),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 - a hook must never break its agent
        sys.exit(0)
