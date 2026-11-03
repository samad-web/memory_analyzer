# Feature: MCP server over the store

## Purpose

`context.py` tells a session what it needs **at session start**. That is one
read, inside a 4,000-character budget, decided before the agent knows what it
has been asked to do.

The problem it leaves: twenty turns in, when the agent is actually standing in
front of the question "have we tried this before?", it has no way to ask. The
answer is in `store.db` and unreachable. So the agent re-derives what it already
knew, and that shows up as prompts per milestone -- the one number this project
exists to reduce.

This feature makes the store queryable **mid-turn**, as tools, over MCP.

## Architecture

A single module, `mcp_server.py`, speaking MCP over stdio. No framework: MCP is
newline-delimited JSON-RPC 2.0 on stdin/stdout, which is `json` and a `while`
loop, and "no new runtime dependencies" is a constraint in GOAL.md.

```
Claude Code  --stdin-->   mcp_server.py   --> store.connect()
   (any project)  <--stdout--   |            --> goals.sync()
                                v
                        TOOLS: name -> (schema, handler)
```

Methods handled: `initialize`, `notifications/initialized`, `ping`,
`tools/list`, `tools/call`, plus `resources/list` and `prompts/list` answering
empty so a client that probes capabilities does not see an error.

### Tools

| Tool | Reads | Answers |
| --- | --- | --- |
| `search_memory(query, project?, limit?)` | `observations_fts` | "have we touched this before?" |
| `project_status(project?)` | `goals`, `milestones`, `decisions`, `observations` | "where is this project?" |
| `milestone_state(project?)` | `milestones` | "what is the live milestone and what proves it?" |
| `recent_decisions(project?, limit?)` | `decisions` | "what was already settled, and why?" |
| `observation_detail(id)` | one `observations` row, untrimmed | "show me that turn in full" |

### Data model

None of its own. It is a read surface over the existing schema.

### Interfaces

- **Output is text, not JSON.** A tool result is read by a model, so it is
  rendered as compact lines in the same register as `context.py`. Handing back
  raw rows spends tokens on punctuation.
- **Output is trimmed.** `search_memory` uses `store._row_out`'s page form
  (12 files, 200-character narrative, no commands) and says how much it hid.
  `observation_detail` is the untrimmed escape hatch. Same progressive
  disclosure as the dashboards, same reason: a tool result is a context cost.
- **The project defaults from the server's cwd** via
  `agent_alert.project_name`, so an agent need not know its own project name.
- **`project_status` and `milestone_state` call `goals.sync` first**, so an
  edit to GOAL.md is reflected without a separate command. `sync` only reads
  the file and writes the database; the file owns structure.

## Impact map

| Touched | What could break |
| --- | --- |
| `store.py` | Nothing: read-only use of existing functions. `_row_out` is reused, so a change to page trimming changes tool output too -- which is intended. |
| `goals.py` | `sync` is called on two tool paths. If GOAL.md is malformed, sync raises; every handler is wrapped, so the tool returns an error string instead of killing the server. |
| `deploy.py`, `install/install.ps1` | The file lists must gain `mcp_server.py`, or a teammate's installed copy has a server that is configured and absent. `tests/test_deploy.py` walks the import closure and will catch a missed dependency, but only if the new module is in the list -- so a test asserts it by name. |
| `~/.claude.json` | Registration writes an `mcpServers` entry. Merge, never overwrite, same rule as `hooks.py`. |
| Nothing else | The tile, the dashboards and the hooks do not import it. |

## Edge cases and failure modes

| Case | Behaviour |
| --- | --- |
| stdout polluted by a stray `print` | Fatal to the protocol. Nothing in the module prints; diagnostics go to stderr, and `store`/`goals` are silent. |
| An empty store (fresh install) | Every tool returns a plain "no history yet" sentence, not an empty structure or an error. |
| A malformed JSON line on stdin | Answered with a JSON-RPC parse error (`-32700`) and the loop continues. |
| A notification (no `id`) | No response at all, as the spec requires. A reply to a notification is a protocol violation. |
| An unknown method | `-32601`, loop continues. |
| A handler raising | Caught, returned as `isError: true` content. A server that dies takes the agent's memory with it. |
| Hostile `query` (`a"b`, `NEAR(`, `*`) | `store.search` already quotes term by term and catches `OperationalError`. Covered by a test. |
| Non-integer `id` to `observation_detail` | `store.observation` catches and returns `{}` -> "no such observation". |
| `limit` absurd or negative | Clamped to 1..50. An unclamped limit is a context-exhaustion vector. |
| cp1252 console | stdout/stdin reconfigured to UTF-8 explicitly; a Windows console default would mangle a narrative and corrupt the frame. |
| EOF on stdin | Clean exit 0. |
| GOAL.md absent | `sync` is wrapped; status still reports observations and counts. |

## Acceptance criteria

1. `initialize` returns a protocol version and `tools` capability; `tools/list`
   lists all five tools, each with a valid JSON Schema.
2. `search_memory` for a term in a stored observation returns that observation
   and not the others.
3. A tool result for a 60-command, 60-file observation stays under 4 KB.
4. `observation_detail` returns the untrimmed row.
5. `project_status` on an empty store returns prose, not an error.
6. A malformed line, an unknown method and a raising handler each leave the
   server answering the next request.
7. A notification produces no reply.
8. `mcp_server.py` is in both install file lists.
9. End to end: a real subprocess, fed a real handshake on stdin, returns a
   search result on stdout.
