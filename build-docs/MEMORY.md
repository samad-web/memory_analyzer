# Project memory

Decisions, why they were made, and what is still open. Read at session start;
updated whenever a decision is made.

## Decisions

- **The event log is the record; SQLite is a projection.** `events` can be
  dropped and replayed from `events.jsonl`. `observations`, `goals`,
  `milestones`, `decisions` cannot, so they migrate in place.
- **Ingest is keyed `(gen, off)`.** Byte offsets are unique only within one life
  of a file. Found by `test_truncated_log_restarts`, which failed 50 != 51 after
  a rotation.
- **A session is named after its nearest `.git`, not its cwd.** Keying on cwd
  split one agent across two rows when it `cd`-ed into a subdirectory, which is
  why the Voice_Transcriber session appeared as `platform`.
- **Milestone status is asserted by running `check:`, not inferred.** No check
  means `unverifiable`. This constrains how acceptance criteria are written:
  they must be mechanically checkable or they do not count.
- **`milestones.file_status` exists** to distinguish a human unticking a box
  from `verify` proving it done.
- **Hooks never block a turn, except `SessionStart`.** An async hook's stdout is
  nobody's input, so the one hook whose whole job is to print context must be
  synchronous -- and therefore must be cheap and must never hang. It reads
  stdin on a 2s bounded thread for exactly that reason.
- **Hooks never call a model or the network.** Observation extraction is
  deterministic parsing. Zero tokens, zero latency, zero failure modes that
  depend on a service being up.
- **Payloads are progressively disclosed.** Page rows ship trimmed lists plus
  untrimmed counts; selecting a row fetches it whole. Measured 15,286 ->
  2,415 bytes a row.
- **One query function, two renderers.** Qt and HTTP both call
  `store.observations_page`.
- **`verify` is not exposed over MCP.** It runs arbitrary `check_cmd` shell
  strings; reachable from an agent mid-turn that is a shell escape around the
  permission system. It stays a CLI and dashboard action.
- **Opening the store must take no write lock.** WAL only buys concurrent
  readers if `connect()` does not write. A current database is opened with no
  migration, no schema script and no header write.
- **A read must never write on another project's behalf.** `goals.sync`
  resolves GOAL.md against the working directory, so only the local project is
  ever synced. Named projects are reported from the database alone.
- **A long-running command must not run inside a write transaction.**
  `goals.verify` commits each result before the next check command starts.
- **API tokens are hashed with sha256, not a password KDF.** 32 bytes of
  `secrets.token_urlsafe` has no dictionary to run against it; scrypt would buy
  nothing and cost real time on every request.
- **401 and 403 are different answers.** Missing credential vs missing scope.
- **Scopes are a list, not a ladder.** `operate` does not imply `read`.
- **`answer` is defined before it has an endpoint**, so the PWA's reply channel
  cannot be born unscoped.
- **The push is sent by the server, never the hook.** A push is a network
  call and hooks may not make one. This is the correct split, not a workaround.
- **Push messages carry no payload.** Encrypting one needs AES-GCM; the
  service worker asks the dashboard instead. Hand-written ciphers are out.
- **Hand-written ECDSA is acceptable only because the nonce is deterministic
  (RFC 6979) and the output is checked against published vectors.** If either
  of those stops being true, this decision stops holding.
- **The static shell is public; every `/api/` route is scoped.** Forced by
  installability: an installed app opens `/` with no token in sight.
- **The palette has no red.** Deliberate: status colour comes from
  `viz.STATUS[...]`, and a tile that cries wolf gets ignored.

## Constraints

- No new runtime dependencies beyond PySide6/psutil/pywin32.
- Nothing merges to `master` unattended.
- Windows first: cp1252 consoles, so any hook stdout is reconfigured to UTF-8
  with `errors="replace"` in code (`python -I` ignores `PYTHONIOENCODING`).

## Known issues

- Four of eleven milestones report `unverifiable` because they are unbuilt.
- **`observations.tests` concatenates every test run in a turn** with `; `, so
  a turn that failed and then passed reads `failed; ok` with no order and no
  indication which was last. It is accurate and hard to read, which is the
  wrong trade for a field an agent is shown at session start.
- **`ran, outcome unparsed`** still appears, so some runner's summary line is
  not matched by `observe._RUNNERS`.

## Settled

- Both installs are registered (2026-11-03): six hooks `ok`, and
  `memory-analyzer` in `~/.claude.json`. Neither could be done from inside a
  session -- the auto-mode classifier refuses them as self-modification, so
  they are a human step by construction.

## Open questions

- Should a milestone with no `check:` command be allowed on the board at all, or
  should `sync` reject it? Currently it is accepted and reports `unverifiable`.
