r"""Moves local activity to the team's Supabase project, and the team's back.

Hooks never touch the network. ``notify.py`` writes to the local log and the
local mailbox and exits, which is what keeps a hook at a few milliseconds and
keeps a laptop on a train from stalling the agent it is measuring. This module
is the part that goes online, on its own schedule: inside the widget it runs on
a background thread, and on a reporter-only install it runs as a scheduled task
every minute.

Three streams go up, each with the shape that suits it:

* ``tasks``        -- append-only records of finished turns. Keyed by
                      (device, task_key) and inserted with duplicates ignored,
                      so a retry after a half-delivered batch is harmless and
                      the watermark can afford to overlap.
* ``live_sessions`` -- upserted presence. Only the latest state per session
                      matters, and the dashboard ages rows out rather than
                      trusting a reporter to clean up after itself.
* ``health``        -- upserted, one row per device. Same reasoning.

One stream comes down: everything the team reported inside the window, cached
to disk so the dashboard paints instantly on open and still shows the last
known board when the network is down.

Nothing is sent unless ``team.json`` says so, and each stream has its own
switch. A laptop with no ``team.json`` never opens a socket.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_alert
import cloud
import events
import identity
import stats

STATE_PATH = os.path.join(identity.STATE_DIR, "sync_state.json")
CACHE_PATH = os.path.join(identity.STATE_DIR, "team_cache.json")

# Tasks whose end time is newer than the watermark minus this are re-offered on
# every push. Duplicate inserts are ignored by the primary key, so the overlap
# costs nothing and covers a turn whose ``done`` landed out of order.
PUSH_OVERLAP_SECONDS = 15 * 60

# A batch bigger than this is split, so a laptop that was offline for a week
# does not try to POST a single multi-megabyte body.
BATCH_SIZE = 200

# How far back the dashboard's team view reaches. Pulled rows outside this are
# not cached; the window is a dashboard control, not a sync setting.
DEFAULT_PULL_DAYS = 30

# A live-session row older than this is treated as gone. Reporters upsert every
# sync_seconds, so a couple of missed cycles is the right threshold -- it means
# the laptop slept, closed, or lost its network.
PRESENCE_STALE_SECONDS = 240


# -- local state -----------------------------------------------------------


def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    try:
        os.makedirs(identity.STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def status() -> dict:
    """What the dashboard shows in its sync footer."""
    state = _read_state()
    cfg = identity.team_config()
    return {
        "enrolled": identity.enrolled(),
        "member": identity.member_name(),
        "device_id": identity.device_id(),
        "team_id": str(cfg.get("team_id") or ""),
        "url": str(cfg.get("url") or ""),
        "last_push": float(state.get("last_push") or 0.0),
        "last_pull": float(state.get("last_pull") or 0.0),
        "last_error": str(state.get("last_error") or ""),
        "pushed_total": int(state.get("pushed_total") or 0),
        "shares": {
            k: bool(cfg.get(k)) for k in
            ("share_tasks", "share_sessions", "share_health", "share_prompts")
        },
    }


# -- row building ----------------------------------------------------------


def _task_payload(row: dict, cfg: dict, device_id: str, team_id: str) -> dict:
    """One ``tasks`` row. Cost is deliberately absent: the dashboard applies
    its own pricing table so a rate change reprices history instead of only
    repricing whatever is reported from then on."""
    tok = row.get("tok") or {}
    return {
        "team_id": team_id,
        "device_id": device_id,
        "task_key": row["task_key"],
        "member": identity.member_name(),
        "session_id": row.get("sid") or "",
        "project": row.get("project") or "agent",
        "prompt": (row.get("prompt") or "") if cfg.get("share_prompts") else "",
        "started_at": _iso(row.get("started_at")),
        "ended_at": _iso(row.get("ended_at")),
        "duration_s": row.get("duration_s"),
        "blocked_s": row.get("blocked_s") or 0,
        "waits": row.get("waits") or 0,
        "max_reply_s": row.get("max_reply_s"),
        "tok_in": int(tok.get("in", 0)),
        "tok_out": int(tok.get("out", 0)),
        "tok_cache_create": int(tok.get("cc", 0)),
        "tok_cache_read": int(tok.get("cr", 0)),
        "tokens": int(row.get("tokens") or 0),
        "model": row.get("model") or "",
        "tools": int(row.get("tools") or 0),
    }


def _session_payload(alert, device_id: str, team_id: str, share_prompts: bool) -> dict:
    return {
        "team_id": team_id,
        "device_id": device_id,
        # The mailbox key already separates concurrent sessions of one project,
        # which is exactly the grain this table wants.
        "session_key": alert.key,
        "member": identity.member_name(),
        "project": alert.source,
        "state": alert.kind,
        "message": (alert.message or "") if share_prompts else "",
        "state_since": _iso(alert.at),
        "updated_at": _iso(time.time()),
    }


def health_row(sample=None) -> dict | None:
    """Machine health for this laptop, from a widget sample or read fresh.

    A reporter-only install has no widget and no sampler thread, so it builds
    a reader on the spot. ``psutil.cpu_percent`` needs two reads to mean
    anything, hence the short interval when there is no running sampler to
    borrow a baseline from.
    """
    if sample is None:
        try:
            import sensors

            reader = sensors.SensorReader()
            # First read establishes the CPU baseline; the second is the value.
            reader.read(include_slow=False)
            time.sleep(0.4)
            sample = reader.read(include_slow=True)
        except Exception:  # noqa: BLE001 - health is the optional stream
            try:
                import psutil

                vm = psutil.virtual_memory()
                return {
                    "cpu_pct": psutil.cpu_percent(interval=0.4),
                    "ram_pct": vm.percent,
                    "ram_used_gb": round((vm.total - vm.available) / 1024**3, 2),
                    "ram_total_gb": round(vm.total / 1024**3, 2),
                    "gpu_pct": None,
                    "gpu_temp": None,
                    "cpu_temp": None,
                    "fan_rpm": None,
                }
            except Exception:  # noqa: BLE001
                return None

    fans = getattr(sample, "fan_values", None) or []
    return {
        "cpu_pct": round(float(getattr(sample, "cpu_pct", 0.0) or 0.0), 1),
        "ram_pct": round(float(getattr(sample, "ram_pct", 0.0) or 0.0), 1),
        "ram_used_gb": round(float(getattr(sample, "ram_used_gb", 0.0) or 0.0), 2),
        "ram_total_gb": round(float(getattr(sample, "ram_total_gb", 0.0) or 0.0), 2),
        "gpu_pct": getattr(sample, "gpu_pct", None),
        "gpu_temp": getattr(sample, "gpu_temp", None),
        "cpu_temp": getattr(sample, "cpu_temp", None),
        "fan_rpm": max(fans) if fans else None,
    }


def _iso(ts) -> str | None:
    """Unix seconds as a UTC timestamp Postgres will accept, or None."""
    if ts is None:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _epoch(value) -> float:
    """A Postgres timestamp back to unix seconds, tolerant of what it finds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            from datetime import timezone

            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


