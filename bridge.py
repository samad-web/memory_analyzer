r"""Push aggregated Claude Code work to SirahOS as WorkLogs.

The tile already records every finished turn: start, end, duration, which
project folder, what the person typed. SirahOS already has a WorkLog table and
a ``POST /api/worklogs`` endpoint. The piece in between -- the bridge -- is
this file.

Grain
-----

**One WorkLog per (IST calendar day, project folder).** The folder is the
``project`` field on a task row, which is the basename of the nearest ``.git``
on that run. Per-turn would mean twenty or more entries a day per person and
would bury the team view; per-week would lose the daily cadence SirahOS
expects. One day, one project, one row -- same shape as a human filling in
``QuickLogDialog`` at the end of the day.

Mapping
-------

The folder name is almost never the SirahOS project name. ``voicetranscriber``
on this laptop is "AuraTel" over there. So config carries a user-maintained
map::

    "sirahos": {
      "api_url": "https://sirahos.example.com",
      "last_signed_in_as": "support@sirahdigital.in",
      "folder_project_map": {
        "voicetranscriber": "clabc123…",
        "WealthOS-Mobile":  "clxyz789…"
      }
    }

An unmapped folder is pushed as "General" (SirahOS falls back there when
``projectId`` is omitted). A re-sync after the user adds a mapping does NOT
re-tag the historical day; the user can delete that day's WorkLog and the
next tick will recreate it under the right project.

Idempotency
-----------

``sirahos_pushes`` carries a UNIQUE on (ist_date, project_dir). Every push
first checks the row; a successful push records the server's id; a retry
sees the row and skips. The watermark can overlap the way ``sync.py`` does
upstream, because the key makes a double-insert harmless.

Scheduling
----------

The bridge runs as a Windows Scheduled Task (``SirahOSBridge``), every 15
minutes while the user is signed in, plus once at logon. It is also callable
on-demand from ``POST /sirahos/push-now`` on the dashboard web server, so a
user who just finished a session can see it land right away.

Nothing here opens a socket if the user has not signed in. ``jwt.json`` living
beside ``config.json`` is the only switch, same bargain the Supabase sync
makes with ``team.json``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import events
import hooks  # noqa: F401 - bundled so `SirahlyzerBridge.exe hooks-install` works
import identity
import notify  # noqa: F401 - bundled so `SirahlyzerBridge.exe notify <action>` works
import observe
import sirahos
import stats
import store

# One WorkLog's note is capped by the server (zod ``max(5000)``). We leave
# room for the header line and a safe multi-byte margin.
NOTES_LIMIT = 4800
PROMPT_LINE_LIMIT = 160

# IST is +05:30, fixed year-round -- the SirahOS server stores "the day you did
# the work" as UTC midnight of the IST calendar day (see work-day.ts).
IST_OFFSET = dt.timedelta(hours=5, minutes=30)

# How many days back a push run considers. A longer window gives an offline
# laptop time to catch up; too long and the push is slow. Fourteen days covers
# a two-week holiday and still runs in well under a second.
LOOKBACK_DAYS = 14

# Where the per-user token is cached. The tile owns these paths; this module
# does not create them (the webserver does, under DPAPI).
JWT_PATH = os.path.join(identity.STATE_DIR, "sirahos.jwt.json")


# -- config --------------------------------------------------------------


def _load_config() -> dict:
    """Read ``config.json`` and return the ``sirahos`` block.

    Missing or malformed -> empty dict, which the caller treats as "not
    configured" and skips the push.

    Opens with ``utf-8-sig`` because a config edited by PowerShell's
    ``Set-Content`` starts with a BOM, and a utf-8-only parser silently
    reads it as not configured -- which is a tedious bug to track down.
    """
    import paths
    cfg_path = paths.CONFIG_PATH
    try:
        with open(cfg_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    block = data.get("sirahos")
    if not isinstance(block, dict):
        return {}
    out = {
        "api_url": str(block.get("api_url") or "").strip(),
        "last_signed_in_as": str(block.get("last_signed_in_as") or ""),
        "folder_project_map": {},
        "share_prompts": bool(block.get("share_prompts", True)),
    }
    fpm = block.get("folder_project_map") or {}
    if isinstance(fpm, dict):
        for k, v in fpm.items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                out["folder_project_map"][k] = v
    return out


def _load_token() -> str:
    """The signed-in user's SirahOS access token, decrypted via DPAPI.

    Running outside Windows (or without ``pywin32``) returns ''. The webserver
    is responsible for writing this file on sign-in; we only read it. A read
    failure is treated as "not signed in" -- the push stays out of the way.
    """
    try:
        with open(JWT_PATH, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    try:
        import win32crypt  # type: ignore
    except ImportError:
        # Dev / non-Windows: fall back to plaintext. The web UI refuses to
        # write a plaintext token outside tests, so this only triggers in CI.
        try:
            data = json.loads(raw.decode("utf-8"))
            return str(data.get("accessToken") or "")
        except (ValueError, UnicodeDecodeError):
            return ""
    try:
        _desc, blob = win32crypt.CryptUnprotectData(raw, None, None, None, 0)
        data = json.loads(blob.decode("utf-8"))
        return str(data.get("accessToken") or "")
    except Exception:  # noqa: BLE001 - any failure means "not signed in"
        return ""


def enrolled() -> bool:
    """Does this laptop have a SirahOS URL AND a cached token?"""
    cfg = _load_config()
    return bool(cfg.get("api_url")) and bool(_load_token())


# -- aggregation ---------------------------------------------------------


def _ist_day_of(ended_at: float) -> str:
    """Which IST calendar day did a turn end on. Returns ``YYYY-MM-DD``.

    Converts the POSIX timestamp to IST wall-clock and reads the date.
    Matches SirahOS ``work-day.ts`` so the date filter the server applies
    sees what the tile logs it as.
    """
    utc = dt.datetime.fromtimestamp(ended_at, tz=dt.timezone.utc)
    ist = utc.astimezone(dt.timezone(IST_OFFSET))
    return ist.strftime("%Y-%m-%d")


def _ist_hhmm(ts: float) -> str:
    utc = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    ist = utc.astimezone(dt.timezone(IST_OFFSET))
    return ist.strftime("%H:%M")


def _lookback_start() -> float:
    """POSIX timestamp of the oldest day we consider on this run."""
    today_ist = dt.datetime.now(dt.timezone(IST_OFFSET)).date()
    start_ist = today_ist - dt.timedelta(days=LOOKBACK_DAYS)
    start_naive = dt.datetime(start_ist.year, start_ist.month, start_ist.day)
    return (start_naive - dt.datetime(1970, 1, 1)).total_seconds() - IST_OFFSET.total_seconds()


def group_by_day_and_project(rows: list[dict]) -> dict:
    """Return ``{(ist_date, folder): aggregate}`` over per-turn rows.

    Only ``ended_at`` is required; ``duration_s`` missing means we fall back
    to counting the turn but not its hours. ``project='agent'`` is the catch-all
    that means "a run outside any .git" and gets its own bucket.
    """
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        ended = float(row.get("ended_at") or 0.0)
        if not ended:
            continue
        folder = str(row.get("project") or "agent").strip() or "agent"
        key = (_ist_day_of(ended), folder)
        bucket = out.setdefault(key, {
            "ist_date": key[0],
            "project_dir": key[1],
            "task_count": 0,
            "duration_s": 0.0,
            "first_started": None,
            "last_ended": 0.0,
            "prompts": [],
            "models": set(),
            "tokens": 0,
        })
        bucket["task_count"] += 1
        dur = row.get("duration_s")
        if isinstance(dur, (int, float)) and dur > 0:
            bucket["duration_s"] += float(dur)
        start = row.get("started_at")
        if isinstance(start, (int, float)) and start > 0:
            if bucket["first_started"] is None or start < bucket["first_started"]:
                bucket["first_started"] = float(start)
        if ended > bucket["last_ended"]:
            bucket["last_ended"] = ended
        # Keep only the human's own words; drop machine turns (task-notification,
        # pasted content, subagent hand-backs) so the WorkLog note is readable.
        prompt = observe.clean_prompt(row.get("prompt") or "")
        if prompt:
            bucket["prompts"].append(prompt)
        model = str(row.get("model") or "")
        if model:
            bucket["models"].add(model)
        bucket["tokens"] += int(row.get("tokens") or 0)
    return out


def build_notes(bucket: dict, share_prompts: bool = True) -> str:
    """One WorkLog's ``notes`` string. Caps at ``NOTES_LIMIT`` characters.

    Shape::

        3 Claude Code turns, 2h 15m total.
        - Added the SirahOS bridge module and wired its scheduled task
        - Fixed the IST day boundary so a 00:30 IST turn lands on the right day
        - Added integration tests for the push watermark
        (and 2 more)

    ``share_prompts`` False (the user turned the sharing toggle off) replaces
    each line with ``(prompt hidden)`` and keeps the count.
    """
    hours = bucket["duration_s"] / 3600.0 if bucket["duration_s"] else 0.0
    mins = int(round(bucket["duration_s"] / 60.0)) if bucket["duration_s"] else 0
    if mins:
        header = "%d Claude Code turn%s, %dh %dm total." % (
            bucket["task_count"],
            "" if bucket["task_count"] == 1 else "s",
            mins // 60,
            mins % 60,
        )
    else:
        header = "%d Claude Code turn%s." % (
            bucket["task_count"], "" if bucket["task_count"] == 1 else "s",
        )

    prompts = bucket["prompts"] if share_prompts else []
    lines: list[str] = []
    extra = 0
    for p in prompts:
        line = p.replace("\r", " ").replace("\n", " ").strip()
        if len(line) > PROMPT_LINE_LIMIT:
            line = line[: PROMPT_LINE_LIMIT - 1] + "…"
        candidate = "- " + line
        projected = len(header) + 1 + sum(len(l) + 1 for l in lines) + len(candidate) + 1
        if projected > NOTES_LIMIT:
            extra = len(prompts) - len(lines)
            break
        lines.append(candidate)
    if not share_prompts and bucket["task_count"]:
        lines.append("- (prompt hidden)")
    if extra:
        lines.append("(and %d more)" % extra)
    body = "\n".join([header] + lines)
    return body[:NOTES_LIMIT]


def _notes_sha(notes: str) -> str:
    return hashlib.sha256(notes.encode("utf-8", errors="replace")).hexdigest()[:16]


# -- push ----------------------------------------------------------------


def push(now: float | None = None) -> dict:
    """One push run. Returns a short report; never raises.

    The report is what the dashboard surfaces, so each failure mode comes
    back as a readable string rather than a traceback.
    """
    report = {
        "ok": False,
        "configured": False,
        "signed_in": False,
        "considered": 0,
        "pushed": 0,
        "skipped_already": 0,
        "skipped_user": 0,
        "errors": [],
    }
    cfg = _load_config()
    base = cfg.get("api_url") or ""
    if not base:
        report["errors"].append("SirahOS URL not configured")
        return report
    report["configured"] = True

    token = _load_token()
    if not token:
        report["errors"].append("not signed in")
        return report
    report["signed_in"] = True

    now = now if now is not None else time.time()
    start = _lookback_start()
    try:
        evs = events.load_tail(8 * 1024 * 1024)
    except Exception as e:  # noqa: BLE001 - log read failure is reportable
        report["errors"].append("could not read event log: %s" % e)
        return report
    rows = stats.task_rows(evs, start=start, end=now, include_prompts=True)
    groups = group_by_day_and_project(rows)
    report["considered"] = len(groups)

    folder_map: dict[str, str] = cfg.get("folder_project_map") or {}
    share_prompts = cfg.get("share_prompts", True)
    today = _ist_day_of(now)

    conn = store.connect()
    try:
        for (ist_date, folder) in sorted(groups.keys()):
            bucket = groups[(ist_date, folder)]
            if ist_date == today:
                # Today is a moving target -- let the next run finalise it so
                # we do not post, then update, then post again.
                continue
            existing = store.sirahos_push_for(ist_date, folder, conn=conn)
            if existing and existing.get("skip"):
                report["skipped_user"] += 1
                continue
            notes = build_notes(bucket, share_prompts=share_prompts)
            sha = _notes_sha(notes)
            if (
                existing
                and existing.get("worklog_id")
                and existing.get("notes_sha") == sha
            ):
                report["skipped_already"] += 1
                continue
            hours = round(bucket["duration_s"] / 3600.0, 2)
            if hours <= 0:
                # A session that opened and closed with no model call does not
                # count as time worked -- push nothing.
                continue
            project_id = folder_map.get(folder, "")
            try:
                wl = sirahos.create_worklog(
                    base, token,
                    date=ist_date,
                    hours=hours,
                    notes=notes,
                    project_id=project_id,
                )
                worklog_id = str(wl.get("id") or "")
                if not worklog_id:
                    raise sirahos.SirahOSError("server returned no id")
                store.sirahos_record_push(
                    ist_date, folder, worklog_id, hours,
                    bucket["task_count"], sha, conn=conn,
                )
                report["pushed"] += 1
            except sirahos.AuthError as e:
                report["errors"].append("auth: %s" % e)
                # Token is dead; stop. Next tick will see no token and skip.
                try:
                    os.unlink(JWT_PATH)
                except OSError:
                    pass
                return report
            except sirahos.SirahOSError as e:
                store.sirahos_record_error(
                    ist_date, folder, str(e), conn=conn,
                )
                report["errors"].append(
                    "%s %s: %s" % (ist_date, folder, e)
                )
    finally:
        conn.close()

    report["ok"] = not report["errors"]
    return report


# -- CLI -----------------------------------------------------------------


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "push").strip().lower()
    if cmd == "push":
        r = push()
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ok"] else 1
    if cmd == "status":
        import paths
        cfg = _load_config()
        try:
            with open(paths.CONFIG_PATH, encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        print(json.dumps({
            "config_path": paths.CONFIG_PATH,
            "config_exists": os.path.exists(paths.CONFIG_PATH),
            "state_dir": identity.STATE_DIR,
            # None = not in config.json yet; the app's built-in default applies.
            "web_enabled": raw.get("web_enabled"),
            "web_port": raw.get("web_port"),
            "web_bind": raw.get("web_bind"),
            "configured": bool(cfg.get("api_url")),
            "api_url": cfg.get("api_url"),
            "signed_in_as": cfg.get("last_signed_in_as"),
            "has_token": bool(_load_token()),
            "mapped_folders": sorted((cfg.get("folder_project_map") or {}).keys()),
        }, indent=2))
        return 0
    if cmd == "notify":
        # Claude Code hook dispatch: `SirahlyzerBridge.exe notify <action>`.
        # notify.main reads action from its own argv[1], so skip our "notify" word.
        return notify.main([sys.argv[0]] + sys.argv[2:])
    if cmd == "hooks-install":
        # Wire the six Claude Code hooks to point at this very exe. Uses
        # sys.executable so the frozen build points at the exe path, not python.
        report = hooks.install(scope="user", exe=sys.executable)
        # Antigravity too, when present; a missing ~/.gemini is reported, not an error.
        report["antigravity"] = hooks.install_agy(exe=sys.executable)
        print(json.dumps(report, indent=2))
        return 0
    if cmd == "hooks-uninstall":
        report = hooks.uninstall(scope="user")
        report["antigravity"] = hooks.uninstall_agy()
        print(json.dumps(report, indent=2))
        return 0
    print("usage: SirahlyzerBridge.exe [push|status|notify <action>|hooks-install|hooks-uninstall]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
