# Feature: scoped access tokens for the web dashboard

## Purpose

The web dashboard currently has one secret: `web_token`, a string in
`config.json`, all-or-nothing. Whoever holds it can read every project's
history **and** change the tile and trigger a sync. There is no way to hand
someone — or a phone, or a script — less than everything, and no way to take
one of them back without invalidating all of them.

That is tolerable while the only client is a browser on `127.0.0.1`. It stops
being tolerable at the next milestone, which puts this on a phone over the
network and adds a channel for answering an agent that is blocked. Answering an
agent is a *write into a running session*; a token that can do that should not
be the same token pinned to a home screen for glancing at a chart.

## Architecture

A new table and a new module. No new endpoint surface.

```
tokens table (store.db, schema v4)
  tid          8 hex chars, public, travels in the token and indexes the row
  hash         sha256 of the secret half -- the secret itself is never stored
  scopes       'read operate answer', space separated
  label        what it is for, so revoking the right one is possible
  created_at / expires_at / last_used_at / revoked_at

token string: "<tid>.<secret>"    e.g. 3f9a2c04.Xy7...  (43 chars of urlsafe)
                ^ lookup key        ^ 32 random bytes, shown once, never stored
```

`tokens.py` owns mint / verify / revoke / list and nothing else.
`webserver.py` gains a scope argument on its existing `_authorised` check.

### Scopes

| Scope | Lets the holder |
| --- | --- |
| `read` | GET any `/api/…` view and the static page |
| `operate` | POST `/api/tile` and `/api/sync` — change this machine's state |
| `answer` | reply to an agent that is blocked. **Reserved**: no endpoint consumes it yet; it arrives with the PWA milestone. It is defined and enforced now so that endpoint cannot be born unscoped. |

A token with no scopes is useless rather than unlimited, which is the safe
direction for a parsing bug.

### Why sha256 and not scrypt

These are not passwords. A token is 32 bytes from `secrets.token_urlsafe`, so
there is no dictionary to run and no human-memorable structure to exploit;
brute-forcing it is brute-forcing 256 bits. A password KDF buys nothing against
that and costs real time **on every request**, which is the wrong trade for a
check that runs on each poll of a dashboard. The secret is still never stored:
only its digest is, so a leaked `store.db` does not yield working tokens.

### Backward compatibility

`config.json`'s existing plaintext `web_token` keeps working and is treated as
carrying every scope. Breaking a working remote dashboard to tidy up the auth
model would be the wrong trade; it is reported as legacy by `tokens.py list` so
it can be replaced deliberately.

## Impact map

| Touched | What could break |
| --- | --- |
| `store.py` | Schema 3 → 4. `tokens` is not derivable from the event log, so it must be an additive `create table`, never a rebuild. A migration that drops it loses every issued token. |
| `webserver.py` | Every request path now resolves a scope. A mistake here either locks the owner out of their own dashboard or opens it up — the two failure directions are both bad, so both are tested. `local_only` must keep bypassing auth or the desktop case regresses. |
| `widget.py` | Holds the `web_token` default. Untouched, but the legacy path depends on it. |
| `dashboard.py` | Shows the dashboard URL. Unaffected: minting is a CLI action. |
| `web/app.js` | Sends `X-Dashboard-Token`. Unchanged — a scoped token goes in the same header. |
| `deploy.py`, `install.ps1` | Must ship `tokens.py`, or an installed copy imports a module that is not there. |
| `mcp_server.py` | Untouched. Tokens are not exposed as a tool: minting credentials is not something an agent should do mid-turn. |

## Edge cases and failure modes

| Case | Behaviour |
| --- | --- |
| Malformed token (no `.`, empty, 10 KB of junk) | Rejected, no exception, no table scan. |
| Unknown `tid` | Rejected in constant time relative to a known one — the hash comparison runs against a dummy digest so a timing difference cannot enumerate valid ids. |
| Correct `tid`, wrong secret | Rejected. `compare_digest`, never `==`. |
| Revoked token | Rejected even before expiry. |
| Expired token | Rejected. Expiry is checked against `time.time()`, and a token with `expires_at` of 0 never expires. |
| Token valid, scope missing | **403**, not 401 — the credential is fine, the permission is not, and conflating them makes a client retry forever. |
| No token at all, not local | 401. |
| `local_only` | No auth, as today. The point of the dashboard is that it opens with one click on the machine it monitors. |
| Two requests at once | `last_used_at` is a single UPDATE; SQLite serialises it. It is written on a separate short connection so a poll cannot hold a lock. |
| Clock moved backwards | Only affects expiry, in the lenient direction, which is survivable. |
| `store.db` unreadable | `verify` returns no scopes, so the dashboard denies rather than admits. Fail closed. |

## Acceptance criteria

1. A minted token round-trips: the string is returned once, and `verify`
   accepts it and returns exactly the scopes it was minted with.
2. The secret is not recoverable from the database — no row contains it.
3. A token minted `read` is refused `operate`, with 403 rather than 401.
4. Revoked and expired tokens are refused.
5. A malformed, empty or unknown token is refused without raising.
6. The legacy `config.json` token still works and carries every scope.
7. `local_only` still needs no token at all.
8. GET requires `read`; POST `/api/tile` and `/api/sync` require `operate`.
9. Scopes survive the schema migration from v3 with existing tokens intact.
10. `tokens.py` is in both install file lists.
