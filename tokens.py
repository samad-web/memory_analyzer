r"""Scoped access tokens for the web dashboard.

Until now the dashboard had one secret: ``web_token`` in ``config.json``,
all-or-nothing. Whoever held it could read every project's history *and*
change the tile and trigger a sync. That is tolerable while the only client is
a browser on ``127.0.0.1``. It stops being tolerable at the next milestone,
which puts this on a phone and adds a channel for answering an agent that is
blocked -- and answering an agent is a write into a running session. A token
pinned to a home screen for glancing at a chart should not be able to do that.

A token looks like ``3f9a2c04.Xy7...``:

* the half before the dot is public, indexes the row, and makes verification
  one lookup instead of a scan over every issued token;
* the half after it is 32 random bytes, shown once at mint time and never
  stored. Only its digest is, so a leaked ``store.db`` yields no credentials.

**Why sha256 and not a password KDF.** These are not passwords. There is no
dictionary to run and no human-memorable structure to exploit; brute-forcing
one is brute-forcing 256 bits of ``secrets.token_urlsafe``. scrypt would buy
nothing against that and would cost real time *on every request*, which is the
wrong trade for a check that runs on each poll of a dashboard.

Everything fails closed. An unreadable database, a malformed token and an
unknown id all produce no scopes, so the caller denies rather than admits.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
import time

import store

# The three things a holder can be allowed to do.
#
# `answer` has no endpoint yet -- it arrives with the PWA milestone, which adds
# the channel for replying to a blocked agent. It is defined and enforced now
# so that endpoint cannot be born unscoped.
READ = "read"
OPERATE = "operate"
ANSWER = "answer"
SCOPES = (READ, OPERATE, ANSWER)

TID_BYTES = 4           # 8 hex characters: enough to index, not a secret
SECRET_BYTES = 32
_SEPARATOR = "."

# A token string longer than this is not one, and hashing it would be work
# done on behalf of whoever sent it.
MAX_TOKEN_CHARS = 200

# Compared against when the id is unknown, so a miss costs the same as a hit
# and the timing cannot be used to enumerate valid ids.
_DUMMY_HASH = hashlib.sha256(b"no such token").hexdigest()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_scopes(scopes) -> list[str]:
    """Normalise a scope list, dropping anything unrecognised.

    An unknown scope is discarded rather than kept, so a typo narrows the
    token instead of silently creating a permission nobody checks.
    """
    if isinstance(scopes, str):
        scopes = scopes.replace(",", " ").split()
    out = []
    for scope in scopes or ():
        scope = str(scope).strip().lower()
        if scope in SCOPES and scope not in out:
            out.append(scope)
    return out


def mint(scopes, label: str = "", days: float = 0, conn=None) -> dict:
    """Issue a token. The secret is in the return value and nowhere else.

    ``days`` of 0 means it does not expire, which is right for the token on
    your own phone and wrong for one you are handing to somebody else.
    """
    wanted = clean_scopes(scopes)
    if not wanted:
        raise ValueError("a token needs at least one of: %s" % ", ".join(SCOPES))

    own = conn is None
    conn = conn or store.connect()
    try:
        now = time.time()
        for _ in range(8):
            tid = secrets.token_hex(TID_BYTES)
            if not conn.execute(
                    "select 1 from tokens where tid=?", (tid,)).fetchone():
                break
        else:                                   # pragma: no cover - 2^-224
            raise RuntimeError("could not find a free token id")

        secret = secrets.token_urlsafe(SECRET_BYTES)
        conn.execute(
            "insert into tokens (tid, hash, scopes, label, created_at, "
            " expires_at) values (?,?,?,?,?,?)",
            (tid, _hash(secret), " ".join(wanted), str(label or ""), now,
             now + days * 86400 if days else 0))
        conn.commit()
        return {
            "token": tid + _SEPARATOR + secret,
            "tid": tid,
            "scopes": wanted,
            "label": str(label or ""),
            "expires_at": now + days * 86400 if days else 0,
        }
    finally:
        if own:
            conn.close()


def verify(token: str, conn=None) -> list[str]:
    """The scopes this token carries, or ``[]``.

    Never raises and never admits on error: an unreadable database, a
    malformed string and an unknown id all come back empty.
    """
    token = str(token or "")
    if not token or len(token) > MAX_TOKEN_CHARS or _SEPARATOR not in token:
        return []
    tid, _, secret = token.partition(_SEPARATOR)
    if not tid or not secret:
        return []

    own = conn is None
    try:
        conn = conn or store.connect()
    except Exception:  # noqa: BLE001
        # Not just sqlite3.Error: opening the store also makes a directory and
        # touches the filesystem, so a full disk or a vanished path arrives as
        # OSError. Anything at all here means "no scopes" -- the caller denies.
        return []
    try:
        row = conn.execute(
            "select * from tokens where tid=?", (tid,)).fetchone()
        # Hash and compare even when there is no such row, so that a miss and
        # a wrong secret take the same time and valid ids cannot be probed.
        expected = row["hash"] if row else _DUMMY_HASH
        matched = secrets.compare_digest(_hash(secret), str(expected))
        if not row or not matched:
            return []

        now = time.time()
        if row["revoked_at"]:
            return []
        if row["expires_at"] and now >= row["expires_at"]:
            return []

        try:
            conn.execute("update tokens set last_used_at=? where tid=?",
                         (now, tid))
            conn.commit()
        except Exception:  # noqa: BLE001
            # Knowing when it was last used is a convenience, not a condition
            # of the token being valid. A read-only database must not cost
            # someone their access.
            pass
        return clean_scopes(row["scopes"])
    except Exception:  # noqa: BLE001
        return []
    finally:
        if own:
            conn.close()


def allows(token: str, scope: str, conn=None) -> bool:
    return scope in verify(token, conn=conn)


def revoke(tid: str, conn=None) -> bool:
    """Withdraw one token. Kept as a row so the audit trail survives it."""
    own = conn is None
    conn = conn or store.connect()
    try:
        cur = conn.execute(
            "update tokens set revoked_at=? where tid=? and revoked_at=0",
            (time.time(), str(tid or "")))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def listing(include_revoked: bool = False, conn=None) -> list[dict]:
    """Every token, without any secret in it."""
    own = conn is None
    conn = conn or store.connect()
    try:
        sql = "select * from tokens "
        if not include_revoked:
            sql += "where revoked_at=0 "
        sql += "order by created_at desc"
        now = time.time()
        out = []
        for row in conn.execute(sql):
            row = dict(row)
            row.pop("hash", None)
            row["scopes"] = clean_scopes(row["scopes"])
            row["expired"] = bool(row["expires_at"]
                                  and now >= row["expires_at"])
            row["revoked"] = bool(row["revoked_at"])
            out.append(row)
        return out
    finally:
        if own:
            conn.close()


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


_USAGE = """usage: python tokens.py <command> [options]

  mint SCOPE[,SCOPE...] [--label TEXT] [--days N]
              issue a token and print it once
  list [--all]
              every live token (--all includes revoked ones)
  revoke TID  withdraw one token

