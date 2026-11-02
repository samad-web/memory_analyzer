r"""Append-only activity log behind the widget's productivity metrics.

Every hook firing appends one small JSON line here -- a prompt submitted, an
agent blocked, a turn finished. The widget's Scorecard reads the log back and
turns it into response times, throughput, token spend and effort per project.

Two design choices keep this cheap enough to sit on a hot-running laptop:

* **No per-tool logging.** ``PreToolUse`` fires dozens of times a turn; writing
  a line each time would churn the disk. Instead the tool count for a turn is
  read straight from the session transcript when the turn ends.
* **Incremental transcript reads.** Token usage lives in the transcript Claude
  Code already writes. Rather than re-parse the whole file each turn, a byte
  offset per session is remembered, so only the lines added since last time are
  parsed. A long session costs the same per turn as a short one.

Written from hooks, which must never fail, so every function swallows its own
errors and simply records less rather than raising.
"""

from __future__ import annotations

import json
import os
import time

ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "SystemMonitorWidget",
)
EVENTS_PATH = os.path.join(ROOT, "events.jsonl")
OFFSETS_PATH = os.path.join(ROOT, "offsets.json")

# Keys of the token dict, matching the transcript's usage fields:
# input, output, cache-creation, cache-read.
TOK_KEYS = ("in", "out", "cc", "cr")

# A prompt line is kept for the dashboard's task list, not for archival, so it
# is clipped to something that still reads as a summary in a table cell.
MAX_MSG_CHARS = 240


def _zero_tok() -> dict:
    return {k: 0 for k in TOK_KEYS}


def _ensure() -> None:
    os.makedirs(ROOT, exist_ok=True)


def log(
    ev: str,
    sid: str = "",
    cwd: str = "",
    proj: str = "",
    tok: dict | None = None,
    model: str = "",
    tools: int = 0,
    msg: str = "",
) -> None:
    """Append one event. ``ev`` is prompt | resume | wait | done | end.

    ``tok``/``model``/``tools`` are attached only on turn-ending events, where
    they carry that turn's token usage and tool-call count.

    ``msg`` is the line the turn started from -- the prompt as typed. It is
    stored so the dashboard can list *what* each task was, not just that one
    happened; the live mailbox also holds it, but only until the entry expires.
    Truncated hard, because a pasted wall of text has no business in a metrics
    log that gets synced.
    """
    try:
        _ensure()
        rec: dict = {
            "t": round(time.time(), 3),
            "ev": ev,
            "sid": (sid or "")[:40],
            "proj": (proj or "")[:40],
            "cwd": cwd or "",
        }
        if tok and any(tok.get(k) for k in TOK_KEYS):
            rec["tok"] = {k: int(tok.get(k, 0)) for k in TOK_KEYS}
        if model:
            rec["model"] = model[:40]
        if tools:
            rec["tools"] = int(tools)
        if msg:
            rec["msg"] = " ".join(str(msg).split())[:MAX_MSG_CHARS]
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        # A single append of a short line is atomic enough across the
        # short-lived hook processes; the reader tolerates a torn line anyway.
        with open(EVENTS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def load() -> list[dict]:
    """Every logged event, oldest first. Unparseable lines are skipped."""
    out: list[dict] = []
    try:
        with open(EVENTS_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and "t" in rec and "ev" in rec:
                    out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("t", 0.0))
    return out


def load_tail(max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """The most recent events, reading at most ``max_bytes`` from the end.

    The cloud sync needs whole tasks, and a task spans a ``prompt`` and the
    ``done`` that may arrive minutes later, so it cannot simply walk forward
    from a byte offset -- advancing past the prompt would lose the task's name
    and start time. It re-reads a tail instead, which bounds the parse cost no
    matter how long the log has grown.

    The first line read is usually a partial one and is discarded.
    """
    try:
        size = os.path.getsize(EVENTS_PATH)
    except OSError:
        return []
    if size <= max_bytes:
        return load()

    try:
        with open(EVENTS_PATH, "rb") as fh:
            fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return []

    nl = data.find(b"\n")
    if nl != -1:
        data = data[nl + 1 :]

    out: list[dict] = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "t" in rec and "ev" in rec:
            out.append(rec)
    out.sort(key=lambda r: r.get("t", 0.0))
    return out


def load_since(offset: int) -> tuple[list[dict], int]:
    """Events appended after ``offset`` bytes, and the offset to pass next time.

    The cloud sync walks the log forward rather than re-reading it, so a laptop
    that has been running for a week does not re-upload a week of history every
    minute. Only whole lines are consumed -- the last line may still be
    mid-write by a hook process, and advancing past it would drop that turn.

    An offset past the end of the file means the log was rotated or cleared, so
    reading restarts from the top rather than returning nothing forever.
    """
    try:
        size = os.path.getsize(EVENTS_PATH)
    except OSError:
        return [], 0
    if not isinstance(offset, int) or offset < 0 or offset > size:
        offset = 0
    if offset == size:
        return [], offset

    try:
        with open(EVENTS_PATH, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset

    nl = data.rfind(b"\n")
    if nl == -1:
        return [], offset
    consumed = offset + nl + 1

    out: list[dict] = []
    for line in data[: nl + 1].decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "t" in rec and "ev" in rec:
            out.append(rec)
    out.sort(key=lambda r: r.get("t", 0.0))
    return out, consumed


def _read_offsets() -> dict:
    try:
        with open(OFFSETS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_offsets(data: dict) -> None:
    try:
        _ensure()
        tmp = OFFSETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, OFFSETS_PATH)
    except OSError:
        pass


def tokens_since(sid: str, transcript_path: str) -> tuple[dict, str, int]:
    """Token usage, model and tool-call count in the transcript since last read.

    Reads from the remembered byte offset for this session to the end of the
    file, parses the new assistant messages, and advances the offset to the
    last complete line. Returns (token_dict, model_id, tool_count); zeros when
    the transcript cannot be read.
    """
    tok = _zero_tok()
    if not sid or not transcript_path or not os.path.exists(transcript_path):
        return tok, "", 0

    offsets = _read_offsets()
    start = offsets.get(sid, 0)
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return tok, "", 0
    # A smaller file than our offset means the transcript was rotated or
    # replaced (e.g. --resume into a fresh file); start over from the top.
    if not isinstance(start, int) or start < 0 or start > size:
        start = 0

    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return tok, "", 0

    # Only consume up to the last newline: the final line may still be
    # mid-write, and advancing past it would lose that turn's tokens.
    nl = data.rfind(b"\n")
    if nl == -1:
        return tok, "", 0
    consumed = start + nl + 1
    text = data[: nl + 1].decode("utf-8", "replace")

    model = ""
    tools = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if isinstance(usage, dict):
            tok["in"] += int(usage.get("input_tokens") or 0)
            tok["out"] += int(usage.get("output_tokens") or 0)
            tok["cc"] += int(usage.get("cache_creation_input_tokens") or 0)
            tok["cr"] += int(usage.get("cache_read_input_tokens") or 0)
        model_id = msg.get("model")
        if isinstance(model_id, str) and model_id:
            model = model_id
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools += 1

    offsets[sid] = consumed
    _write_offsets(offsets)
    return tok, model, tools
