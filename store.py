r"""The indexed store behind the activity log.

``events.jsonl`` does not change. Hooks still append one short line and exit --
that is what keeps a hook at a few milliseconds and keeps an offline laptop
from stalling the agent it measures -- and nothing here touches that contract.
This module is the *reader*. It drains the log into SQLite from a watermark, so
a query stops costing a full-file parse, and so the things built on top of it
have somewhere indexed to live.

Three reasons the log alone stopped being enough:

* **Queries got real.** ``stats.load_tail`` reads the last 4 MB and parses it
  per call. That was fine for counters. It is not fine for "every turn that
  touched this file", which is a join.
* **Text search needs an index.** Observations are prose, and prose wants FTS5,
  which the stdlib ``sqlite3`` already ships, so this adds no dependency.
* **Some state is not an event.** A goal, a milestone and its status are
  mutable records, not an append-only stream. They need rows that can be
  updated, which a log cannot give them.

**Idempotency comes from the byte offset.** Each line's offset in the log is the
primary key of ``events``, so re-ingesting from an earlier point lands the same
rows on the same keys instead of double-counting a turn. That lets the watermark
overlap freely -- the same bargain ``sync.py`` strikes upstream with
``(device, task_key)``.

Run it directly:

    python store.py ingest          # drain the log into the database
    python store.py stats           # what is in there
    python store.py search QUERY    # full-text over observations
    python store.py brief [PROJECT] # the orientation blob for SessionStart

The database sits beside the log, in ``%LOCALAPPDATA%\SystemMonitorWidget``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import events

DB_PATH = os.path.join(events.ROOT, "store.db")

SCHEMA_VERSION = 1

# Statuses a milestone moves through. "done" is only ever set from evidence --
# see the note on `check_cmd` in the schema.
PENDING, ACTIVE, DONE, BLOCKED = "pending", "active", "done", "blocked"

# What an observation is about. Borrowed from the vocabulary claude-mem settled
# on, because it turned out to be the right set: enough to filter usefully, few
# enough that classifying is not a judgement call.
KINDS = ("decision", "bugfix", "feature", "refactor", "discovery", "change")

_SCHEMA = """
create table if not exists meta (
  key   text primary key,
  value text not null
);

-- ----------------------------------------------------------------- events --
-- A mirror of events.jsonl, indexed. `off` is the byte offset of the line it
-- came from, which is what makes a re-ingest idempotent.
create table if not exists events (
  off     integer primary key,
  t       real    not null,
  ev      text    not null,
  sid     text    not null default '',
  proj    text    not null default '',
  cwd     text    not null default '',
  tok_in  integer not null default 0,
  tok_out integer not null default 0,
  tok_cc  integer not null default 0,
  tok_cr  integer not null default 0,
  model   text    not null default '',
  tools   integer not null default 0,
  msg     text    not null default ''
);

create index if not exists events_t_idx    on events (t);
create index if not exists events_sid_idx  on events (sid, t);
create index if not exists events_proj_idx on events (proj, t);
create index if not exists events_ev_idx   on events (ev, t);

-- ----------------------------------------------------------- observations --
-- One row per finished turn, carrying what the turn *did* rather than only
-- what it cost. The event log answers "how much"; this answers "what".
--
-- `source` records how the row was made: `extract` is deterministic parsing of
-- the transcript and costs nothing, `llm` is an opt-in summarisation pass, and
-- `manual` is a human note. Keeping all three in one table with a provenance
-- column means retrieval does not care which tier produced a row.
create table if not exists observations (
  id             integer primary key autoincrement,
  sid            text    not null default '',
  proj           text    not null default '',
  cwd            text    not null default '',
  started_at     real,
  ended_at       real    not null,
  kind           text    not null default 'change',
  title          text    not null default '',
  narrative      text    not null default '',
  facts          text    not null default '',
  prompt         text    not null default '',
  files_read     text    not null default '[]',
  files_modified text    not null default '[]',
  commands       text    not null default '[]',
  tools          text    not null default '[]',
  tests          text    not null default '',
  tokens         integer not null default 0,
  model          text    not null default '',
  source         text    not null default 'extract',
  created_at     real    not null
);

