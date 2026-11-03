# Feature: PWA shell and push

## Purpose

The dashboard is a page you have to decide to open. That is fine for the
Scorecard, which you read when you feel like it, and useless for the one thing
that is actually time-critical: an agent sitting blocked, waiting for a human.
The measured baseline has 62 of those over nine days. Each one is dead time
that nobody is told about.

This milestone makes the dashboard installable on a phone and makes an
escalation arrive without the phone being asked.

## The constraint problem, and how far push can honestly go

Three board constraints collide here, so the design is mostly a consequence of
them rather than a preference.

**1. "Hooks never touch the network and never block a turn."**
A push is a network call. So the push cannot be sent by the hook that notices
the agent is blocked. The hook writes its mailbox file and exits, as it always
has; the **dashboard server** — already a long-running local process — watches
the mailbox and sends. This is not a workaround, it is the correct split: the
thing that must be fast stays fast, and the thing that can be slow lives where
slowness is survivable.

**2. "No new runtime dependencies."**
Web Push (RFC 8030/8291/8292) needs two primitives the standard library does
not have:

| Needed for | Primitive | In stdlib? |
| --- | --- | --- |
| VAPID JWT (`ES256`) | ECDSA over P-256 | no |
| Encrypted payload | AES-128-GCM | no |

The payload requirement is **avoidable**. RFC 8030 allows a push message with
no body at all. The service worker then asks the dashboard what happened —
it is already a client of it — and falls back to a generic "an agent needs
you" if that fetch fails. So no AES is written here.

The VAPID requirement is **not** avoidable: both FCM and Mozilla's autopush
reject an unauthenticated subscription. So `vapid.py` implements P-256 ECDSA
signing in pure Python, in about 120 lines of integer arithmetic.

**Writing your own crypto is normally the wrong call and this is worth being
explicit about.** What makes it acceptable here, specifically:

* It is **signing only**, with a key this machine generated, for this machine.
  There is no key exchange and no confidentiality requirement — a VAPID JWT is
  public by design, it only proves the sender is the same sender as last time.
* The real failure mode in hand-written ECDSA is a repeated or biased nonce,
  which leaks the private key. That is eliminated outright by **RFC 6979
  deterministic `k`**, derived by HMAC-SHA256 from stdlib `hmac`. There is no
  randomness in the signature at all.
* It is checkable. RFC 6979 publishes exact signatures for P-256/SHA-256, so
  the implementation is tested against a known-answer vector rather than
  against itself.
* The threat model does not include local timing attacks: an attacker who can
  time this process can read the private key off the disk beside it.

**3. "Nothing merges to master unattended"** is untouched — this notifies, it
does not act.

### What this honestly does not do

Stated here so the milestone is not read as more than it is:

* **iOS needs the PWA added to the Home Screen first.** Safari will not deliver
  push to a tab. Nothing can be done about that from this side.
* **The notification body is generic unless the phone can reach the
  dashboard.** That is the price of sending no payload, and it is the right
  price: the alternative is hand-rolled AES-GCM.
* **A push service is a third party.** Chrome's is Google's. No prompt text
  ever travels through it — the push body is empty — but the fact that *a*
  notification happened does.

## Architecture

```
agent blocked
  |  Notification hook (local, instant, no network)  -- unchanged
  v
alert mailbox (agent_alert)
  |  watcher thread, inside the dashboard server
  v
push.notify_escalations()
  |  vapid.authorization()        <- ES256 JWT, deterministic k
  v
push service (FCM / autopush)     <- body is EMPTY
  |
  v
sw.js 'push' event
  |  fetch /api/escalations  -- or fall back to generic text
  v
notification -> tap -> dashboard, already open on the right project
```

| Module | Owns |
| --- | --- |
| `vapid.py` | P-256, RFC 6979, ES256 JWT, the keypair on disk |
| `push.py` | subscriptions table, sending, pruning, the escalation watcher |
| `web/sw.js` | offline shell, push handling, notification click |
| `web/manifest.webmanifest` | installability |
| `webserver.py` | the new routes, and starting the watcher |

