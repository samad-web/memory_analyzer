# Architecture

## Data flow

```
Claude Code session
  |  hooks (notify.py <action>)
  v
events.jsonl  (append-only, the record of truth)
  |  store.ingest() -- byte-offset watermark, generation-aware
  v
store.db  (SQLite WAL + FTS5)
  ^                      ^                    ^
  | observe.record()     | goals.sync()       |
  | (Stop hook, parses   | (GOAL.md owns      |
  |  the transcript)     |  structure)        |
  |                      |                    |
  +-- observations       +-- goals            +-- events
      observations_fts       milestones            decisions

store.db is read by four things:
  context.py      -> SessionStart stdout (an agent, at session start)
  mcp_server.py   -> MCP tools over stdio (an agent, mid-turn)
  dashboard.py    -> Qt Memory tab (a human, at the desk)
  webserver.py    -> /api/memory (a human, on a phone)
```

## Module boundaries

| Module | Owns | Never does |
| --- | --- | --- |
| `events.py` | the append-only log, `ROOT` | query, aggregate |
| `store.py` | schema, migration, ingest, every query | parse transcripts, run commands |
| `observe.py` | transcript -> observation, deterministically | call a model, touch the network |
| `goals.py` | GOAL.md <-> goals/milestones, and `verify` | decide status without evidence |
| `context.py` | the SessionStart payload, within a budget | exceed `BUDGET` characters |
| `mcp_server.py` | MCP stdio JSON-RPC over `store`/`goals` | run a check command, write to stdout off-protocol |
| `notify.py` | hook dispatch | raise (it exits 0 on anything) |
| `dashboard.py` / `webserver.py` | rendering | their own queries |

The rule that keeps the two dashboards honest: **one query function, two
renderers.** `store.observations_page` is called by Qt and by HTTP, so they
cannot quietly disagree about what a row means.

## Why SQLite is a projection, not the record

`events.jsonl` can be replayed into an empty database. That is why `_migrate`
is allowed to drop and rebuild `events` wholesale on a schema bump, while
`observations`, `goals`, `milestones` and `decisions` are migrated in place --
those are not derivable from the log alone.

Ingest is keyed `(gen, off)`, not `off`. A byte offset identifies a line only
within one life of a file; after a rotation, offset 400 is a different line, and
with `off` alone as the key new rows silently overwrote unrelated old ones.

## Status is asserted, not inferred

A milestone is done when its `check:` command exits 0. No check means
`unverifiable` -- never "probably done". A failing check on a done milestone
reopens it to `active`, so a regression un-ticks the board. `milestones.file_status`
records what GOAL.md last said, which is what distinguishes "a human unticked
this" from "verify proved it".