# -- push ------------------------------------------------------------------


def push(backend: cloud.Backend | None = None, sample=None) -> dict:
    """Send this laptop's new activity. Returns a short report, never raises."""
    report = {"ok": False, "tasks": 0, "sessions": 0, "health": 0, "error": ""}
    if not identity.enrolled():
        report["error"] = "not enrolled"
        return report

    cfg = identity.team_config()
    backend = backend or cloud.Backend.from_team_config(cfg)
    if not backend.configured:
        report["error"] = "no url or key"
        return report

    device_id = identity.device_id()
    team_id = str(cfg.get("team_id") or "")
    state = _read_state()
    errors: list[str] = []

    # The device row doubles as the heartbeat, so a laptop that reports nothing
    # else still shows up as present rather than vanishing from the board.
    row = identity.device_row()
    row["last_seen"] = _iso(time.time())
    result = backend.upsert("devices", [row], on_conflict="device_id")
    if not result.ok:
        errors.append(f"devices: {result.error}")

    if cfg.get("share_tasks"):
        watermark = float(state.get("last_task_at") or 0.0)
        rows = stats.task_rows(
            events.load_tail(),
            start=max(0.0, watermark - PUSH_OVERLAP_SECONDS),
            include_prompts=bool(cfg.get("share_prompts")),
        )
        payload = [_task_payload(r, cfg, device_id, team_id) for r in rows]
        sent = 0
        highest = watermark
        # Oldest first, so a failure part-way leaves the watermark behind a
        # contiguous run rather than ahead of a gap.
        for batch_start in range(0, len(payload), BATCH_SIZE):
            batch = payload[batch_start : batch_start + BATCH_SIZE]
            ordered = sorted(batch, key=lambda r: r["ended_at"] or "")
            result = backend.insert("tasks", ordered)
            if not result.ok:
                errors.append(f"tasks: {result.error}")
                break
            sent += len(ordered)
            for r in rows[batch_start : batch_start + BATCH_SIZE]:
                highest = max(highest, float(r.get("ended_at") or 0.0))
        report["tasks"] = sent
        if sent:
            state["last_task_at"] = highest
            state["pushed_total"] = int(state.get("pushed_total") or 0) + sent

    if cfg.get("share_sessions"):
        try:
            alerts = agent_alert.read_all(reap=False)
        except Exception:  # noqa: BLE001
            alerts = []
        rows = [
            _session_payload(a, device_id, team_id, bool(cfg.get("share_prompts")))
            for a in alerts
        ]
        if rows:
            result = backend.upsert(
                "live_sessions", rows, on_conflict="device_id,session_key"
            )
            if result.ok:
                report["sessions"] = len(rows)
            else:
                errors.append(f"sessions: {result.error}")

    if cfg.get("share_health"):
        health = health_row(sample)
        if health:
            health.update(
                {
                    "team_id": team_id,
                    "device_id": device_id,
                    "member": identity.member_name(),
                    "sampled_at": _iso(time.time()),
                }
            )
            result = backend.upsert("health", [health], on_conflict="device_id")
            if result.ok:
                report["health"] = 1
            else:
                errors.append(f"health: {result.error}")

    state["last_push"] = time.time()
    state["last_error"] = "; ".join(errors)[:300]
    _write_state(state)
    report["ok"] = not errors
    report["error"] = state["last_error"]
    return report


