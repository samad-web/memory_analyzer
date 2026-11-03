# Changelog

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
