r"""HTTPS client for the SirahOS backend.

The bridge aggregates Claude Code turns per (IST day, project folder) and posts
one WorkLog each. This module is the thin layer in between: login, project
list, worklog create -- nothing cleverer.

Why stdlib and not ``requests``: the project's constraint is no new runtime
dependencies. ``urllib.request`` is enough for a small REST client, and the
cost of writing one is a few helpers below.

Why a JWT, not a scoped token: SirahOS already issues JWTs on
``POST /api/auth/login``. Each teammate signs in from the tile with their own
email+password and the resulting ``accessToken`` is what the bridge uses.
That makes the server see the WorkLog as being created by *that user*, which is
what makes XP, streaks and project-membership checks come out right.

The token is held in memory by the running bridge and persisted to disk by the
caller (via DPAPI on Windows). This module never writes it.

Nothing here retries on failure. The caller decides whether a failed push
should be re-attempted on the next tick; retrying inside the request would hide
"SirahOS is down" from the dashboard.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


# Hard caps so a buggy server, or a buggy client thinking it is talking to one,
# cannot wedge the scheduled task forever.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0

# SirahOS runs on a modern HTTPS stack. We stop talking to anything older than
# TLS 1.2, which is already the Python default on current systems; this is
# explicit so an upgrade to an older stdlib does not quietly weaken it.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.minimum_version = ssl.TLSVersion.TLSv1_2


class SirahOSError(Exception):
    """Any failure talking to SirahOS. Carries the HTTP status when known."""

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(SirahOSError):
    """401 or an expired JWT. The caller surfaces a "sign back in" banner."""


def _request(
    method: str,
    url: str,
    body: dict | None = None,
    token: str = "",
    extra_headers: dict | None = None,
) -> tuple[int, dict]:
    """One round-trip. Returns ``(status, parsed_json)``.

    A non-2xx with a JSON body is still parsed; the caller decides what to do
    with it. A non-JSON body (503 HTML page from a proxy, say) returns an empty
    dict and the status, which is enough to tell "retry" from "give up".
    """
    data = None
    headers = {"Accept": "application/json", "User-Agent": "memory-analyzer-bridge/1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=READ_TIMEOUT, context=_SSL_CTX
        ) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        # 4xx/5xx: we still want the body so the UI can show what SirahOS said.
        raw = e.read() or b""
        status = e.code
    except urllib.error.URLError as e:
        # DNS, TLS, refused connection. No HTTP status to speak of.
        raise SirahOSError("network: %s" % e.reason) from e
    except TimeoutError as e:
        raise SirahOSError("timed out") from e

    parsed: dict = {}
    if raw:
        try:
            parsed_any = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(parsed_any, dict):
                parsed = parsed_any
            elif isinstance(parsed_any, list):
                # List endpoints (projects, worklogs) wrap under a single key
                # so callers can type-check on a dict shape uniformly.
                parsed = {"items": parsed_any}
        except ValueError:
            parsed = {"_raw": raw[:400].decode("utf-8", errors="replace")}

    return status, parsed


def _api_url(base: str, path: str) -> str:
    """Join a user-supplied base to a known relative path.

    The base may be ``https://sirahos.example.com`` or
    ``https://sirahos.example.com/`` or even ``.../api``; we normalise so a
    typo in config does not land requests on the wrong path.
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        raise SirahOSError("SirahOS API URL not configured")
    if not base.lower().startswith(("http://", "https://")):
        raise SirahOSError("SirahOS API URL must be http(s)://…")
    if base.lower().endswith("/api"):
        base = base[:-4]
    return base + path


# ---------------------------------------------------------------- auth ------


def login(base_url: str, email: str, password: str) -> dict:
    """Exchange email+password for an access token and the user profile.

    Returns ``{"accessToken": str, "user": {...}}`` on success.

    Raises ``AuthError`` on 401 ("invalid email or password") and 403
    (account deactivated), ``SirahOSError`` on anything else.
    """
    url = _api_url(base_url, "/api/auth/login")
    status, body = _request("POST", url, {"email": email, "password": password})
    if status == 200 and body.get("accessToken"):
        return {
            "accessToken": body["accessToken"],
            "user": body.get("user") or {},
        }
    if status == 401:
        raise AuthError(
            body.get("error") or "Invalid email or password",
            status=status,
            body=json.dumps(body),
        )
    if status == 403:
        raise AuthError(
            body.get("error") or "Account is deactivated",
            status=status,
            body=json.dumps(body),
        )
    if status == 429:
        # Per-account rate limit; surface the server's message so the user
        # knows to wait, not to re-enter their password harder.
        raise SirahOSError(
            body.get("error") or "Too many login attempts",
            status=status,
            body=json.dumps(body),
        )
    raise SirahOSError(
        "login failed (http %d)" % status,
        status=status,
        body=json.dumps(body)[:400],
    )