# -- pull ------------------------------------------------------------------


def pull(
    backend: cloud.Backend | None = None, days: int = DEFAULT_PULL_DAYS
) -> dict:
    """Fetch the team's rows for the window and cache them.

    Requires a signed-in viewer: ``SELECT`` is granted to authenticated users
    only, so the anon key every laptop carries cannot read this back.
    """
    cfg = identity.team_config()
    backend = backend or cloud.Backend.from_team_config(cfg)
    cache = read_cache()
    cache.setdefault("tasks", [])

    if not backend.configured:
        cache["error"] = "not configured"
        return cache
    if not backend.signed_in:
        cache["error"] = "sign in to view the team board"
        return cache

    since = _iso(stats.window_start(days))
    errors: list[str] = []

    tasks = backend.select(
        "tasks",
        filters={"ended_at": f"gte.{since}"},
        order="ended_at.desc",
        limit=20000,
    )
    if tasks.ok:
        cache["tasks"] = [_task_from_cloud(r) for r in (tasks.data or [])]
    else:
        errors.append(f"tasks: {tasks.error}")

    sessions = backend.select("live_sessions", order="updated_at.desc", limit=500)
    if sessions.ok:
        cache["sessions"] = list(sessions.data or [])
    else:
        errors.append(f"sessions: {sessions.error}")

    health = backend.select("health", order="sampled_at.desc", limit=500)
    if health.ok:
        cache["health"] = list(health.data or [])
    else:
        errors.append(f"health: {health.error}")

    devices = backend.select("devices", order="last_seen.desc", limit=500)
    if devices.ok:
        cache["devices"] = list(devices.data or [])
    else:
        errors.append(f"devices: {devices.error}")

    cache["fetched_at"] = time.time()
    cache["window_days"] = days
    cache["error"] = "; ".join(errors)[:300]
    _write_cache(cache)

    state = _read_state()
    state["last_pull"] = cache["fetched_at"]
    if errors:
        state["last_error"] = cache["error"]
    _write_state(state)
    return cache