Scopes: read (view the dashboard), operate (change the tile, trigger a sync),
        answer (reply to a blocked agent -- reserved for the PWA milestone).
"""


def main(argv: list[str]) -> int:
    args = list(argv)
    label, days = "", 0.0
    for flag, cast in (("--label", str), ("--days", float)):
        if flag in args:
            i = args.index(flag)
            value = args[i + 1] if i + 1 < len(args) else ""
            del args[i:i + 2]
            try:
                if flag == "--label":
                    label = cast(value)
                else:
                    days = cast(value)
            except ValueError:
                print("%s needs a number" % flag)
                return 2
    include_revoked = "--all" in args
    args = [a for a in args if a != "--all"]

    command = args[0] if args else ""
    if command == "mint":
        if len(args) < 2:
            print(_USAGE)
            return 2
        try:
            issued = mint(args[1], label=label, days=days)
        except ValueError as exc:
            print(exc)
            return 2
        print(issued["token"])
        print()
        print("scopes: %s   expires: %s" % (
            " ".join(issued["scopes"]), _when(issued["expires_at"])))
        print("Copy it now -- it is not stored and cannot be shown again.")
        return 0
    if command == "list":
        rows = listing(include_revoked=include_revoked)
        if not rows:
            print("no tokens issued")
            return 0
        print("%-10s %-22s %-22s %-17s %s" % (
            "id", "scopes", "label", "last used", "state"))
        for row in rows:
            state = ("revoked" if row["revoked"] else
                     "expired" if row["expired"] else "live")
            print("%-10s %-22s %-22s %-17s %s" % (
                row["tid"], " ".join(row["scopes"]), row["label"][:22],
                _when(row["last_used_at"]), state))
        return 0
    if command == "revoke":
        if len(args) < 2:
            print(_USAGE)
            return 2
        print("revoked" if revoke(args[1]) else "no live token with that id")
        return 0
    print(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