def me(base_url: str, token: str) -> dict:
    """The signed-in user's profile.

    SirahOS does not expose ``GET /me`` directly, so we call a cheap authed
    endpoint and let the auth middleware answer "who are you". The projects
    list is the natural pick: it is already authed, scoped to the user's
    company, and is also what the mapping UI needs anyway.

    Returns an empty dict when the token is missing. Raises ``AuthError`` on
    401, which the UI treats as "sign back in".
    """
    if not token:
        return {}
    status, body = _request(
        "GET", _api_url(base_url, "/api/projects/selectable"), token=token
    )
    if status == 401:
        raise AuthError("session expired", status=status)
    if status == 200:
        # The response is the project list; the auth middleware already
        # validated the token by this point, so the token itself is live.
        items = body.get("items", []) if "items" in body else []
        return {"ok": True, "projects_sample_count": len(items)}
    raise SirahOSError(
        "SirahOS said http %d on auth check" % status, status=status
    )


# ---------------------------------------------------------- projects ------


def list_projects(base_url: str, token: str) -> list[dict]:
    """Projects the signed-in user may log work against.

    Shape: ``[{"id": "...", "name": "...", "client": "...", "status": "..."}]``.
    Empty list on 200 with no projects. ``AuthError`` on 401.
    """
    status, body = _request(
        "GET", _api_url(base_url, "/api/projects/selectable"), token=token
    )
    if status == 401:
        raise AuthError("session expired", status=status)
    if status != 200:
        raise SirahOSError(
            "could not list projects (http %d)" % status, status=status
        )
    items = body.get("items", []) if "items" in body else []
    out: list[dict] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        name = p.get("name")
        if not pid or not name:
            continue
        out.append({
            "id": str(pid),
            "name": str(name),
            "client": str(p.get("client") or ""),
            "status": str(p.get("status") or ""),
        })
    return out


# ------------------------------------------------------------- worklogs ----


def create_worklog(
    base_url: str,
    token: str,
    *,
    date: str,
    hours: float,
    notes: str,
    project_id: str = "",
    start_time: str = "",
    end_time: str = "",
) -> dict:
    """Create one WorkLog for the signed-in user.

    Mirrors ``POST /api/worklogs`` on SirahOS: a date (``YYYY-MM-DD``), a
    project id (omit for General), either ``hours`` or ``start_time``+``end_time``
    (``HH:MM``), and a note. SirahOS computes XP and advances the user's streak.

    Returns the created WorkLog as the server shaped it (includes ``id``).
    """
    payload: dict = {"notes": notes, "date": date}
    if project_id:
        payload["projectId"] = project_id
    if start_time and end_time:
        payload["startTime"] = start_time
        payload["endTime"] = end_time
    elif hours > 0:
        payload["hours"] = round(hours, 2)

    status, body = _request(
        "POST", _api_url(base_url, "/api/worklogs"), body=payload, token=token
    )
    if status == 401:
        raise AuthError("session expired", status=status)
    if status == 201:
        return body
    # 400 bodies carry zod's details; surface a short message so the dashboard
    # does not need to interpret the shape.
    err = body.get("error") or "create failed"
    details = body.get("details") or ""
    raise SirahOSError(
        "%s (http %d)" % (err, status),
        status=status,
        body=(json.dumps(details)[:400] if details else json.dumps(body)[:400]),
    )


def delete_worklog(base_url: str, token: str, worklog_id: str) -> None:
    """Remove a WorkLog the bridge created. Idempotent on 404."""
    url = _api_url(base_url, "/api/worklogs/" + urllib.parse.quote(worklog_id))
    status, _ = _request("DELETE", url, token=token)
    if status in (200, 204, 404):
        return
    if status == 401:
        raise AuthError("session expired", status=status)
    raise SirahOSError("delete failed (http %d)" % status, status=status)


def list_my_worklogs(
    base_url: str,
    token: str,
    *,
    user_id: str = "",
    date: str = "",
) -> list[dict]:
    """What SirahOS says about this user for a given day. Used to detect that
    the user deleted the bridge's own WorkLog in the web UI."""
    q: dict[str, str] = {}
    if user_id:
        q["userId"] = user_id
    if date:
        q["date"] = date
    query = ("?" + urllib.parse.urlencode(q)) if q else ""
    url = _api_url(base_url, "/api/worklogs" + query)
    status, body = _request("GET", url, token=token)
    if status == 401:
        raise AuthError("session expired", status=status)
    if status != 200:
        raise SirahOSError(
            "could not list worklogs (http %d)" % status, status=status
        )
    items = body.get("items", []) if "items" in body else []
    return [i for i in items if isinstance(i, dict)]