def _task_from_cloud(row: dict) -> dict:
    """A cloud ``tasks`` row in the shape ``stats.summarize_tasks`` expects.

    Cost is computed here rather than read, so the dashboard's pricing table is
    the only place rates live.
    """
    tok = {
        "in": int(row.get("tok_in") or 0),
        "out": int(row.get("tok_out") or 0),
        "cc": int(row.get("tok_cache_create") or 0),
        "cr": int(row.get("tok_cache_read") or 0),
    }
    model = str(row.get("model") or "")
    return {
        "task_key": str(row.get("task_key") or ""),
        "sid": str(row.get("session_id") or ""),
        "member": str(row.get("member") or "?"),
        "device_id": str(row.get("device_id") or ""),
        "project": str(row.get("project") or "agent"),
        "cwd": "",
        "prompt": str(row.get("prompt") or ""),
        "started_at": _epoch(row.get("started_at")) or None,
        "ended_at": _epoch(row.get("ended_at")),
        "duration_s": row.get("duration_s"),
        "blocked_s": float(row.get("blocked_s") or 0.0),
        "waits": int(row.get("waits") or 0),
        "max_reply_s": row.get("max_reply_s"),
        "tokens": int(row.get("tokens") or sum(tok.values())),
        "tok": tok,
        "model": model,
        "tools": int(row.get("tools") or 0),
        "cost": stats.estimate_cost(tok, model),
    }


def read_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("tasks", [])
            data.setdefault("sessions", [])
            data.setdefault("health", [])
            data.setdefault("devices", [])
            return data
    except (OSError, ValueError):
        pass
    return {
        "fetched_at": 0.0,
        "window_days": DEFAULT_PULL_DAYS,
        "tasks": [],
        "sessions": [],
        "health": [],
        "devices": [],
        "error": "",
    }


