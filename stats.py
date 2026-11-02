r"""Turn the raw event log into the numbers a business owner actually watches.

Four questions, all answered from the same event stream (see ``events.py``):

* **Attentiveness** -- when an agent is blocked on you, how long until you
  answer? Measured from a ``wait`` to the next ``prompt``/``resume`` in that
  session. Median and slowest.
* **Throughput** -- how many turns finish, and how long each takes (the
  ``prompt`` to ``done`` gap).
* **Spend** -- tokens per turn, summed per project and per day. (A dollar
  estimate can be computed from the pricing table below, but the UI shows token
  counts only, by preference.)
* **Effort** -- tool-calls per turn, a rough proxy for how much work a task
  took (not a quality score -- that can't be read from hooks).

Everything is derived; nothing is stored twice. Pairing (wait->reply,
prompt->done) is done per session across the whole history, then each derived
figure is filed under the time of the event that completed it, so a window
filter stays honest across day boundaries.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime

import events

try:
    import agent_alert
except Exception:  # noqa: BLE001 - stats still work without the live mailbox
    agent_alert = None  # type: ignore

# -- pricing ---------------------------------------------------------------
#
# USD per MILLION tokens, by model family. These are ESTIMATES used only to
# turn token counts into a rough dollar figure -- edit them to match the rates
# you actually pay. Matching is by substring of the model id, first hit wins,
# so "claude-opus-4-8" -> opus. Unknown models fall back to "default".
PRICING = {
    "opus":   {"in": 15.0, "out": 75.0, "cc": 18.75, "cr": 1.50},
    "sonnet": {"in": 3.0,  "out": 15.0, "cc": 3.75,  "cr": 0.30},
    "haiku":  {"in": 0.80, "out": 4.0,  "cc": 1.00,  "cr": 0.08},
    "default": {"in": 3.0, "out": 15.0, "cc": 3.75,  "cr": 0.30},
}

# Pairings longer than these are treated as a dead session, not a real reply or
# task, so one forgotten prompt does not wreck the medians.
MAX_REPLY_SECONDS = 24 * 3600
MAX_TASK_SECONDS = 6 * 3600


def price_of(model: str) -> dict:
    m = (model or "").lower()
    if "opus" in m:
        return PRICING["opus"]
    if "haiku" in m:
        return PRICING["haiku"]
    if "sonnet" in m:
        return PRICING["sonnet"]
    return PRICING["default"]


def estimate_cost(tok: dict, model: str) -> float:
    if not tok:
        return 0.0
    rate = price_of(model)
    return sum((tok.get(k, 0) / 1_000_000.0) * rate[k] for k in rate)


# -- formatting ------------------------------------------------------------


def fmt_tokens(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def fmt_cost(c: float) -> str:
    return f"${c:,.2f}"


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "--"
    s = float(s)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _project_key(ev: dict) -> str:
    """A stable grouping key for one project, regardless of how its path was
    spelled between hook firings."""
    cwd = (ev.get("cwd") or "").strip().lower().replace("\\", "/").rstrip("/")
    return cwd or (ev.get("proj") or "agent").lower()


# -- derivation ------------------------------------------------------------


def _pairings(evs: list[dict]):
    """Yield (replies, tasks) across all sessions.

    ``replies`` -- (completed_at, latency_seconds, event) for each time an
    agent was waiting and you answered.
    ``tasks`` -- (done_at, duration_or_None, event) for each finished turn.
    """
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for ev in evs:
        by_sid[ev.get("sid", "")].append(ev)

    replies: list[tuple] = []
    tasks: list[tuple] = []
    for session in by_sid.values():
        session.sort(key=lambda e: e.get("t", 0.0))
        pending_wait: float | None = None
        last_prompt: float | None = None
        for ev in session:
            kind = ev.get("ev")
            t = float(ev.get("t", 0.0))
            if kind == "wait":
                pending_wait = t
            elif kind in ("prompt", "resume"):
                if pending_wait is not None:
                    gap = t - pending_wait
                    if 0 <= gap <= MAX_REPLY_SECONDS:
                        replies.append((t, gap, ev))
                    pending_wait = None
                if kind == "prompt":
                    last_prompt = t
            elif kind == "done":
                # A turn ending also clears a block (e.g. a permission answered
                # without a typed prompt), so count it as a reply too.
                if pending_wait is not None:
                    gap = t - pending_wait
                    if 0 <= gap <= MAX_REPLY_SECONDS:
                        replies.append((t, gap, ev))
                    pending_wait = None
                duration = None
                if last_prompt is not None and 0 <= (t - last_prompt) <= MAX_TASK_SECONDS:
                    duration = t - last_prompt
                last_prompt = None
                tasks.append((t, duration, ev))
            elif kind == "end":
                pending_wait = None
                last_prompt = None
    return replies, tasks


def summarize(evs: list[dict], start: float, end: float | None = None) -> dict:
    """Aggregate every metric for events whose completing time is in the window.

    Token spend and tool counts are summed over turn-ending events; reply and
    task times come from the cross-session pairings, filed under the time they
    completed.
    """
    end = end if end is not None else time.time()

    def in_window(t: float) -> bool:
        return start <= t <= end

    totals = {
        "tasks": 0,
        "tok": {k: 0 for k in events.TOK_KEYS},
        "tokens": 0,
        "cost": 0.0,
        "tools": 0,
    }
    projects: dict[str, dict] = {}

    def project(ev: dict) -> dict:
        key = _project_key(ev)
        p = projects.get(key)
        if p is None:
            p = {
                "name": ev.get("proj") or "agent",
                "tasks": 0,
                "tokens": 0,
                "cost": 0.0,
                "tools": 0,
                "replies": [],
                "durations": [],
            }
            projects[key] = p
        # Prefer a non-empty, human name if a later event has one.
        if ev.get("proj"):
            p["name"] = ev["proj"]
        return p

    for ev in evs:
        if not in_window(float(ev.get("t", 0.0))):
            continue
        p = project(ev)
        if ev.get("ev") == "done":
            totals["tasks"] += 1
            p["tasks"] += 1
            totals["tools"] += int(ev.get("tools", 0))
            p["tools"] += int(ev.get("tools", 0))
        tok = ev.get("tok")
        if isinstance(tok, dict):
            for k in events.TOK_KEYS:
                totals["tok"][k] += int(tok.get(k, 0))
                totals["tokens"] += int(tok.get(k, 0))
                p["tokens"] += int(tok.get(k, 0))
            cost = estimate_cost(tok, ev.get("model", ""))
            totals["cost"] += cost
            p["cost"] += cost

    replies, tasks = _pairings(evs)
    reply_times = [lat for (t, lat, ev) in replies if in_window(t)]
    task_times = [d for (t, d, ev) in tasks if d is not None and in_window(t)]

    for t, lat, ev in replies:
        if in_window(t):
            project(ev)["replies"].append(lat)
    for t, d, ev in tasks:
        if d is not None and in_window(t):
            project(ev)["durations"].append(d)

    totals["reply_median"] = _median(reply_times)
    totals["reply_max"] = max(reply_times) if reply_times else None
    totals["reply_count"] = len(reply_times)
    totals["task_median"] = _median(task_times)
    totals["tools_per_task"] = (
        totals["tools"] / totals["tasks"] if totals["tasks"] else 0.0
    )
    totals["projects"] = len(projects)

    project_rows = []
    for p in projects.values():
        project_rows.append(
            {
                "name": p["name"],
                "tasks": p["tasks"],
                "tokens": p["tokens"],
                "cost": p["cost"],
                "tools": p["tools"],
                "reply_median": _median(p["replies"]),
                "task_median": _median(p["durations"]),
            }
        )
    project_rows.sort(key=lambda r: r["tokens"], reverse=True)
    totals["project_rows"] = project_rows
    return totals


# -- task records ----------------------------------------------------------
#
# ``summarize`` above answers "how did the window go" straight from the event
# stream. The dashboard also needs the opposite: one row per task, with the
# prompt it started from, so you can read what was actually done and not just
# how much. That is what ``task_rows`` builds.
#
# The row shape is deliberately the same one the cloud ``tasks`` table stores,
# so the dashboard runs identical aggregation over your own log and over a
# teammate's synced rows -- ``summarize_tasks`` does not know or care which it
# was handed. The walk below therefore duplicates a little of ``_pairings``:
# that one yields measurements, this one yields records, and collapsing them
# would make both harder to read.

TASK_FIELDS = (
    "task_key", "sid", "project", "cwd", "prompt",
    "started_at", "ended_at", "duration_s",
    "blocked_s", "waits", "max_reply_s",
    "tokens", "tok", "model", "tools", "cost",
)


def task_rows(
    evs: list[dict],
    start: float = 0.0,
    end: float | None = None,
    include_prompts: bool = True,
) -> list[dict]:
    """One record per finished turn, newest first.

    Filed under the time the turn *ended*, so the window filter matches what
    ``summarize`` reports for the same window. A turn with token usage but no
    matching prompt (the tail of a session that was already running when the
    hooks were installed, or a ``SessionEnd`` sweep) still produces a row with
    an empty prompt, so the token totals stay whole.
    """
    end = end if end is not None else time.time()

    by_sid: dict[str, list[dict]] = defaultdict(list)
    for ev in evs:
        by_sid[ev.get("sid", "")].append(ev)

    rows: list[dict] = []
    for session in by_sid.values():
        session.sort(key=lambda e: e.get("t", 0.0))
        prompt_at: float | None = None
        prompt_msg = ""
        pending_wait: float | None = None
        blocked = 0.0
        waits = 0
        worst = 0.0
        # A block that ended before the next prompt belongs to no open task;
        # it is carried onto the task that prompt starts rather than dropped.
        carried = 0.0
        carried_n = 0
        carried_worst = 0.0

        def take_wait(at: float, now: float) -> None:
            nonlocal blocked, waits, worst, carried, carried_n, carried_worst
            gap = now - at
            if not 0 <= gap <= MAX_REPLY_SECONDS:
                return
            if prompt_at is not None:
                blocked += gap
                waits += 1
                worst = max(worst, gap)
            else:
                carried += gap
                carried_n += 1
                carried_worst = max(carried_worst, gap)

        for ev in session:
            kind = ev.get("ev")
            t = float(ev.get("t", 0.0))

            if kind == "wait":
                pending_wait = t
                continue

            if pending_wait is not None and kind in ("prompt", "resume", "done"):
                take_wait(pending_wait, t)
                pending_wait = None

            if kind == "prompt":
                prompt_at = t
                prompt_msg = str(ev.get("msg") or "") if include_prompts else ""
                blocked, waits, worst = carried, carried_n, carried_worst
                carried, carried_n, carried_worst = 0.0, 0, 0.0
            elif kind in ("done", "end"):
                tok = ev.get("tok") if isinstance(ev.get("tok"), dict) else {}
                carries_tokens = any(tok.get(k) for k in events.TOK_KEYS)
                # A SessionEnd with nothing to report is bookkeeping, not a
                # task -- it would otherwise show as an empty row per session.
                if kind == "end" and not carries_tokens:
                    prompt_at, prompt_msg = None, ""
                    blocked, waits, worst = 0.0, 0, 0.0
                    continue
                duration = None
                if prompt_at is not None and 0 <= (t - prompt_at) <= MAX_TASK_SECONDS:
                    duration = t - prompt_at
                model = str(ev.get("model") or "")
                total = sum(int(tok.get(k, 0)) for k in events.TOK_KEYS)
                rows.append(
                    {
                        # Stable across re-reads of the log, so re-syncing a
                        # batch cannot create a second copy of the same task.
                        "task_key": f"{ev.get('sid', '')}-{int(t * 1000)}",
                        "sid": str(ev.get("sid") or ""),
                        "project": str(ev.get("proj") or "agent"),
                        "cwd": str(ev.get("cwd") or ""),
                        "prompt": prompt_msg,
                        "started_at": prompt_at,
                        "ended_at": t,
                        "duration_s": duration,
                        "blocked_s": round(blocked, 2),
                        "waits": waits,
                        "max_reply_s": round(worst, 2) if worst else None,
                        "tokens": total,
                        "tok": {k: int(tok.get(k, 0)) for k in events.TOK_KEYS},
                        "model": model,
                        "tools": int(ev.get("tools", 0)),
                        "cost": estimate_cost(tok, model),
                    }
                )
                prompt_at, prompt_msg = None, ""
                blocked, waits, worst = 0.0, 0, 0.0

    rows = [r for r in rows if start <= r["ended_at"] <= end]
    rows.sort(key=lambda r: r["ended_at"], reverse=True)
    return rows


def summarize_tasks(rows: list[dict]) -> dict:
    """Headline figures over a list of task records, local or synced.

    Reply time is reported as a mean and a worst case rather than a median:
    a task record carries the total time it sat blocked and the worst single
    block, not every individual gap, which is what keeps a synced row small.
    ``summarize`` still gives the exact median for your own log.
    """
    totals = {
        "tasks": len(rows),
        "tokens": 0,
        "cost": 0.0,
        "tools": 0,
        "tok": {k: 0 for k in events.TOK_KEYS},
        "blocked_s": 0.0,
        "waits": 0,
    }
    durations: list[float] = []
    worst_reply = 0.0
    by_project: dict[str, dict] = {}
    by_member: dict[str, dict] = {}
    by_model: dict[str, int] = defaultdict(int)

    def bucket(store: dict, key: str, name: str) -> dict:
        row = store.get(key)
        if row is None:
            row = {
                "name": name,
                "tasks": 0,
                "tokens": 0,
                "cost": 0.0,
                "tools": 0,
                "blocked_s": 0.0,
                "waits": 0,
                "durations": [],
                "last_at": 0.0,
            }
            store[key] = row
        return row

    for r in rows:
        tokens = int(r.get("tokens") or 0)
        cost = float(r.get("cost") or 0.0)
        tools = int(r.get("tools") or 0)
        blocked = float(r.get("blocked_s") or 0.0)
        waits = int(r.get("waits") or 0)

        totals["tokens"] += tokens
        totals["cost"] += cost
        totals["tools"] += tools
        totals["blocked_s"] += blocked
        totals["waits"] += waits
        tok = r.get("tok")
        if isinstance(tok, dict):
            for k in events.TOK_KEYS:
                totals["tok"][k] += int(tok.get(k, 0) or 0)

        if r.get("duration_s") is not None:
            durations.append(float(r["duration_s"]))
        if r.get("max_reply_s"):
            worst_reply = max(worst_reply, float(r["max_reply_s"]))

        family = model_family(r.get("model", ""))
        by_model[family] += tokens

        pkey = (r.get("cwd") or r.get("project") or "agent").strip().lower()
        p = bucket(by_project, pkey, str(r.get("project") or "agent"))
        m = bucket(by_member, str(r.get("member") or "me"), str(r.get("member") or "me"))
        for row in (p, m):
            row["tasks"] += 1
            row["tokens"] += tokens
            row["cost"] += cost
            row["tools"] += tools
            row["blocked_s"] += blocked
            row["waits"] += waits
            row["last_at"] = max(row["last_at"], float(r.get("ended_at") or 0.0))
            if r.get("duration_s") is not None:
                row["durations"].append(float(r["duration_s"]))

    totals["task_median"] = _median(durations)
    totals["reply_mean"] = (
        totals["blocked_s"] / totals["waits"] if totals["waits"] else None
    )
    totals["reply_max"] = worst_reply or None
    totals["tools_per_task"] = (
        totals["tools"] / totals["tasks"] if totals["tasks"] else 0.0
    )
    totals["projects"] = len(by_project)
    totals["members"] = len(by_member)
    totals["project_rows"] = _finish_buckets(by_project)
    totals["member_rows"] = _finish_buckets(by_member)
    totals["model_rows"] = sorted(
        ({"name": k, "tokens": v} for k, v in by_model.items()),
        key=lambda r: r["tokens"],
        reverse=True,
    )
    return totals


def _finish_buckets(store: dict) -> list[dict]:
    out = []
    for row in store.values():
        out.append(
            {
                "name": row["name"],
                "tasks": row["tasks"],
                "tokens": row["tokens"],
                "cost": row["cost"],
                "tools": row["tools"],
                "task_median": _median(row["durations"]),
                "reply_mean": (
                    row["blocked_s"] / row["waits"] if row["waits"] else None
                ),
                "last_at": row["last_at"],
            }
        )
    out.sort(key=lambda r: r["tokens"], reverse=True)
    return out


def model_family(model: str) -> str:
    """The family a model id belongs to, for grouping and colouring.

    Matched on substring and deliberately coarse: the dashboard wants to say
    "most of this was Opus", not to distinguish two Opus point releases, and a
    chart with a slice per exact model id would be unreadable within a month of
    model launches.
    """
    m = (model or "").lower()
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in m:
            return name
    return "other" if m else "unknown"


# Older name, kept so nothing that reached for it breaks.
_model_family = model_family


def daily_series(rows: list[dict], days: int) -> list[dict]:
    """Per-day totals for the trend chart, oldest first, gaps filled with zero.

    Empty days are emitted rather than skipped: a chart that silently drops
    quiet days compresses the x-axis and makes a weekend look like a slowdown.
    """
    from datetime import date, timedelta

    span = max(1, days or 1)
    today = date.today()
    buckets: dict[str, dict] = {}
    for offset in range(span - 1, -1, -1):
        day = today - timedelta(days=offset)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "label": day.strftime("%d %b"),
            "tasks": 0,
            "tokens": 0,
            "cost": 0.0,
        }

    for r in rows:
        try:
            key = datetime.fromtimestamp(float(r.get("ended_at") or 0.0)).date().isoformat()
        except (OSError, OverflowError, ValueError):
            continue
        slot = buckets.get(key)
        if slot is None:
            continue
        slot["tasks"] += 1
        slot["tokens"] += int(r.get("tokens") or 0)
        slot["cost"] += float(r.get("cost") or 0.0)

    return list(buckets.values())


def hourly_series(rows: list[dict]) -> list[dict]:
    """Tokens per hour of the day across ``rows`` -- when the work happens."""
    buckets = [
        {"hour": h, "label": f"{h:02d}", "tasks": 0, "tokens": 0} for h in range(24)
    ]
    for r in rows:
        try:
            hour = datetime.fromtimestamp(float(r.get("ended_at") or 0.0)).hour
        except (OSError, OverflowError, ValueError):
            continue
        buckets[hour]["tasks"] += 1
        buckets[hour]["tokens"] += int(r.get("tokens") or 0)
    return buckets


# -- windows ---------------------------------------------------------------


def _today_start() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _window_start(period_days: int) -> float:
    if period_days <= 0:
        return _today_start()
    # Whole days back from midnight, not a rolling 7x24 hours: "last 7 days"
    # that moves its own left edge every minute makes a daily chart's first
    # bar grow and shrink as you watch it.
    from datetime import timedelta

    midnight = datetime.fromtimestamp(_today_start())
    return (midnight - timedelta(days=period_days - 1)).timestamp()


def window_start(period_days: int) -> float:
    """Start of the named window: 0 means today, N means the last N days."""
    return _window_start(period_days)


def today_summary() -> dict:
    """Compact figures for the tile's one-line summary."""
    data = summarize(events.load(), _today_start())
    reply = data["reply_median"]
    return {
        "tasks": data["tasks"],
        "tokens": data["tokens"],
        "reply": reply,
        "tokens_h": fmt_tokens(data["tokens"]),
        "reply_h": fmt_duration(reply),
    }