create index if not exists obs_proj_idx on observations (proj, ended_at desc);
create index if not exists obs_sid_idx  on observations (sid, ended_at desc);

-- The FTS mirror. `content=` makes it an external-content index, so the prose
-- is not stored twice; the triggers below keep it in step.
create virtual table if not exists observations_fts using fts5 (
  title, narrative, facts, prompt,
  content='observations',
  content_rowid='id'
);

create trigger if not exists obs_ai after insert on observations begin
  insert into observations_fts (rowid, title, narrative, facts, prompt)
  values (new.id, new.title, new.narrative, new.facts, new.prompt);
end;

create trigger if not exists obs_ad after delete on observations begin
  insert into observations_fts (observations_fts, rowid, title, narrative, facts, prompt)
  values ('delete', old.id, old.title, old.narrative, old.facts, old.prompt);
end;

create trigger if not exists obs_au after update on observations begin
  insert into observations_fts (observations_fts, rowid, title, narrative, facts, prompt)
  values ('delete', old.id, old.title, old.narrative, old.facts, old.prompt);
  insert into observations_fts (rowid, title, narrative, facts, prompt)
  values (new.id, new.title, new.narrative, new.facts, new.prompt);
end;

-- ------------------------------------------------------------------ goals --
-- Where a project is *going*, as opposed to what has happened to it. This is
-- the half of memory that reduces how often a human has to say what next.
--
-- Provenance is deliberately split: a human writes `goals` (via a committed
-- GOAL.md), an agent maintains `milestones`. You edit intent; you stop typing
-- the next step.
create table if not exists goals (
  proj        text primary key,
  statement   text not null default '',
  constraints text not null default '',
  source      text not null default 'GOAL.md',
  revised_at  real not null
);

-- `check_cmd` is the point of this table. A milestone whose completion can be
-- *asserted* by running a command is one an unattended run cannot talk itself
-- into having finished. Where a criterion is genuinely unmechanisable,
-- `acceptance` carries the prose and a human confirms it.
create table if not exists milestones (
  id         integer primary key autoincrement,
  proj       text    not null,
  ordinal    integer not null default 0,
  title      text    not null,
  acceptance text    not null default '',
  check_cmd  text    not null default '',
  status     text    not null default 'pending',
  blocked_by text    not null default '',
  evidence   text    not null default '',
  created_at real    not null,
  updated_at real    not null,
  unique (proj, title)
);

create index if not exists ms_proj_idx on milestones (proj, ordinal);

-- Settled choices, so they stop being re-litigated every session. The cheapest
-- possible fix for the most irritating kind of repeated prompt.
create table if not exists decisions (
  id         integer primary key autoincrement,
  proj       text    not null,
  title      text    not null,
  rationale  text    not null default '',
  supersedes integer,
  decided_at real    not null
);

create index if not exists dec_proj_idx on decisions (proj, decided_at desc);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating it and its schema if need be.

    WAL is what lets a reader (the dashboard) and a writer (the ingester) work
    at once without either blocking the other, which matters because the
    dashboard polls.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    # Durability is not worth a disk sync here: every row is derived from
    # events.jsonl, so the worst a crash costs is a re-ingest.
    conn.execute("pragma synchronous=normal")
    conn.executescript(_SCHEMA)
    conn.execute("pragma user_version=%d" % SCHEMA_VERSION)
    conn.commit()
    return conn


def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("select value from meta where key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "insert into meta (key, value) values (?, ?) "
        "on conflict(key) do update set value=excluded.value",
        (key, str(value)),
    )