def _write_cache(cache: dict) -> None:
    try:
        os.makedirs(identity.STATE_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def live_sessions(cache: dict | None = None) -> list[dict]:
    """Team sessions still believed to be live, newest first.

    Reporters never delete their presence rows -- the anon key they carry is
    insert-and-update only, by design -- so a row is judged by its age instead.
    """
    cache = cache if cache is not None else read_cache()
    now = time.time()
    out = []
    for row in cache.get("sessions", []):
        seen = _epoch(row.get("updated_at"))
        if seen and (now - seen) > PRESENCE_STALE_SECONDS:
            continue
        item = dict(row)
        item["age_s"] = now - _epoch(row.get("state_since")) if row.get("state_since") else None
        item["seen_s"] = now - seen if seen else None
        out.append(item)
    order = {agent_alert.NEEDS_INPUT: 0, agent_alert.DONE: 1, agent_alert.WORKING: 2}
    out.sort(key=lambda r: (order.get(r.get("state"), 3), -(_epoch(r.get("state_since")))))
    return out


def team_devices(cache: dict | None = None) -> list[dict]:
    """One row per laptop, health joined on, newest heartbeat first."""
    cache = cache if cache is not None else read_cache()
    now = time.time()
    health_by_device = {
        str(h.get("device_id")): h for h in cache.get("health", [])
    }
    sessions = live_sessions(cache)
    out = []
    for dev in cache.get("devices", []):
        did = str(dev.get("device_id") or "")
        health = health_by_device.get(did, {})
        mine = [s for s in sessions if str(s.get("device_id")) == did]
        last_seen = _epoch(dev.get("last_seen"))
        out.append(
            {
                "device_id": did,
                "member": str(dev.get("member") or "?"),
                "host": str(dev.get("host") or ""),
                "agent_version": str(dev.get("agent_version") or ""),
                "last_seen": last_seen,
                "offline_s": (now - last_seen) if last_seen else None,
                "online": bool(last_seen and (now - last_seen) <= PRESENCE_STALE_SECONDS),
                "cpu_pct": health.get("cpu_pct"),
                "ram_pct": health.get("ram_pct"),
                "gpu_pct": health.get("gpu_pct"),
                "cpu_temp": health.get("cpu_temp"),
                "gpu_temp": health.get("gpu_temp"),
                "ram_used_gb": health.get("ram_used_gb"),
                "ram_total_gb": health.get("ram_total_gb"),
                "fan_rpm": health.get("fan_rpm"),
                "health_age_s": (now - _epoch(health.get("sampled_at")))
                if health.get("sampled_at")
                else None,
                "working": sum(1 for s in mine if s.get("state") == agent_alert.WORKING),
                "waiting": sum(1 for s in mine if s.get("state") == agent_alert.NEEDS_INPUT),
                "sessions": mine,
            }
        )
    out.sort(key=lambda r: (not r["online"], r["member"].lower()))
    return out


# -- combined --------------------------------------------------------------


def once(sample=None, days: int = DEFAULT_PULL_DAYS, viewer: bool = True) -> dict:
    """One full cycle: report, then refresh the team board if we can read it."""
    report = push(sample=sample)
    if viewer:
        backend = cloud.Backend.from_team_config()
        if backend.configured and backend.signed_in:
            cache = pull(backend, days)
            report["pulled"] = len(cache.get("tasks", []))
    return report


# -- CLI -------------------------------------------------------------------

_USAGE = """usage: python sync.py <command>

  push            report this laptop's new activity
  pull [days]     refresh the cached team board (needs a signed-in viewer)
  once            push, then pull
  daemon          push on the interval in team.json, until killed
  status          what this laptop is enrolled as, and when it last synced
  login EMAIL     sign in as a viewer so the dashboard can read the team board
  logout          forget the viewer session
"""


def main(argv: list[str]) -> int:
    command = (argv[1] if len(argv) > 1 else "status").strip().lower()

    if command == "status":
        for key, value in status().items():
            print(f"{key:14} {value}")
        return 0

    if command == "push":
        print(push())
        return 0

    if command == "pull":
        days = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else DEFAULT_PULL_DAYS
        cache = pull(days=days)
        print(
            f"tasks={len(cache.get('tasks', []))} "
            f"devices={len(cache.get('devices', []))} "
            f"error={cache.get('error', '')}"
        )
        return 0

    if command == "once":
        print(once())
        return 0

    if command == "daemon":
        interval = max(20, int(identity.team_config().get("sync_seconds") or 60))
        while True:
            try:
                push()
            except Exception as exc:  # noqa: BLE001 - a daemon must not die
                _write_state({**_read_state(), "last_error": str(exc)[:300]})
            time.sleep(interval)

    if command == "login":
        import getpass

        email = argv[2] if len(argv) > 2 else input("email: ").strip()
        password = getpass.getpass("password: ")
        backend = cloud.Backend.from_team_config()
        result = backend.sign_in(email, password)
        print("signed in" if result.ok else f"failed: {result.error}")
        return 0 if result.ok else 1

    if command == "logout":
        cloud.Backend.from_team_config().sign_out()
        print("viewer session cleared")
        return 0

    print(_USAGE)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
