# Feature: Antigravity agents on the tile and in memory

## Purpose

Sessions run in Antigravity (CLI `agy`, IDE, App) are invisible today: no
tile row, no Scorecard events, no observations, no SessionStart context, no
MCP memory. Half the agent work on this machine goes unrecorded.

## Verified facts (agy 1.5.13, Windows, probed 2026-11-20)

- Hooks live in `~/.gemini/config/hooks.json` (global) and
  `<ws>/.agents/hooks.json`. Top level = named specs only; **any other top-level
  key silently disables the whole file** (firstmate, verified on 1.4.9).
- `SessionStart`, `PreInvocation`, `PostInvocation`, `Stop` take flat handler
  lists; `PreToolUse`/`PostToolUse` take `{matcher, hooks:[...]}` groups.
- Commands run through **cmd.exe**, cwd = the `.agents` dir. Hooks are
  synchronous: there is no async flag.
- Every payload has `cwd`, `conversationId`, `transcriptPath`, `lastUserInput`
  and `hookEventName`. `Stop` adds `fullyIdle` and `terminationReason`.
- `invocationNum` resets to 0 on each user turn. `SessionStart` does not fire
  on `--continue`.
- There is **no Notification and no SessionEnd event**.
- SessionStart context goes on stdout as
  `{"injectSteps":[{"systemMessage":{"systemMessage":"..."}}]}`.
- In PreToolUse, empty stdout means allow, and a printed object with no
  `decision` **denies** the tool call.
- `transcript_full.jsonl` lines with `source: MODEL` carry top-level `model`,
  `input_tokens`, `output_tokens`, `cache_read_tokens` and `tool_calls`.
- MCP servers are registered in `~/.gemini/config/mcp_config.json`
  (`{"mcpServers":{...}}`), which is currently empty.

## Architecture

The same `notify.py` / `SirahlyzerBridge.exe notify` entry point handles both
agents. There is no second pipeline.

| agy event | notify action | Note |
| --- | --- | --- |
| SessionStart | `context --agy` | prints the `injectSteps` JSON instead of `hookSpecificOutput` |
| PreInvocation | `prompt` | only when `invocationNum == 0`; other invocations exit silently |
| Stop | `done` | tokens + observation, as for Claude |

- `notify.main`: normalise the payload once (`session_id ← conversationId`,
  `transcript_path ← transcriptPath`, `prompt ← lastUserInput`). `cwd` is
  already present.
- `events.tokens_since`: add a branch for `source == "MODEL"` lines.
- `stats.price_of`: Gemini models are priced at 0. They are paid in Antigravity
  credits, not metered dollars, and the `default` (Claude) rate would invent
  spend.
- `hooks.py`: `install/uninstall/status(agent="agy")` writes one named spec,
  `"sirahlyzer"`, into `~/.gemini/config/hooks.json`. It uses the same rules as
  the Claude path: merge, idempotent, back up first. Foreign specs are untouched.
- `mcp_server.py`: `install(target="agy")` merges `memory-analyzer` into
  `mcp_config.json`.
- `bridge.py` / `setup.cmd`: `hooks-install` also wires agy when `~/.gemini`
  exists.

**Not registered:** PreToolUse. With no Notification event there is never a
blocked state for "working" to resume from. Registering it would put a
synchronous Python start on every tool call, and a stray stdout object would
deny tool calls.

## Impact map

| Touches | Risk |
| --- | --- |
| `notify.py` main | Claude payloads must be unchanged; normalisation only fills keys that are missing |
| `context.emit` | Claude output shape must not change |
| `events.tokens_since` | Claude transcripts must not double-count; the agy branch is keyed on `source` |
| `stats.price_of` | Claude prices unchanged |
| `hooks.py` | Claude `settings.json` path untouched; `status` reports both agents |
| `mcp_server.py` | `~/.claude.json` path untouched |
| `bridge.py`, `build-exe/team-files/setup.cmd` | the exe must still install when `~/.gemini` is absent |
| `observe.record` | parses Claude transcripts; on agy it yields an observation with no files or tests (accepted, see below) |
| `agent_alert` | agy rows never get an `end`; they age out like idle Claude rows |

## Edge cases and failure modes

- **Malformed or non-dict `hooks.json`:** the file is backed up, then replaced
  with our spec only, and the event is reported.
- **A foreign top-level key (e.g. `version`):** it is kept, never deleted, and
  `status` warns that agy ignores the whole file.
- **Hook timeout:** set to 10s. `done` stays under 1s on the measured path.
- **`~/.gemini` missing:** agy install is skipped and reported, not an error.
- **Hook prints anything except the SessionStart JSON:** that would be a bug.
  Tested: `prompt`/`done` print nothing.
- **Workspace hooks (e.g. firstmate's `.agents/hooks.json`):** they run
  alongside the global file. Ours never blocks and never returns a `decision`.

## Out of scope (v1)

- Waiting on permission: agy has no event for it, so agy rows never turn orange.
- Files changed and tests run, from agy transcripts: `observe` would need a
  second parser.
- Antigravity IDE/App verified live: same config files per the docs, but only
  the CLI was probed.

## Notes from the live run

- Headless `agy -p` auto-denies MCP calls without a `permissions.allow` rule.
  `mcp(*)` worked; `mcp(memory-analyzer)` did not. Interactive sessions prompt.

## Acceptance criteria

1. `hooks.py install --agy` writes `sirahlyzer` with SessionStart, PreInvocation
   and Stop; re-running produces no duplicates; foreign specs survive.
2. A real `agy -p` run in this repo adds `prompt` and `done` events with the
   project name and non-zero tokens, and a tile row appears.
3. A new agy session receives the project context (the agent can quote the GOAL
   line).
4. `PreInvocation` with `invocationNum > 0` logs nothing.
5. Every existing Claude hook test still passes, unchanged.
6. `agy mcp list` shows `memory-analyzer`, and a tool call returns data.
