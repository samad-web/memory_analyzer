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
- **The palette has no red.** Deliberate: status colour comes from
  `viz.STATUS[...]`, and a tile that cries wolf gets ignored.

## Constraints

- No new runtime dependencies beyond PySide6/psutil/pywin32.
- Nothing merges to `master` unattended.
- Windows first: cp1252 consoles, so any hook stdout is reconfigured to UTF-8
  with `errors="replace"` in code (`python -I` ignores `PYTHONIOENCODING`).

## Known issues

- **`python hooks.py install` has not been run.** The `SessionStart` hook is
  written, tested and shipped but not registered, so injection is not live. The
  settings.json edit was refused as self-modification and was not worked around.
- **`python mcp_server.py install` has not been run**, for the same reason. The
  server passes its tests and answers a real handshake over a pipe, but nothing
  has registered it with Claude Code, so no agent can call it yet.
- Four of eleven milestones report `unverifiable` because they are unbuilt.

## Open questions

- Should a milestone with no `check:` command be allowed on the board at all, or
  should `sync` reject it? Currently it is accepted and reports `unverifiable`.
