r"""Telling a phone that an agent is waiting.

The measured baseline has agents blocked on a human 62 times over nine days.
Every one of those is dead time nobody was told about, because the dashboard
is a page you have to decide to open.

**Where this runs matters.** "Hooks never touch the network and never block a
turn" is a constraint on this project's board, and a push is a network call.
So the hook that notices the block does what it always did -- write its
mailbox file and exit -- and the *dashboard server*, which is already a
long-running local process, watches that mailbox and sends. The thing that
must be fast stays fast; the thing that can be slow lives where slowness is
survivable.

**Messages carry no body.** Encrypting a Web Push payload needs AES-128-GCM,
which is not in the standard library and is not worth hand-writing. RFC 8030
allows an empty message, so the service worker is told only that *something*
happened and asks the dashboard what -- it is already a client of it. Where it
cannot reach the dashboard it shows generic text. That is the honest cost of
not writing a cipher.

Nothing here is allowed to raise into a caller. A dashboard that falls over
because a push service is down would be worse than no notifications at all.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request

import agent_alert
import store
import vapid

# How long the push service should hold a message for a phone that is off.
TTL_SECONDS = 6 * 3600

# Beyond this a subscription is treated as dead even without a 404/410 -- a
# phone that has been failing for this many attempts is not coming back.
MAX_FAILURES = 12

NETWORK_TIMEOUT = 10

# How often the watcher looks at the mailbox. An escalation is human-latency
# work; a few seconds either way is not worth a tighter loop.
POLL_SECONDS = 20

# Gone: the push service says this subscription will never work again.
_DEAD_STATUS = (404, 410)


def _sub_id(endpoint: str) -> str:
    return hashlib.sha256(str(endpoint or "").encode("utf-8")).hexdigest()[:32]


def subscribe(subscription: dict, label: str = "", conn=None) -> dict:
    """Record a browser's subscription. Re-subscribing updates in place."""
    # The body came off the network, so "a dict" is a hope rather than a fact.
    if not isinstance(subscription, dict):
        return {"ok": False, "error": "a subscription must be an object"}
    endpoint = str(subscription.get("endpoint") or "").strip()
    if not endpoint.startswith(("http://", "https://")):
        return {"ok": False, "error": "a subscription needs an https endpoint"}
    keys = (subscription or {}).get("keys") or {}

    own = conn is None
    conn = conn or store.connect()
    try:
        sid = _sub_id(endpoint)
        now = time.time()
        conn.execute(
            "insert into push_subs (id, endpoint, p256dh, auth, label, "
            " created_at) values (?,?,?,?,?,?) "
            "on conflict(id) do update set endpoint=excluded.endpoint, "
            " p256dh=excluded.p256dh, auth=excluded.auth, "
            " label=case when excluded.label<>'' then excluded.label "
            "            else push_subs.label end, "
            " failures=0, last_error=''",
            (sid, endpoint, str(keys.get("p256dh") or ""),
             str(keys.get("auth") or ""), str(label or ""), now))
        conn.commit()
        return {"ok": True, "id": sid}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        if own:
            conn.close()


