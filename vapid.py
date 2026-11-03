r"""VAPID: the signature that lets a push service believe us.

Web Push requires the sender to prove it is the same sender as last time, with
an ES256 JWT -- ECDSA over NIST P-256. The standard library has no elliptic
curve, and "no new runtime dependencies" is a constraint on this project's
board, so the curve is here. It is about a hundred lines of integer
arithmetic.

**Hand-writing crypto is normally the wrong call**, so the reasons this one is
acceptable are worth stating rather than assuming:

* It is **signing only**, with a key this machine generated, for this machine.
  No key agreement, no confidentiality. A VAPID JWT is public by design -- it
  carries no secret, it only proves continuity of sender.
* The way hand-written ECDSA actually fails is a repeated or biased nonce,
  which leaks the private key outright. That failure is removed rather than
  mitigated: ``k`` is derived by **RFC 6979**, deterministically, by HMAC from
  the message and the key. There is no randomness in a signature at all.
* It is checkable against someone else's arithmetic. RFC 6979 A.2.5 publishes
  exact ``r`` and ``s`` for P-256/SHA-256, and the tests assert those numbers,
  so this is verified against the standard rather than against itself.
* Local timing is not in the threat model. Anyone who can time this process
  can read ``vapid.json`` off the disk beside it.

What this does *not* do is encrypt a payload -- that would need AES-GCM, which
is not here and is not written by hand either. Push messages from this project
carry no body; see ``push.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

import events

# NIST P-256 (secp256r1). These are the curve's published parameters.
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = -3 % P
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

KEY_PATH = os.path.join(events.ROOT, "vapid.json")

# How long a signed token stays good. Far beyond any plausible clock skew, and
# well inside the 24 hours the spec permits.
TOKEN_LIFETIME = 12 * 3600

# Who the push service should complain to. Not an email that is checked; it is
# a required field and a hostname is the honest answer for a local tool.
DEFAULT_SUBJECT = "mailto:dashboard@localhost"


# ------------------------------------------------------------ the curve --
#
# Points are (x, y) or None for the point at infinity. Jacobian coordinates
# would be faster; at one signature per push, plain affine arithmetic with
# Python's big integers is fast enough and much easier to read.


def _inverse(value: int, modulus: int) -> int:
    return pow(value, -1, modulus)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        slope = (3 * x1 * x1 + A) * _inverse(2 * y1, P) % P
    else:
        slope = (y2 - y1) * _inverse(x2 - x1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return (x3, (slope * (x1 - x3) - y1) % P)


def _multiply(k: int, point):
    """Double-and-add. Not constant time, which is fine here -- see the
    module docstring on the threat model."""
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


# -------------------------------------------------------------- RFC 6979 --


def _bits2int(data: bytes) -> int:
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - N.bit_length()
    return value >> excess if excess > 0 else value


def _int2octets(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _bits2octets(data: bytes) -> bytes:
    z1 = _bits2int(data)
    z2 = z1 - N
    return _int2octets(z2 if z2 >= 0 else z1)


def _deterministic_k(digest: bytes, private: int) -> int:
    """RFC 6979 section 3.2. The whole reason this file is defensible.

    A nonce that repeats across two signatures reveals the private key by
    simple algebra, and a biased one reveals it over enough samples. Deriving
    it from the key and the message means it cannot repeat for different
    messages and cannot be biased by a weak random source.
    """
    key = b"\x00" * 32
    value = b"\x01" * 32
    seed = _int2octets(private) + _bits2octets(digest)

    for prefix in (b"\x00", b"\x01"):
        key = hmac.new(key, value + prefix + seed, hashlib.sha256).digest()
        value = hmac.new(key, value, hashlib.sha256).digest()

    while True:
        value = hmac.new(key, value, hashlib.sha256).digest()
        candidate = _bits2int(value)
        if 1 <= candidate < N:
            return candidate
        key = hmac.new(key, value + b"\x00", hashlib.sha256).digest()
        value = hmac.new(key, value, hashlib.sha256).digest()


def sign(message: bytes, private: int) -> bytes:
    """An ES256 signature: r and s, 32 big-endian bytes each.

    JWS wants the raw pair, not the DER wrapper X.509 uses.
    """
    digest = hashlib.sha256(message).digest()
    z = _bits2int(digest)
    while True:
        k = _deterministic_k(digest, private)
        point = _multiply(k, (GX, GY))
        r = point[0] % N
        if r == 0:
            continue                       # pragma: no cover - 2^-128
        s = (_inverse(k, N) * (z + r * private)) % N
        if s == 0:
            continue                       # pragma: no cover - 2^-128
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def public_point(private: int) -> tuple[int, int]:
    return _multiply(private, (GX, GY))


def verify(message: bytes, signature: bytes, point) -> bool:
    """Check a signature. Not needed to send a push -- it exists so the tests
    can prove the signer is self-consistent as well as matching the vector."""
    if len(signature) != 64 or point is None:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    z = _bits2int(hashlib.sha256(message).digest())
    w = _inverse(s, N)
    candidate = _add(_multiply(z * w % N, (GX, GY)), _multiply(r * w % N, point))
    return candidate is not None and candidate[0] % N == r


# ----------------------------------------------------------- the keypair --


def b64(raw: bytes) -> str:
    """base64url with the padding stripped, which is what JOSE wants."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate() -> dict:
    """A fresh keypair. ``secrets`` for the key itself -- the determinism
    above is about the per-signature nonce, not about the key."""
    private = secrets.randbelow(N - 1) + 1
    x, y = public_point(private)
    return {
        "private": private,
        # Uncompressed point, which is the form a browser's
        # `applicationServerKey` expects.
        "public": b64(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")),
        "created_at": time.time(),
    }


def load(path: str = "") -> dict:
    """The machine's keypair, created on first use.

    A missing or unreadable file means a new key, which invalidates every
    existing subscription. ``push.py`` clears them at the same moment rather
    than leaving them to fail one at a time.
    """
    path = path or KEY_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        private = int(data["private"])
        if not 1 <= private < N:
            raise ValueError("private key out of range")
        # Recomputed rather than trusted: a public half that does not match
        # the private half would produce signatures nothing can verify.
        x, y = public_point(private)
        return {"private": private, "public": b64(
            b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")),
            "created_at": data.get("created_at", 0), "fresh": False}
    except (OSError, ValueError, KeyError, TypeError):
        pass

    key = generate()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(key, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass                # best effort; Windows ACLs are not POSIX modes
    key["fresh"] = True
    return key


def public_key(path: str = "") -> str:
    return load(path)["public"]


# ---------------------------------------------------------------- the JWT --


def audience(endpoint: str) -> str:
    """The scheme and host of the push service, which is what ``aud`` means
    here -- not the full subscription URL."""
    parts = urllib.parse.urlsplit(str(endpoint or ""))
    if not parts.scheme or not parts.netloc:
        raise ValueError("endpoint is not a URL: %r" % (endpoint,))
    return "%s://%s" % (parts.scheme, parts.netloc)


def token(endpoint: str, subject: str = DEFAULT_SUBJECT, key=None,
          now: float = 0) -> str:
    """A signed ES256 JWT for one push service."""
    key = key or load()
    now = now or time.time()
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {
        "aud": audience(endpoint),
        "exp": int(now + TOKEN_LIFETIME),
        "sub": subject or DEFAULT_SUBJECT,
    }
    signing_input = "%s.%s" % (
        b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        b64(json.dumps(claims, separators=(",", ":")).encode("utf-8")))
    signature = sign(signing_input.encode("ascii"), key["private"])
    return "%s.%s" % (signing_input, b64(signature))


def headers(endpoint: str, subject: str = DEFAULT_SUBJECT, key=None,
            ttl: int = 3600) -> dict:
    """Everything a payload-less push needs on the wire.

    ``Content-Length: 0`` is explicit because some push services reject a POST
    without it, and ``Urgency`` is set high: the whole point of this message is
    that somebody is waiting.
    """
    key = key or load()
    return {
        "Authorization": "vapid t=%s, k=%s" % (
            token(endpoint, subject, key), key["public"]),
        "TTL": str(int(ttl)),
        "Urgency": "high",
        "Content-Length": "0",
    }
