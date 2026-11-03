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
| 9 | PWA shell and push | planned |
| 10 | Goal-driven runner with worktree isolation | planned |
| 11 | Autonomy budgets and escalation policy | planned |

## Build order

The order above is a dependency order, not a preference. 1 is the floor
everything else queries. 3 fills it. 4 gives it direction. 5 and 6 are the two
read paths (agent and human). 7 turns the session-start read into an any-time
read, which 10 needs to work at all. 8 gates 9. 11 bounds 10.