def _live_counts() -> tuple[int, int]:
    """(waiting_now, working_now) from the live mailbox, for the header."""
    if agent_alert is None:
        return 0, 0
    try:
        alerts = agent_alert.read_all(reap=False)
    except Exception:  # noqa: BLE001
        return 0, 0
    waiting = sum(1 for a in alerts if a.needs_input)
    working = sum(1 for a in alerts if a.kind == agent_alert.WORKING)
    return waiting, working


# -- HTML report for the Scorecard window ----------------------------------

_PERIOD_LABEL = {0: "Today", 7: "Last 7 days", 30: "Last 30 days"}


def report_html(period_days: int) -> str:
    data = summarize(events.load(), _window_start(period_days))
    waiting, working = _live_counts()
    label = _PERIOD_LABEL.get(period_days, f"Last {period_days} days")

    def tile(value: str, caption: str, accent: str = "#e6eaf2") -> str:
        return (
            f'<td style="padding:10px 14px;background:#141720;border-radius:10px;">'
            f'<div style="font:600 20px Consolas,monospace;color:{accent};">{value}</div>'
            f'<div style="font:11px Segoe UI,sans-serif;color:#7b8498;'
            f'margin-top:2px;">{caption}</div></td>'
        )

    gap = '<td style="width:8px;"></td>'
    row1 = gap.join([
        tile(str(data["tasks"]), "tasks finished", "#4ade80"),
        tile(fmt_tokens(data["tokens"]), "tokens used", "#a78bfa"),
        tile(fmt_duration(data["reply_median"]), "median reply", "#4cc2ff"),
    ])
    row2 = gap.join([
        tile(fmt_duration(data["reply_max"]), "slowest reply", "#4cc2ff"),
        tile(fmt_duration(data["task_median"]), "median task time", "#e6eaf2"),
        tile(f'{data["tools_per_task"]:.1f}', "tool-calls / task"),
    ])
    row3 = gap.join([
        tile(str(data["projects"]), "active projects"),
        tile(
            f'{waiting} / {working}',
            "waiting / working now",
            "#ff9500" if waiting else "#7b8498",
        ),
    ])

    rows = ""
    for p in data["project_rows"]:
        rows += (
            "<tr>"
            f'<td style="padding:6px 8px;color:#e6eaf2;">{_esc(p["name"])}</td>'
            f'<td style="padding:6px 8px;text-align:right;color:#cdd3df;">{p["tasks"]}</td>'
            f'<td style="padding:6px 8px;text-align:right;color:#a78bfa;">{fmt_tokens(p["tokens"])}</td>'
            f'<td style="padding:6px 8px;text-align:right;color:#4cc2ff;">{fmt_duration(p["reply_median"])}</td>'
            f'<td style="padding:6px 8px;text-align:right;color:#cdd3df;">{fmt_duration(p["task_median"])}</td>'
            "</tr>"
        )
    if not rows:
        rows = (
            '<tr><td colspan="5" style="padding:16px 8px;color:#7b8498;">'
            "No activity in this window yet. The board fills as agents run.</td></tr>"
        )

    return f"""
    <body style="background:#0e1119;font-family:'Segoe UI',sans-serif;color:#e6eaf2;">
      <div style="font:600 15px 'Segoe UI';margin:2px 0 2px;">Agent Scorecard
        <span style="color:#7b8498;font-weight:400;">&nbsp;&middot;&nbsp;{label}</span>
      </div>
      <table cellspacing="0" style="margin:10px 0;"><tr>{row1}</tr></table>
      <table cellspacing="0" style="margin:10px 0;"><tr>{row2}</tr></table>
      <table cellspacing="0" style="margin:10px 0;"><tr>{row3}</tr></table>

      <div style="font:600 12px 'Segoe UI';color:#9aa3b5;margin:18px 0 6px;">
        BY PROJECT</div>
      <table cellspacing="0" width="100%" style="font:12px 'Segoe UI';">
        <tr style="color:#7b8498;font-size:11px;">
          <td style="padding:4px 8px;">Project</td>
          <td style="padding:4px 8px;text-align:right;">Tasks</td>
          <td style="padding:4px 8px;text-align:right;">Tokens</td>
          <td style="padding:4px 8px;text-align:right;">Reply</td>
          <td style="padding:4px 8px;text-align:right;">Task</td>
        </tr>
        {rows}
      </table>

      <div style="color:#5c6575;font-size:10px;margin-top:18px;line-height:1.5;">
        Reply = how long an agent waited on you before you answered.
        Task = prompt&nbsp;&rarr;&nbsp;finish. Tokens are the total read from the
        session transcript.
      </div>
    </body>
    """


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
