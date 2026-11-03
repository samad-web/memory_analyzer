# Plan

## Goal and scope

Grow a Windows system-monitor tile into an **agent control plane**: it carries a
project's end goal, remembers what has been done, is reachable from another
device, and runs supervised autonomous work against a milestone board.

The metric being optimised is **prompts per completed milestone**. Measured
baseline over nine days: 351 prompts, 219 finished turns, 15 projects, and
agents blocked on a human only 62 times. Interruption is not the bottleneck;
direction is.

Out of scope, deliberately:

- Unattended merges to `master`. A run opens a PR; a human merges.
- Cloud execution. Execution stays on this machine; remote access is a view and
  a control channel, not a second runtime.
- New runtime dependencies. Standard library, plus the PySide6/psutil/pywin32
  the tile already needs.
- Model calls inside hooks. Hooks append locally and exit.

## Project memory

Decisions that constrain this plan are in [MEMORY.md](MEMORY.md). The load-bearing
ones: a session is named after its nearest `.git`, status is **asserted** by
running a check command rather than inferred, the event log is append-only and
the SQLite store is a rebuildable projection of it, and hooks must never block a
turn (with `SessionStart` the single synchronous exception).

## Features, prioritized

Each has a doc under `features/`. The board of record is [GOAL.md](../GOAL.md) at
the repository root, which is both the human-editable source and the thing
`goals.py` parses.

| # | Feature | State |
| --- | --- | --- |
| 1 | Indexed store behind the event log | done |
| 2 | Stop logging resumes that resume nothing | done |
| 3 | Deterministic observation extractor | done |
| 4 | Goal and milestone layer read from GOAL.md | done |
| 5 | SessionStart injection of goal and recent work | done |
| 6 | Memory tab in both dashboards | done |
| 7 | [MCP server over the store](features/mcp-server.md) | done |
| 8 | [Scoped access tokens for the web dashboard](features/scoped-tokens.md) | done |
| 9 | [PWA shell and push](features/pwa-and-push.md) | done |
| 10 | Goal-driven runner with worktree isolation | planned |
| 11 | Autonomy budgets and escalation policy | planned |
| 12 | [SirahOS WorkLog bridge](features/sirahos-worklog-bridge.md) | core done; UI pending |
| 13 | [Shareable `.exe` for the team](features/exe-distribution.md) | planned |
| 14 | [Dashboard visual identity: match SirahOS](features/sirahos-visual-identity.md) | done |
| 15 | [SirahOS-side webhook: server-to-server WorkLog ingest](features/sirahos-webhook-ingest.md) | declined 2026-11-04 — per-user sign-in stays the only path |

## Open questions on the SirahOS bridge

Decisions needed before build starts. Each has a recommendation.

1. **Aggregation granularity.** The tile records per-turn (seconds-to-minutes).
   SirahOS WorkLogs are per-day human entries. Recommendation: one WorkLog per
   `(IST calendar day, project folder)`, auto-generated notes listing the turns.
   The alternative — one WorkLog per turn — would make hundreds of 2-minute
   entries and swamp the SirahOS team view.

2. **Project mapping.** `memory_analyzer.tasks.project` is a disk folder name;
   `SirahOS.projectId` is a cuid. Recommendation: a user-maintained
   `folder_project_map` in the tile's config, with a nag when an unmapped
   folder appears for ≥ 2 days. Alternative: fuzzy-match on folder name vs
   SirahOS project name (brittle; names drift).

3. **Milestone-to-task mapping.** GOAL.md milestones ≠ SirahOS tasks today.
   Recommendation: skip `taskId` in v1. Add it in v2 once the GOAL.md board
   has a stable id for each milestone.

4. **Code signing.** See `features/exe-distribution.md`. Recommendation: ship
   unsigned v1, revisit if a teammate's IT blocks it.

5. **Who authenticates the push.** Each teammate signs in to SirahOS from the
   tile; their JWT is stored via Windows DPAPI; WorkLogs are posted *as them*.
   The alternative — one service account posting on everyone's behalf — would
   require a new SirahOS endpoint and would strip per-user XP/streak.
   Recommendation: per-user sign-in, no new SirahOS endpoint.

6. **Non-Windows teammates.** The tile is Windows-only (PySide6+pywin32). If
   any teammate uses macOS or Linux, they will keep logging by hand. Flag:
   who on the team is on what OS?

## Build order

The order above is a dependency order, not a preference. 1 is the floor
everything else queries. 3 fills it. 4 gives it direction. 5 and 6 are the two
read paths (agent and human). 7 turns the session-start read into an any-time
read, which 10 needs to work at all. 8 gates 9. 11 bounds 10.
