# SirahOS-side webhook: server-to-server WorkLog ingest

> **Status: declined 2026-11-04.** After weighing the tradeoffs the team chose
> the per-user sign-in path (feature 12) as the single way the tile talks to
> SirahOS. This doc stays as the record of why, so a future reopening does
> not re-derive the same thinking.

## The pitch

Add one new HTTP endpoint on SirahOS -- `POST /api/integrations/memory-analyzer/worklogs`
-- that any teammate's tile can push to with **one shared team secret**,
without each teammate having to sign in from the tile. The server resolves
the SirahOS user from the Windows username (or an email override in config)
and writes WorkLogs on their behalf. No tunnel; no per-user JWT; one admin
step at install time per team.

Why "webhook" is the right word: it is a push-only, server-to-server
endpoint. The tile posts; nothing is read back; nothing is interactive.

## The ambiguity worth settling first

Two different things in the conversation have carried the word "tunnel":

1. **Phone -> tile tunnel** (ngrok/cloudflared on your laptop). This is the
   remote access channel for *reading* your dashboard from your phone. It is
   the thing the 2026-11-03 security fix locked down -- a tunneled URL now
   needs a scoped token. The webhook plan does nothing to or for this; it is
   an independent problem.

2. **Tile -> SirahOS "tunnel"**. This never existed as a tunnel. The push
   has always been plain outbound HTTPS to `sirahos.example.com`, which no
   teammate's laptop needs to be reachable from the outside for. The current
   bridge already works this way -- just with a per-user JWT instead of a
   team secret.

So the choice this feature actually makes is NOT "tunnel vs no tunnel". It
is **per-user sign-in vs team integration secret**.

## What changes vs today

Today (just built):

- Each teammate opens the dashboard, types their SirahOS email and
  password, the tile exchanges it for a JWT, and the bridge posts WorkLogs
  under that JWT. XP and streaks are credited to that user automatically
  because SirahOS sees the WorkLog as created BY them.

Proposed:

- One shared integration secret, written by the admin into `config.json`
  during install (via `install.ps1 -SirahOSIntegrationKey ...`), that every
  teammate's tile uses.
- Each push carries the Windows user's identity as metadata
  (`{"actor_email": "jane@sirahdigital.in"}`), optionally with a
  `signature = HMAC-SHA256(secret, body)` header so SirahOS can trust the
  payload came from a laptop that holds the secret.
- SirahOS looks up the matching `User` by email, writes the WorkLog with
  `userId = <that user>.id` and still runs the XP / streak logic the
  existing endpoint runs.

## Tradeoffs

| | Per-user JWT (today) | Team integration secret (proposed) |
| --- | --- | --- |
| User flow | Each teammate signs in once | Admin installs the secret; teammates just install the exe |
| New SirahOS code | None -- reuses `/api/auth/login` + `/api/worklogs` | One new route + one new auth middleware |
| Secret lifetime | Per-user JWT (expires on refresh) | Team secret (long-lived, rotatable) |
| Revocation | Teammate signs out, or admin deactivates them in SirahOS | Admin rotates the team secret |
| If secret leaks | One teammate's JWT; they re-login after it is revoked | Whole team's write path; must rotate across every laptop |
| Audit | Per-user, since every WorkLog is created BY the real user | Still per-user in the SirahOS DB; the secret is only how we authenticated |
| Non-teammate push | Impossible without that user's password | Impossible without the secret AND an `actor_email` on the roster |
| Non-Windows teammate | Can log in from the tile (though tile is Windows-only) | Can't -- the tile is still Windows-only, so this does not matter today |

Reality check: for a small, trusted team the integration-secret path is
genuinely simpler and nobody has to type a password into the tile. For a
larger company it has one real risk -- a single leaked secret writes WorkLogs
across the whole team until rotated. Audit still shows each push as the
right user, so detection is possible, but prevention is harder than
yanking one JWT.

**Recommendation**: ship the integration-secret path and keep the per-user
sign-in as the fallback for anyone who prefers it. The code is a thin layer
on top of what the bridge already does; the two can coexist in the same
`bridge.py`, picking whichever one `config.json` has filled in.

## New SirahOS endpoint

`POST /api/integrations/memory-analyzer/worklogs`