def ingest(conn: sqlite3.Connection | None = None, path: str = "") -> dict:
    """Drain new lines from the activity log into ``events``.

    Returns ``{"read": n, "inserted": n, "from": off, "to": off}``.

    Offsets are tracked over the raw bytes, so the file is read binary -- text
    mode's newline translation would make the arithmetic lie on Windows. Only
    whole lines are consumed: a torn final line (a hook caught mid-append) is
    left behind the watermark and picked up next run.
    """
    own = conn is None
    conn = conn or connect()
    log_path = path or events.EVENTS_PATH
    try:
        start = int(_meta_get(conn, "ingest_offset", "0") or 0)
        seen_size = int(_meta_get(conn, "ingest_size", "0") or 0)
        try:
            size = os.path.getsize(log_path)
        except OSError:
            return {"read": 0, "inserted": 0, "from": start, "to": start}

        # A log that shrank was rotated or truncated, so the old offset points
        # into different content. Start over rather than ingest nonsense.
        if size < seen_size:
            start = 0

        if size <= start:
            _meta_set(conn, "ingest_size", size)
            conn.commit()
            return {"read": 0, "inserted": 0, "from": start, "to": start}

        with open(log_path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()

        cut = blob.rfind(b"\n")
        if cut < 0:
            return {"read": 0, "inserted": 0, "from": start, "to": start}
        usable, end = blob[: cut + 1], start + cut + 1

        rows: list[tuple] = []
        read = 0
        pos = start
        for raw in usable.split(b"\n"):
            off, pos = pos, pos + len(raw) + 1
            line = raw.strip()
            if not line:
                continue
            read += 1
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                # A torn line is expected now and then; the log's own reader
                # tolerates them too.
                continue
            tok = rec.get("tok") or {}
            rows.append((
                off,
                float(rec.get("t") or 0.0),
                str(rec.get("ev") or ""),
                str(rec.get("sid") or ""),
                str(rec.get("proj") or ""),
                str(rec.get("cwd") or ""),
                int(tok.get("in") or 0),
                int(tok.get("out") or 0),
                int(tok.get("cc") or 0),
                int(tok.get("cr") or 0),
                str(rec.get("model") or ""),
                int(rec.get("tools") or 0),
                str(rec.get("msg") or ""),
            ))

        conn.executemany(
            "insert or replace into events "
            "(off, t, ev, sid, proj, cwd, tok_in, tok_out, tok_cc, tok_cr, "
            " model, tools, msg) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        _meta_set(conn, "ingest_offset", end)
        _meta_set(conn, "ingest_size", size)
        _meta_set(conn, "ingest_at", round(time.time(), 3))
        conn.commit()
        return {"read": read, "inserted": len(rows), "from": start, "to": end}
    finally:
        if own:
            conn.close()


def counts(conn: sqlite3.Connection | None = None) -> dict:
    """Event totals by type, plus what else the database holds."""
    own = conn is None
    conn = conn or connect()
    try:
        out = {
            "events": {
                r["ev"]: r["n"]
                for r in conn.execute(
                    "select ev, count(*) n from events group by ev order by n desc"
                )
            },
            "observations": conn.execute(
                "select count(*) n from observations"
            ).fetchone()["n"],
            "goals": conn.execute("select count(*) n from goals").fetchone()["n"],
            "milestones": conn.execute(
                "select count(*) n from milestones"
            ).fetchone()["n"],
            "projects": conn.execute(
                "select count(distinct proj) n from events where proj<>''"
            ).fetchone()["n"],
            "ingest_offset": int(_meta_get(conn, "ingest_offset", "0") or 0),
        }
        span = conn.execute("select min(t) a, max(t) b from events").fetchone()
        out["span"] = (span["a"], span["b"])
        return out
    finally:
        if own:
            conn.close()


def search(
    query: str,
    proj: str = "",
    limit: int = 20,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Full-text search over observations, best match first.

    FTS5's own ``rank`` is the ordering. A bare user string is not valid FTS
    syntax (a stray quote or ``*`` raises), so it is quoted term by term and
    matched as a set of phrases rather than handed over raw.
    """
    own = conn is None
    conn = conn or connect()
    try:
        terms = [t.replace('"', "") for t in query.split() if t.strip()]
        if not terms:
            return []
        match = " ".join('"%s"' % t for t in terms)
        sql = (
            "select o.* from observations_fts f "
            "join observations o on o.id = f.rowid "
            "where observations_fts match ? "
        )
        args: list = [match]
        if proj:
            sql += "and o.proj = ? "
            args.append(proj)
        sql += "order by rank limit ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        if own:
            conn.close()


def recent_observations(
    proj: str = "",
    limit: int = 20,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """The newest observations, optionally for one project."""
    own = conn is None
    conn = conn or connect()
    try:
        sql = "select * from observations "
        args: list = []
        if proj:
            sql += "where proj = ? "
            args.append(proj)
        sql += "order by ended_at desc limit ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        if own:
            conn.close()


def brief(proj: str, conn: sqlite3.Connection | None = None) -> dict:
    """Everything a session needs to orient itself in one project.

    This is the shape the ``SessionStart`` hook renders: where the project is
    going, which milestone is live, what is already settled, and what happened
    last. Small on purpose -- detail is pulled on demand rather than injected,
    which is cheaper in tokens and ages better.
    """
    own = conn is None
    conn = conn or connect()
    try:
        goal = conn.execute("select * from goals where proj=?", (proj,)).fetchone()
        milestones = [
            dict(r)
            for r in conn.execute(
                "select * from milestones where proj=? order by ordinal, id", (proj,)
            )
        ]
        decisions = [
            dict(r)
            for r in conn.execute(
                "select * from decisions where proj=? "
                "order by decided_at desc limit 8",
                (proj,),
            )
        ]
        return {
            "project": proj,
            "goal": dict(goal) if goal else None,
            "milestones": milestones,
            "active": next((m for m in milestones if m["status"] == ACTIVE), None),
            "next": next((m for m in milestones if m["status"] == PENDING), None),
            "done": sum(1 for m in milestones if m["status"] == DONE),
            "total": len(milestones),
            "blocked": [m for m in milestones if m["status"] == BLOCKED],
            "decisions": decisions,
            "recent": recent_observations(proj, limit=5, conn=conn),
        }
    finally:
        if own:
            conn.close()


_USAGE = """usage: python store.py <command> [args]

  ingest              drain events.jsonl into the database
  stats               show what the database holds
  search QUERY        full-text search over observations
  brief [PROJECT]     the orientation blob for a project
"""


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "").strip().lower()

    if cmd == "ingest":
        r = ingest()
        print(
            "read %d line(s), stored %d, offset %d -> %d"
            % (r["read"], r["inserted"], r["from"], r["to"])
        )
        return 0

    if cmd == "stats":
        c = counts()
        total = sum(c["events"].values())
        print("%s\n" % DB_PATH)
        print("events        %d" % total)
        for k, v in c["events"].items():
            pct = (100.0 * v / total) if total else 0.0
            print("  %-8s %7d  %5.1f%%" % (k, v, pct))
        print("observations  %d" % c["observations"])
        print("goals         %d" % c["goals"])
        print("milestones    %d" % c["milestones"])
        print("projects      %d" % c["projects"])
        a, b = c["span"]
        if a and b:
            fmt = "%Y-%m-%d %H:%M"
            print(
                "span          %s -> %s"
                % (
                    time.strftime(fmt, time.localtime(a)),
                    time.strftime(fmt, time.localtime(b)),
                )
            )
        return 0

    if cmd == "search":
        q = " ".join(argv[2:]).strip()
        if not q:
            print(_USAGE, end="")
            return 2
        rows = search(q)
        if not rows:
            print("no matches")
            return 0
        for r in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(r["ended_at"]))
            label = r["title"] or r["prompt"][:60]
            print("%s  %-20s %s" % (when, r["proj"], label))
        return 0

    if cmd == "brief":
        proj = (argv[2] if len(argv) > 2 else os.path.basename(os.getcwd())).strip()
        print(json.dumps(brief(proj), indent=2, default=str))
        return 0

    print(_USAGE, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