def unsubscribe(endpoint_or_id: str, conn=None) -> bool:
    own = conn is None
    conn = conn or store.connect()
    try:
        # Three ways to name the same row, because the caller may hold the id
        # (from a listing) or the endpoint (from the browser). Matching only
        # the digest of whatever arrived meant an id never matched anything,
        # since it would be hashed a second time.
        value = str(endpoint_or_id or "")
        cur = conn.execute(
            "delete from push_subs where id=? or id=? or endpoint=?",
            (value, _sub_id(value), value))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def listing(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or store.connect()
    try:
        return [dict(r) for r in conn.execute(
            "select * from push_subs order by created_at desc")]
    finally:
        if own:
            conn.close()


def clear(conn=None) -> int:
    """Drop every subscription.

    Used when the keypair changes: every existing subscription was made
    against the old public key and can never succeed again, so they go at the
    same moment rather than failing one at a time over the following week.
    """
    own = conn is None
    conn = conn or store.connect()
    try:
        cur = conn.execute("delete from push_subs")
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def _post(endpoint: str, headers: dict) -> tuple[int, str]:
    """One POST to a push service. Returns (status, detail), never raises."""
    request = urllib.request.Request(endpoint, data=b"", method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as reply:
            return reply.status, ""
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        return exc.code, detail
    except Exception as exc:  # noqa: BLE001
        # URLError, socket timeout, DNS, a proxy refusing: all the same answer.
        return 0, str(exc)[:200]


def send_all(conn=None) -> dict:
    """Ring every subscription. Returns what happened, and never raises."""
    own = conn is None
    conn = conn or store.connect()
    try:
        subs = [dict(r) for r in conn.execute("select * from push_subs")]
        if not subs:
            return {"sent": 0, "dead": 0, "failed": 0, "subscriptions": 0}

        try:
            key = vapid.load()
        except Exception as exc:  # noqa: BLE001
            return {"sent": 0, "dead": 0, "failed": len(subs),
                    "subscriptions": len(subs), "error": str(exc)}

        sent = dead = failed = 0
        now = time.time()
        for sub in subs:
            try:
                headers = vapid.headers(sub["endpoint"], key=key,
                                        ttl=TTL_SECONDS)
            except ValueError:
                # The stored endpoint is not a URL, so it never will be.
                conn.execute("delete from push_subs where id=?", (sub["id"],))
                dead += 1
                continue

            status, detail = _post(sub["endpoint"], headers)
            if 200 <= status < 300:
                conn.execute(
                    "update push_subs set last_ok_at=?, failures=0, "
                    "last_error='' where id=?", (now, sub["id"]))
                sent += 1
            elif status in _DEAD_STATUS:
                # Gone for good. Deleting beats retrying something the service
                # has told us will never work.
                conn.execute("delete from push_subs where id=?", (sub["id"],))
                dead += 1
            else:
                failures = int(sub["failures"] or 0) + 1
                if failures >= MAX_FAILURES:
                    conn.execute("delete from push_subs where id=?",
                                 (sub["id"],))
                    dead += 1
                else:
                    conn.execute(
                        "update push_subs set failures=?, last_error=? "
                        "where id=?",
                        (failures, ("%s %s" % (status, detail)).strip(),
                         sub["id"]))
                    failed += 1
        conn.commit()
        return {"sent": sent, "dead": dead, "failed": failed,
                "subscriptions": len(subs)}
    except Exception as exc:  # noqa: BLE001
        return {"sent": 0, "dead": 0, "failed": 0, "subscriptions": 0,
                "error": str(exc)}
    finally:
        if own:
            conn.close()


def escalations() -> list[dict]:
    """Agents currently blocked on a human, newest first.

    Read straight from the local mailbox, not from the store: this has to be
    true *now*, and the mailbox is what the hook writes.
    """
    try:
        alerts = agent_alert.read_all(reap=False)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for alert in alerts:
        if getattr(alert, "kind", "") != agent_alert.NEEDS_INPUT:
            continue
        # An Alert carries a cwd, not a project name -- the same repository
        # walk the tile uses turns one into the other, so the notification
        # says what the board says rather than "a project".
        try:
            project = agent_alert.project_name(alert.cwd) if alert.cwd else ""
        except Exception:  # noqa: BLE001
            project = ""
        out.append({
            "project": project or alert.project_key.rsplit("/", 1)[-1]
            or "a project",
            "message": (getattr(alert, "message", "") or "").strip(),
            "at": float(getattr(alert, "at", 0) or 0),
            "sid": getattr(alert, "sid", ""),
            "tag": getattr(alert, "session_tag", ""),
            "age": getattr(alert, "age_text", ""),
        })
    out.sort(key=lambda row: -row["at"])
    return out


class Watcher:
    """Polls the mailbox and pushes once per escalation.

    "Once" is the whole job. A naive loop would re-send every poll for as long
    as the agent stayed blocked, which is twenty seconds apart and would train
    anyone to turn notifications off within the hour. An escalation is
    identified by its session and the moment it blocked, so the same wait is
    never announced twice -- while a *new* wait in the same session is.
    """

    def __init__(self, interval: float = POLL_SECONDS, sender=None,
                 source=None) -> None:
        self.interval = interval
        self._send = sender or send_all
        self._source = source or escalations
        self._announced: set[tuple] = set()
        self._thread = None
        self._stop = None

    def poll(self) -> dict:
        """One pass. Returns what it did, and never raises."""
        try:
            current = self._source()
        except Exception as exc:  # noqa: BLE001
            return {"new": 0, "error": str(exc)}

        live = {(row.get("sid", ""), round(float(row.get("at") or 0), 1))
                for row in current}
        fresh = live - self._announced
        # Forget waits that have ended, so the set cannot grow without bound
        # and so a session that blocks again is announced again.
        self._announced = live

        if not fresh:
            return {"new": 0}
        try:
            result = self._send()
        except Exception as exc:  # noqa: BLE001
            return {"new": len(fresh), "error": str(exc)}
        return {"new": len(fresh), **result}

    def prime(self) -> None:
        """Adopt the current escalations without announcing them.

        Called at startup: an agent that was already blocked before the server
        came up is not news, and notifying about it would make every restart
        buzz the phone.
        """
        try:
            self._announced = {
                (row.get("sid", ""), round(float(row.get("at") or 0), 1))
                for row in self._source()}
        except Exception:  # noqa: BLE001
            self._announced = set()

    def start(self) -> None:
        import threading

        if self._thread is not None:
            return
        self.prime()
        self._stop = threading.Event()

        def loop() -> None:
            while not self._stop.wait(self.interval):
                self.poll()

        # Daemon: this must never be the reason the tile will not close.
        self._thread = threading.Thread(target=loop, name="push-watcher",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        thread, self._thread, self._stop = self._thread, None, None
        if thread is not None:
            thread.join(timeout=2.0)


_USAGE = """usage: python push.py <command>

  key         print the VAPID public key a browser subscribes with
  list        every subscription
  send        send a test notification to all of them now
  escalations what is currently blocked on a human
  clear       drop every subscription
"""


def main(argv: list[str]) -> int:
    command = (argv[0] if argv else "").strip().lower()
    if command == "key":
        print(vapid.public_key())
        return 0
    if command == "list":
        rows = listing()
        if not rows:
            print("no subscriptions")
            return 0
        for row in rows:
            print("%s  %-18s fails=%-3d %s" % (
                row["id"][:12], row["label"][:18] or "-", row["failures"],
                row["endpoint"][:60]))
        return 0
    if command == "send":
        print(json.dumps(send_all(), indent=2))
        return 0
    if command == "escalations":
        rows = escalations()
        if not rows:
            print("nothing is blocked")
            return 0
        for row in rows:
            print("%-24s %s" % (row["project"], row["message"][:80]))
        return 0
    if command == "clear":
        print("dropped %d subscription(s)" % clear())
        return 0
    print(_USAGE)
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
