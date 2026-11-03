# SirahOS WorkLog bridge

Make the tile a time-tracking client for SirahOS: every teammate installs a
single `.exe`, signs in with their SirahOS credentials, and their Claude Code
turns show up in SirahOS WorkLogs tagged to the right project. The web
dashboard inside SirahOS stays the source of truth for cost/billing.

## Purpose

Right now the team logs work by hand in SirahOS (`WorkLogs.tsx` → `QuickLogDialog`).
That hand-entry decides project cost. Memory analyzer already captures *when*
each turn ran, *for how long*, *against which folder*, and *what the person
asked* — this is strictly a superset of what WorkLog wants, except for the
SirahOS user identity and the SirahOS `projectId`. The bridge supplies those
two and pushes aggregated daily rollups to `POST /api/worklogs`.

## Field alignment

SirahOS `WorkLog` (from `server/src/routes/worklogs.ts` + `prisma/schema.prisma`):

| Field | Required | Source in memory_analyzer |
| --- | --- | --- |
| `notes` | one of notes/task/hours | Concatenated prompts for the day, truncated to 5000; or the user's own text if they edit before submit |
| `hours` | one of hours/startTime+endTime | `sum(duration_s)/3600` for the day, per project |
| `startTime` | optional | First turn's start time (HH:MM, IST) if the user picks "span", otherwise omitted |
| `endTime` | optional | Last turn's end time (HH:MM, IST) if "span", otherwise omitted |
| `date` | optional (defaults today) | `ended_at` of each turn, bucketed by IST calendar day |
| `projectId` | optional | Resolved via a **folder → SirahOS projectId mapping table** the user maintains in the tile. `project='agent'` or an unmapped folder → omit (lands in General) |
| `taskId` | optional | Resolved via a **milestone → SirahOS taskId mapping** when GOAL.md milestones align; otherwise omit |
| `newTaskTitle` | optional | Not used — the tile does not create tasks on the user's behalf |
| `taskStatus` | optional | Set to `DONE` when the mapped milestone is checked off, otherwise omit |

Fields memory_analyzer has that SirahOS does not want: `blocked_s`, `waits`,
`max_reply_s`, `tok_*`, `model`, `tools`. These stay in the local store; the
dashboard keeps using them. The bridge only sends what SirahOS asked for.

## Architecture

```
+------------------+   stdout append    +----------------+
|  Claude hooks    | -----------------> |  events.jsonl  |
+------------------+                    +----------------+
                                               |
                                               v
                                        +----------------+
                                        |   store.db     |  (tasks, obs)
                                        +----------------+
                                               |
                           read + aggregate    |
                                               v
                                       +------------------+
                                       |  bridge job      |  new file: bridge.py
                                       |  (scheduled)     |
                                       +------------------+
                                               |
                                               | HTTPS POST /api/worklogs
                                               | Bearer <SirahOS token>
                                               v
                                       +------------------+
                                       |  SirahOS server  |
                                       +------------------+
```

New files:

- `bridge.py` — reads `store.tasks` since the last watermark, groups by `(ist_date, project)`, posts one WorkLog per group.
- `sirahos.py` — thin HTTPS client (stdlib `urllib.request`): `login(email, password) -> token`, `me() -> user`, `list_projects()`, `create_worklog(…)`. No `requests` dependency (constraint: no new runtime deps).
- `web/signin.html` — small page served by `webserver.py` for the user to type their SirahOS email+password once; the resulting JWT is stored encrypted at rest alongside the existing config.
- A tab inside the tile dashboard: **"SirahOS mapping"** — a list of discovered folder names on the left, a dropdown of SirahOS projects on the right, saved to `config.json` under `sirahos.folder_project_map`.

New schema (added to `schema.sql`, applied by `store.ensure_schema`):

```sql
create table if not exists sirahos_pushes (
  push_id      integer primary key autoincrement,
  ist_date     text not null,         -- YYYY-MM-DD
  project_dir  text not null,         -- the memory_analyzer project folder name
  worklog_id   text not null,         -- id returned by SirahOS
  hours        real not null,
  task_count   integer not null,
  pushed_at    text not null default (datetime('now')),
  unique (ist_date, project_dir)      -- one WorkLog per (day, project)
);
```

That UNIQUE is the watermark. On retry we see the row exists and skip; on edit
(hours changed because another late turn landed) we `UPDATE` instead of `POST`
again by deleting+recreating only when the user explicitly re-syncs the day.

### Auth

- The user signs in once from the tile's web UI. We call `POST /api/auth/login`
  with their email+password, store the returned JWT in Windows DPAPI
  (`win32crypt.CryptProtectData`) so only this Windows user can decrypt it.
- The JWT is sent as `Authorization: Bearer <jwt>` on every WorkLog push.
- If the JWT expires or is revoked, the next push gets 401; the tile shows a
  "Sign back in to SirahOS" banner. No silent retry — the user re-authenticates.
- The SirahOS password is NEVER stored. Only the resulting JWT, and only via DPAPI.

### Aggregation rules