### Data model (schema 4 -> 5, additive)

```
push_subs
  id           endpoint digest, so re-subscribing updates rather than duplicates
  endpoint     the push service URL
  p256dh/auth  the browser's keys. Stored but unused while payloads are empty;
               kept because dropping them would mean re-subscribing every
               client the day a payload is added.
  label, created_at, last_ok_at, last_error, failures
```

### Routes

| Route | Scope | Why |
| --- | --- | --- |
| `GET /manifest.webmanifest` | none | A manifest is fetched before any token exists and contains nothing private. |
| `GET /sw.js` | none | Same, and a service worker must be served from the origin root. |
| `GET /icon-<n>.png` | none | Generated from `viz`, contains nothing. |
| `GET /api/push/key` | `read` | The VAPID public key. Public by definition, but there is no reason to hand it to an unauthenticated caller. |
| `POST /api/push/subscribe` | `read` | Being notified about the board is a read channel, not an operation on the machine. |
| `POST /api/push/test` | `operate` | Sends a real notification. That is an action. |
| `GET /api/escalations` | `read` | What the service worker fetches to fill in the text. |

## Impact map

| Touched | What could break |
| --- | --- |
| `store.py` | Schema 4 -> 5. Additive, like `tokens`: a subscription cannot be regenerated. |
| `webserver.py` | Three unauthenticated routes. Each must be inert — a mistake here is a hole, so a test asserts exactly which paths skip auth and that nothing else does. The watcher thread must not keep the process alive or stop a clean shutdown. |
| `web/index.html`, `app.js` | Registering the service worker. A failed registration must not break the page, which is the normal state on plain HTTP. |
| `agent_alert.py` | Read only, by the watcher. |
| Hooks | **Unchanged.** If this feature required a hook change it would be wrong. |
| `deploy.py`, `install.ps1` | Must ship `vapid.py`, `push.py` and the new web assets. |

## Edge cases and failure modes

| Case | Behaviour |
| --- | --- |
| No subscriptions | The watcher does nothing and costs one directory listing. |
| Push service returns 404/410 | The subscription is dead. Deleted, not retried. |
| Push service returns 429/5xx | Counted, kept, retried next time. |
| Push service unreachable | Caught. A dashboard must not fall over because Google is down. |
| Same escalation seen twice | Sent once. The watcher remembers what it has already notified, keyed on session and the time it blocked. |
| Agent unblocks before delivery | Harmless: the SW's fetch returns an empty list and it says so. |
| Service worker on plain HTTP | Browsers only allow SW on HTTPS or `localhost`. Registration fails, the page works, and the UI says why rather than silently doing nothing. |
| Clock skew | The VAPID JWT `exp` is 12 hours out, far beyond any plausible skew. |
| Private key missing or corrupt | Regenerated. Every existing subscription is then invalid, so they are cleared at the same time rather than left to fail one by one. |
| Two servers, one database | The `id` is the endpoint digest, so a re-subscribe updates in place. |

## Acceptance criteria

1. `manifest.webmanifest` validates: name, `start_url`, `display: standalone`,
   and icons at 192 and 512 that are real, decodable PNGs.
2. The service worker is served from the root with a JavaScript content type.
3. The three unauthenticated routes are exactly the three listed, and every
   other route still demands a token.
4. A generated VAPID keypair round-trips, and a signature verifies against the
   **RFC 6979 known-answer vector** for P-256/SHA-256.
5. An `Authorization: vapid` header is well-formed: three dot-separated
   base64url segments, `alg: ES256`, an `aud` matching the endpoint's origin,
   and an `exp` in the future.
6. Subscribing twice with the same endpoint leaves one row.
7. A push service answering 410 deletes the subscription; 500 keeps it.
8. One escalation produces one notification, not one per poll.
9. `GET /api/escalations` lists blocked agents and needs `read`.
10. The whole send path, with the network stubbed, never raises.
