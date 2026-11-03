# Testing

## Strategy

`unittest`, standard library only, run with `python -m unittest discover tests`.

Every module reads its paths off `LOCALAPPDATA` once at import time, so a test
wanting its own database must redirect the variable **before** the first import
and reload in dependency order. That is `tests/_env.Sandbox`, which exists so
that five test files cannot each get it subtly wrong and one of them end up
writing to the real event log.

Two rules learned the hard way:

1. **Never assert against a live log.** A growing `events.jsonl` added four rows
   mid-test and read as duplication. Tests copy the log frozen.
2. **Size is a behaviour.** `test_a_page_row_stays_small` asserts a page row
   stays under 4 KB, because that regression is invisible until someone opens
   the dashboard on a phone.

## Per-feature results

| Feature | Tests | Result |
| --- | --- | --- |
| Indexed store | `tests/test_store.py` | pass |
| Observation extractor | `tests/test_observe.py` | pass |
| Goal layer | `tests/test_goals.py` | pass |
| SessionStart context | `tests/test_context.py` | pass |
| Hook dispatch | `tests/test_notify.py` | pass |
| Install bundle closure | `tests/test_deploy.py` | pass |
| Memory tab | `tests/test_memory.py` | pass |
| MCP server | `tests/test_mcp.py` | pass |
| Scoped tokens | `tests/test_tokens.py` | pass |
| PWA and push | `tests/test_push.py` | pass |

## Failures found and fixed

- **A failing suite recorded as passing.** pytest prints `1 failed, 6 passed`
  and a passed-first regex matched the `6 passed` inside it. Patterns are now
  runner-aware and check failure first.
- **The install bundle stopped importing itself.** Two new lazy imports inside
  `notify.py` function bodies were not added to the file lists, and `notify`'s
  top-level guard turns `ImportError` into `exit(0)` -- so `SessionStart` would
  have failed silently forever. `test_deploy.py` now walks the import closure
  including function-body imports, and was checked against a deliberately
  broken file list to confirm it fails.
- **An unclosed stdin stalled a synchronous hook.** Now a 2s bounded read.
- **Offsets collided across log generations.** `(gen, off)` primary key.
- **`verify` could not tell a human untick from its own proof.** Added
  `milestones.file_status`.
- **Wrapped GOAL.md lines were silently truncated.** Added continuation-line
  parsing to `goals.parse`.
- **A `<task-notification>` wrapper became an observation title.** The harness
  submits prompts through `UserPromptSubmit` exactly like typed ones; machine
  wrappers are now stripped.
- **A read tool wrote to another project's board.** `goals.sync` resolves
  GOAL.md against the working directory, so `project_status(project="X")`
  read this repository's board and wrote it under X's name. Found by a test
  asserting a milestone count that came back as this repo's own 11.
- **Opening the store took a write lock.** `pragma user_version=N` writes the
  file header, so a read queued behind any open write transaction, waited the
  full ten seconds and failed "database is locked". Found because the MCP
  milestone's own `check:` command failed under `goals.py verify` while passing
  when run directly -- the only difference being that `verify` held a
  transaction.
- **`goals.verify` held a write transaction across every check command.** Both
  fixes were confirmed by reverting them: four tests fail, and the suite takes
  44s instead of 1.6s because of the lock waits.
- **`tokens.verify` could raise into the HTTP handler.** It promised
  "never raises" but caught only `sqlite3.Error`; opening the store also
  makes a directory, so a full disk arrives as `OSError`. Found by the
  fail-closed test, which patched `connect` to raise `RuntimeError`.
- **A non-dict subscription body crashed `push.subscribe`**, reachable
  from the network as `{"subscription": "anything"}`.
- **`push.unsubscribe` by row id never matched**: the id was hashed a
  second time and compared against the digest of itself.
- **`pwa` imports `viz`, which the reporter bundle does not ship.**
  Caught by the import-closure test rather than on a teammate's laptop.
  The four webserver-only modules moved to the full bundle.

The crypto is the one thing that cannot be checked by agreeing with
itself, so `tests/test_push.CurveTest` asserts RFC 6979's published `r`
and `s` for P-256/SHA-256 on two messages. If the curve arithmetic, the
hash handling or the nonce derivation were wrong, they could not match.