- Bucket by **IST calendar day** (SirahOS stores date at UTC-midnight of the IST
  day, matching the server's `workDateOf`). A turn that started 23:30 IST and
  ended 00:30 IST counts as its *end* date (matches how a human would log it).
- One WorkLog per `(date, project_dir)`. Multiple folders worked on the same day
  → multiple WorkLogs.
- `notes` is auto-generated as:
  ```
  <N> Claude Code turn(s), <H>h total.
    - <first prompt, truncated to 120 chars>
    - <next prompt…>
    (…and <X> more)
  ```
  Capped at 5000 chars (SirahOS limit). Prompts that are empty (user has
  `share_prompts` off) show as `(prompt hidden)`.
- Hours rounded to 2 decimals, matching `QuickLogDialog`.
- `startTime`/`endTime` only sent if the user flips a per-folder toggle
  **"Log as a time window"**; default is `hours` only, which matches how
  teammates typically log.

### Scheduling

The push job runs on a Windows Scheduled Task, same pattern as the existing
`AgentMonitorSync`:

- Trigger: every 15 minutes while the user is signed in.
- Also runs once at logon (catches overnight turns).
- Also runs on-demand from the tile's "Push to SirahOS" button.

A new scheduled task **`SirahOSBridge`** is created by `install.ps1` (new
parameter `-SirahOS` and a URL to the SirahOS API; absent → bridge is not
installed, backwards compatible with existing reporter/full installs).

## Impact map

Touches:

- `schema.sql` — adds `sirahos_pushes` table. Existing tables unchanged.
- `store.py` — adds helpers `last_push_for(date, project)` and `record_push(...)`.
- `webserver.py` — adds `/sirahos/signin`, `/sirahos/me`, `/sirahos/map`, `/sirahos/push-now` endpoints, all gated by the existing `operate` scope token.
- `install/install.ps1` — adds optional `-SirahOS -SirahOSUrl` and creates the `SirahOSBridge` scheduled task.
- `config.json` schema — new `sirahos` block (`api_url`, `folder_project_map`, `last_signed_in_as`).
- Memory analyzer dashboard (`web/`) — new tab.

Does NOT touch:

- SirahOS server. We use the existing `POST /api/worklogs` endpoint unchanged.
- The telemetry sync path (`sync.py`). It keeps pushing to Supabase as before. Supabase and SirahOS are independent sinks.
- `hooks.py`. Hooks still only append locally. The bridge is a scheduled job.

What could break:

- **SirahOS auth change** (JWT format, login route path). Mitigation: a single `sirahos.py` to update; the rest of the code is behind it.
- **SirahOS `WorkLog` schema change** (new required field). Mitigation: zod errors come back as HTTP 400 with a readable message; we surface them in the tile tray with a "see what SirahOS wants" link.
- **Rate limiting on the SirahOS API**. Mitigation: at most one request per `(date, project)` per push, and pushes are every 15 min.
- **A project folder gets renamed on disk**. The mapping key is the folder basename. If someone renames `WealthOS-Mobile` to `wealthos-mobile`, their map entry breaks and new turns land as General. Mitigation: when the tile sees an unmapped folder for ≥ 2 days, nag the user to map it.

## Edge cases and failure modes

- Offline laptop: push fails, the row is not in `sirahos_pushes`, next tick retries. Hours never double-count because the UNIQUE constraint catches a second insert.
- User logs a WorkLog in SirahOS by hand for the same day: it coexists with the bridge's entry. We never overwrite or delete entries the user made themselves — a safeguard: the bridge only touches WorkLogs whose `id` is in `sirahos_pushes`.
- User deletes the bridge's WorkLog in SirahOS: next tick notices the row is gone from `/api/worklogs?userId=&date=…` and re-creates it. If the user wants to suppress a day, they instead toggle **"Don't push this day"** in the tile (a `skip` row in `sirahos_pushes`).
- Clock skew between the laptop and the server > 5 minutes: SirahOS accepts any `date` the client provides, so this is fine for date bucketing. JWT exp validation is server-side and will reject stale tokens; the user re-signs in.
- JWT theft: a stolen JWT grants exactly what that user can do in SirahOS. No worse than a stolen SirahOS browser session. DPAPI binds it to the Windows user.
- Prompt leakage: a user's `share_prompts` setting (already enforced by `sync.py` for Supabase) also gates what goes into WorkLog `notes`. Off → notes say `(prompt hidden)` plus the turn count. On → prompts up to 120 chars each.

## Acceptance criteria

1. A teammate runs the `.exe` installer, signs in once with their SirahOS credentials, maps their `WealthOS-Mobile` folder to the SirahOS "WealthOS Mobile" project, works for an hour across three Claude turns, and 15 minutes later sees a single WorkLog in SirahOS WorkLogs — tagged to that project, with the right hours, with their name on it.
2. The same WorkLog shows up under the "Projects" grouping on the Team tab for admins.
3. Deleting the WorkLog in SirahOS and waiting 15 minutes re-creates it. Toggling "Don't push this day" in the tile and deleting again leaves it gone.
4. Signing out of the tile wipes the JWT and the next push does nothing until the user signs in again.
5. Uninstalling the tile removes the scheduled task, the DPAPI blob, and the mapping — nothing left on disk under `%LOCALAPPDATA%\SystemMonitorWidget`.
6. No new runtime dependency in `requirements.txt`.

## Open questions / decisions needed from the user

See `build-docs/PLAN.md` → **Open questions on the SirahOS bridge**.