```http
POST /api/integrations/memory-analyzer/worklogs HTTP/1.1
Host: sirahos.example.com
Content-Type: application/json
X-Integration-Key: <team secret id>
X-Integration-Signature: <hex HMAC-SHA256 of body under the key's secret>

{
  "actor_email": "jane@sirahdigital.in",
  "date": "2026-11-03",
  "hours": 2.5,
  "project_id": "clabc...",
  "notes": "3 Claude Code turns, 2h 30m total.\n- added the webhook...",
  "task_count": 3,
  "idempotency_key": "jane@sirahdigital.in|2026-11-03|voicetranscriber"
}
```

Responses:

- `201 Created` -- WorkLog created, body returns the `WorkLog.id`.
- `200 OK`      -- an idempotent repost of the same `idempotency_key`
                   (same user, day and folder); returns the existing id so
                   the tile can record it the same way.
- `401 Unauthorized` -- missing / unknown key, or signature mismatch.
- `404 Not Found` on `actor_email` -- the email is not on this company's
                   roster. The tile reports which email was unknown so the
                   admin can add them.
- `422` -- the usual zod-shaped validation errors (hours out of range,
                   malformed date).

SirahOS-side pieces to write:

1. A `IntegrationKey` table: `id`, `companyId`, `label`, `hashed_secret`,
   `created_by`, `revoked_at`. The admin mints one from Settings; the UI
   shows the secret once at mint and never again, same pattern as the
   dashboard tokens on this side.
2. A `requireIntegration` middleware: pulls `X-Integration-Key`, finds the
   row, verifies `X-Integration-Signature` with the stored secret, and
   attaches `req.integration = { companyId, keyId }` so the handler knows
   which company's roster to resolve the actor against.
3. The route itself: resolves `actor_email` to a `User` (same company,
   active), computes `hours` or `startTime/endTime`, calls the same
   WorkLog creation path the existing `POST /api/worklogs` uses (same XP /
   streak / attendance side-effects), and returns the created id.

Idempotency key is the server's own composite key -- a second POST with the
same `idempotency_key` returns the already-created row, so a half-delivered
batch is safe to retry. The tile's `sirahos_pushes.worklog_id` is still
the server-returned id; the bridge code is unchanged.

## Tile-side changes

`sirahos.py`:
- Add `create_worklog_via_integration(base, key_id, secret, *, actor_email, ...)`.
- Signs the body with `hmac.new(secret.encode(), body, 'sha256').hexdigest()`.

`bridge.py`:
- Read `cfg['sirahos'].get('integration_key')` and `['integration_secret']`.
- If both present, use the integration path and skip the JWT loader entirely.
- If only JWT exists, keep today's behaviour.
- `actor_email` falls back to Windows username when nothing is set; the
  server rejects unknown users with a readable error.

`install.ps1`:
- Add `-SirahOSIntegrationKey <id>` and `-SirahOSIntegrationSecret <secret>`
  parameters. When supplied, writes them to `config.json` under
  `sirahos.integration_key` / `sirahos.integration_secret` and skips the
  "sign-in prompt" the dashboard currently shows on first launch.

`webserver.py`:
- The SirahOS tab in the dashboard shows which auth mode is active; sign-in
  only shows if neither an integration key nor a JWT is present.

## Impact map

- **New in memory_analyzer**: a client wrapper (`create_worklog_via_integration`),
  one branch in `bridge.push()`, two new config fields.
- **New in SirahOS**: one new table, one new middleware, one new route,
  one Settings page to mint keys.
- **No change** to the existing `POST /api/worklogs` -- the per-user path
  still works.
- **No new memory_analyzer runtime deps** -- `hmac` is stdlib.

## Acceptance criteria

1. An admin runs `install.ps1 -SirahOSIntegrationKey K -SirahOSIntegrationSecret S`
   on five laptops. Each teammate's tile pushes WorkLogs for the right user
   on SirahOS, with the right hours, under their own email. No teammate
   types a password.
2. Rotating the secret in SirahOS and re-running the installer flips
   everyone over without data loss.
3. A laptop whose Windows user has no match on SirahOS gets a 404 with
   the unknown email in the body; the admin sees it in the tile dashboard,
   adds the user to SirahOS, and the next push succeeds.
4. The existing per-user JWT path keeps working for anyone with just a
   SirahOS login in `config.json` and no integration key.
5. A tampered body (same key, wrong signature) is rejected with 401 and
   never writes a WorkLog.
