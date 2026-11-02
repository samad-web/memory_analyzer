# Goal

Grow this from a machine-telemetry tile into an agent control plane: it should
carry a project's end goal, remember what has been done, be reachable from
another device, and run supervised autonomous work against a milestone board.
The tile stays the tile -- all of it lives behind the dashboard and the HTTP
API.

The thing being optimised is **prompts per completed milestone**. Measured
baseline over nine days: 351 prompts against 219 finished turns across 15
projects, while agents blocked on a human only 62 times. Interruption is not the
bottleneck; direction is.

## Constraints

- No new runtime dependencies. Standard library, plus the PySide6/psutil/pywin32
  the tile already needs.
- Hooks never touch the network and never block a turn. They append locally and
  exit.
- A milestone is done when something ran and proved it, not when an agent says so.
- Nothing merges to master unattended.

## Milestones

- [x] Indexed store behind the event log
      acceptance: re-ingesting an overlapping range adds no duplicate rows
      check: python -m unittest tests.test_store
- [x] Stop logging resumes that resume nothing
      acceptance: a turn with no block logs no resume event
      check: python -m unittest tests.test_notify
- [x] Deterministic observation extractor
      acceptance: a finished turn yields its files, commands, tests and narrative
                  without calling a model
      check: python -m unittest tests.test_observe
- [x] Goal and milestone layer, read from GOAL.md
      acceptance: sync reads structure, verify asserts status, export writes it back
      check: python -m unittest tests.test_goals
- [x] SessionStart injection of goal and recent work
      acceptance: a fresh session is told the goal, the live milestone and the
                  last few turns, inside a character budget
      check: python -m unittest tests.test_context
- [ ] Memory tab in the dashboard
      acceptance: observations are searchable from the Qt and web dashboards
- [ ] MCP server over the store
      acceptance: an agent can search its own history mid-turn, not only at
                  session start
- [ ] Scoped access tokens for the web dashboard
      acceptance: a token carries read/operate/answer scopes and is stored hashed
- [ ] PWA shell and push for the web dashboard
      acceptance: installable on a phone, and an escalation arrives as a push
- [ ] Goal-driven runner with worktree isolation
      acceptance: a queued milestone runs headless in its own worktree, verifies
                  against its check command, and opens a PR
- [ ] Autonomy budgets and escalation policy
      acceptance: a run stops on token, wall-clock or tool-call ceiling, and
                  escalates only on scope change, irreversible action, overrun
                  or repeated failure
