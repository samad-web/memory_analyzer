# SirahOS API spec — what the Memory Analyzer bridge calls

**Audience:** engineer building (or verifying) the SirahOS backend.
**Client:** `bridge.py` + `sirahos.py` in this repo. The client is stdlib
`urllib`, TLS 1.2 minimum, 10s connect / 30s read, `User-Agent:
memory-analyzer-bridge/1`, `Accept: application/json`. No retries on the
client side — the next 15-minute tick retries.

All endpoints are relative to `api_url` in the user's `config.json`
(live: `https://api.os.sirahagents.com`). The client trims a trailing
`/api` if the user typed one, so both base shapes work.

**Verified against the deployed API, 2026-11-14 (commit `7e6e087`).** The
three gaps the SirahOS team flagged on review are now fixed server-side:
`hours: 0` returns 400 (was 201 when notes were present), `userId=me`
resolves to the caller (was matched as a literal id), and
`/api/projects/selectable` returns `status`. One policy left open: a POST
with a `projectId` the user isn't a member of currently succeeds rather
than 403'ing — fine for now, this spec marks `projectId` optional.

Note on DELETE 404: the API also returns 404 (not 403) when the id belongs
to another company's user, so the client treats "404 or 403" as
already-gone.

---

## Shared conventions

- **Auth:** `Authorization: Bearer <accessToken>` on every endpoint except login.
- **Content type:** `application/json` in and out. 4xx and 5xx bodies should
  also be JSON — `{"error": "<short>", "details": <optional>}`. Non-JSON
  bodies (proxy HTML pages) are tolerated but the user sees a raw snippet.
- **Dates:** `YYYY-MM-DD`, representing an IST calendar day
  (SirahOS `work-day.ts` already stores UTC-midnight-of-IST-day; the bridge
  matches).
- **Times:** `HH:MM` 24-hour, IST wall-clock. Optional.
- **Rate limits:** per-account only (login endpoint should return 429 after
  repeated bad passwords; everything else can be generous — a user tops out
  at ~100 pushes/day).

---

## 1. `POST /api/auth/login`

Exchange email + password for a JWT.

**Request**
```json
{ "email": "user@sirahdigital.in", "password": "…" }
```

**200 OK**
```json
{
  "accessToken": "<jwt>",
  "refreshToken": "<jwt or omitted>",
  "user": {
    "id": "clabc123…",
    "email": "user@sirahdigital.in",
    "name": "…",
    "companyId": "…"
  }
}
```
The bridge only reads `accessToken` and `user` — any extra fields are ignored.

**Errors**
| Status | When | Body |
|---|---|---|
| 401 | wrong email or password | `{"error":"Invalid email or password"}` |
| 403 | account deactivated | `{"error":"Account is deactivated"}` |
| 429 | too many attempts | `{"error":"Too many login attempts, try again in Xm"}` |

---

## 2. `GET /api/projects/selectable`

Projects the signed-in user may log work against (member of, active, in
their company). This endpoint doubles as the "is my token still valid"
probe — if it 200s, the token is live.

**Request:** no body, auth required.

**200 OK** — JSON array (not wrapped):
```json
[
  { "id": "clabc123…", "name": "AuraTel",  "client": "Acme",  "status": "active" },
  { "id": "clxyz789…", "name": "WealthOS", "client": "Beta",  "status": "active" }
]
```
Fields the bridge requires: `id`, `name`. `client` and `status` are shown in
the mapping UI; empty strings are fine.

**401:** expired or missing JWT — the tile shows a "sign back in" banner.

---

## 3. `POST /api/worklogs`

Create one WorkLog for the signed-in user. The bridge posts at most one per
(IST day, project folder) per machine.

**Request**
```json
{
  "date": "2026-11-13",
  "hours": 2.25,
  "notes": "3 Claude Code turns, 2h 15m total.\n- …",
  "projectId": "clabc123…"
}
```

