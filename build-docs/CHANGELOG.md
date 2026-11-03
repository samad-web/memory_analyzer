# Changelog

## fix(auth)

**The loopback bypass survived a tunnel.** `local_only` was decided by the
bind address, but ngrok, cloudflared and `ssh -R` all run on this machine and
connect to `127.0.0.1` -- so every tunnelled request looked local and got
every scope, unauthenticated, at exactly the moment the dashboard was most
exposed. Found while actually tunnelling it to a phone.

The bypass now needs three conditions: bound to loopback, arrived on loopback,
and carrying no forwarding header. Verified against a live ngrok tunnel: the
shell serves, every `/api/` route 401s without a token, and a read token gets
403 on POST.

## feat(pwa-and-push)

Installable dashboard and Web Push for escalations. `pwa.py` (manifest and
generated PNG icons), `vapid.py` (P-256 + RFC 6979 for the VAPID signature),
`push.py` (subscriptions, sending, and the mailbox watcher), `web/sw.js`.
Schema 4 -> 5, additive.

Design consequences of the board's own constraints, not preferences:

- The push is sent by the **dashboard server**, not the hook, because "hooks
  never touch the network" and a push is a network call.
- Messages carry **no payload**, because encrypting one needs AES-GCM and "no
  new runtime dependencies" would mean hand-writing a cipher. The service
  worker asks the dashboard what happened and falls back to generic text.
- VAPID could not be avoided, so P-256 is implemented here -- signing only,
  with RFC 6979 deterministic nonces, checked against the RFC's published
  signatures for two messages.

One deliberate behaviour change: **the static shell is now served without a
token.** An installed app launches at `/` with no query string and the browser
fetches the manifest and service worker before any page runs, so a gate there
meant a home-screen icon opened a 401 forever. The shell carries no data;
every `/api/` route still demands a scope, and a test asserts the public set
exactly.

Two bugs found by its own tests: a non-dict subscription body crashed
`subscribe`, and `unsubscribe` by row id never matched because the id was
hashed a second time.

Caught by `test_deploy`'s import closure: `pwa` imports `viz`, which is not in
the reporter bundle. `tokens`, `vapid`, `push` and `pwa` are webserver-only
and moved to the full bundle.

## feat(scoped-tokens)

`tokens.py`: hashed, scoped, revocable access tokens for the web dashboard,
replacing the single all-or-nothing `web_token`. Scopes are `read`, `operate`
and `answer`; `answer` is reserved for the PWA milestone's reply channel and is
defined now so that endpoint cannot be born unscoped. Schema 3 -> 4, additive.

A missing credential is 401 and a missing scope is 403. Scopes are a list, not
a ladder. The legacy plaintext token keeps working and carries everything.

Found while testing: `verify` promised "never raises" but caught only
`sqlite3.Error`, so an `OSError` from opening the store would have escaped into
the HTTP handler. It catches everything now and fails closed.

## fix(ingest)

Three more defects:

- **`ingest` left an uncommitted generation bump on the torn-line path.**
  `observe.record` passes its own connection and commits afterwards, so the
  bump landed while the size it was derived from did not -- every later turn
  saw the same shrunken log and bumped again, and the Stop hook was left
  holding a write transaction it never asked for.
- **A rotated log whose first read landed on a partial line was never
  ingested.** Proven by a test that comes back `None`.
- **That path's return dict omitted `gen`**, which every other path carries.
- **"all five wired"** was printed by a release with six hooks -- in
  `hooks.py status`, the installer's success message, its header comment and
  the README. It is the line someone reads to decide the install worked.

## feat(mcp-server)

Stdio JSON-RPC MCP server over the store: `search_memory`, `project_status`,
`milestone_state`, `recent_decisions`, `observation_detail`. Makes the
session-start read an any-time read. `verify` is deliberately not exposed.

Three defects found while building it, two of them pre-existing:

- **`project_status(project="X")` synced this repo's GOAL.md into X's board.**
  `goals.sync` resolves GOAL.md against the working directory, so a read tool
  was rewriting another project's milestones. Now only the local project syncs.
- **`store.connect()` took a write lock on every open.**
  `pragma user_version=N` writes the file header, so a pure read queued behind
  any open write transaction, waited the full ten seconds and failed "database
  is locked". A current database is now opened without writing at all --
  which also removes a 150-line `executescript` from every dashboard poll.
- **`goals.verify` held a write transaction across every check command.**
  Results were committed once at the end, so any check that touched the store
  died on a locked database and was recorded as a failure it never had. Each
  result now commits before the next command runs.

Both store/goals fixes were confirmed to fail their new tests when reverted.

## feat(memory-tab) -- b614e19

Memory tab in both dashboards over one query function. Payload trimmed from
15,286 to 2,415 bytes a row via page rows plus untrimmed counts plus
detail-on-demand.

## fix(install-bundle) -- 1c85de0

Four bugs found by exercising the install bundle: a failing test suite recorded
as passing, the bundle no longer importing itself, an unbounded hook stdin read,
and `agent_alert.write` claiming never to raise without a try/except.

## feat(memory-layer) -- 3329d47

`observe.py`, `goals.py`, `context.py`, GOAL.md, and sessions named after their
repository rather than their cwd.

## feat(store) -- 075297a

`store.py`: SQLite WAL + FTS5 behind the event log, and resume events that
resume nothing are no longer logged (87% of the log was redundant resumes).

## feat(tile) -- 1736201

Always-on-top tile, agent board and dashboard.