Field rules:
| Field | Type | Required | Notes |
|---|---|---|---|
| `date` | `YYYY-MM-DD` | yes | IST calendar day |
| `notes` | string, ≤ 5000 chars | yes | bridge caps at 4800; see note format below |
| `hours` | number, 0 < h ≤ 24 | one of `hours` OR `startTime`+`endTime` | rounded to 2 decimals |
| `startTime` | `HH:MM` | optional alt to `hours` | IST |
| `endTime` | `HH:MM` | optional alt to `hours` | IST |
| `projectId` | string | optional | omit = General bucket |
| `taskId` | string | optional, bridge does not send it | |
| `newTaskTitle` | string | optional, bridge does not send it | |
| `taskStatus` | string | optional, bridge does not send it | |

**201 Created**
```json
{ "id": "clwl456…", "date": "2026-11-13", "hours": 2.25, "xp": 15, "streak": 7 }
```
The bridge only reads `id`; everything else is for the dashboard's status line.

**Errors**
| Status | When | Body |
|---|---|---|
| 400 | zod validation | `{"error":"Invalid input","details":[…]}` — bridge surfaces the first detail |
| 401 | expired JWT | bridge deletes its cached token and bails until the user signs in again |
| 403 | user not a member of `projectId` | should 400 with a clearer message if possible |
| 409 | duplicate WorkLog policy | optional; bridge's own `sirahos_pushes` table prevents this from the client side |

**Notes block format (informational)** — this is what the bridge writes into `notes`:
```
3 Claude Code turns, 2h 15m total.
- added the SirahOS bridge module and wired its scheduled task
- fixed the IST day boundary so a 00:30 IST turn lands on the right day
- added integration tests for the push watermark
(and 2 more)
```
First line is a stat header. Each `- …` is one user prompt (cap 160 chars).
`(and N more)` appears when the per-WorkLog cap is hit. If the user turns
off prompt sharing the body becomes `- (prompt hidden)` with the count
preserved in the header.

---

## 4. `DELETE /api/worklogs/:id`

Remove a WorkLog. The bridge uses this only when the user explicitly deletes
one from the tile UI (not currently wired; endpoint shape is here so a later
"undo last push" button needs no server change).

**200 / 204:** success.
**404:** also acceptable — treated as already-gone.
**401:** expired JWT.

---

## 5. `GET /api/worklogs?userId=<id>&date=<YYYY-MM-DD>`

List the signed-in user's WorkLogs for a date. Used to detect "user deleted
the bridge's row in the web UI, so stop re-creating it on next tick."

**200 OK** — same shape as the array convention:
```json
[ { "id":"clwl456…", "date":"2026-11-13", "hours":2.25, "notes":"…", "projectId":"…" } ]
```
Query params:
- `userId` — `"me"` or the user's id. Server should accept either.
- `date` — exact IST day. Server should also accept a range, but the bridge
  only asks for a single day.

**401:** expired JWT.

---

## What the bridge does NOT call (but you might expect)

- No `GET /api/auth/me` — the bridge uses `/api/projects/selectable` as its
  liveness probe (cheap, already authed, and the response is what the mapping
  UI needs anyway).
- No `PATCH /api/worklogs/:id` — on a change the bridge deletes + re-creates.
  No endpoint change needed on the SirahOS side.
- No webhook / push from SirahOS to the tile. The user explicitly declined
  this on 2026-11-04 — see `build-docs/features/sirahos-webhook-ingest.md`.

---

## Acceptance checks (one per endpoint)

1. `POST /api/auth/login` with bad password returns 401, not 500 and not 200-with-null-token.
2. `GET /api/projects/selectable` with an expired JWT returns 401, not 500.
3. `POST /api/worklogs` with `hours: 2.25, date: "2026-11-13", notes: "x", projectId: "<a real one>"` returns 201 with a non-empty `id`.
4. The same call again with the same day + project is allowed (the bridge's own table dedupes; the server does not need a 409).
5. `POST /api/worklogs` with `hours: 0` returns 400, not 201.
6. `DELETE /api/worklogs/:id` on a non-existent id returns 404, not 500.

If all six pass against the deployed SirahOS instance, the bridge works.
