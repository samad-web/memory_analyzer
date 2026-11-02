r"""Supabase transport for team telemetry, over the stdlib alone.

Supabase is a REST API -- PostgREST for the tables, GoTrue for auth -- so the
official SDK would only be wrapping ``urllib``. Skipping it keeps a reporter
install at zero dependencies beyond ``psutil``, which matters: teammates who
only report (no tile, no dashboard) should not need PySide6 or a pinned SDK
tree on their laptop.

Two roles, two credentials, because the data includes prompt text:

* **Reporter** -- every laptop, holding the project's anon key. RLS lets that
  key ``INSERT`` and nothing else (see ``schema.sql``), so a key lifted off a
  laptop can add noise but cannot read a single row back.
* **Viewer** -- whoever opens the dashboard, signing in with a Supabase Auth
  email and password. ``SELECT`` is granted to the ``authenticated`` role only,
  scoped to their own team.

Every call returns ``Result(ok, data, error)`` instead of raising. This runs
behind hooks and a background timer where an exception would either break the
agent being measured or kill the sync thread, and a laptop that is simply
offline is the normal case, not an error worth surfacing.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import identity

SESSION_PATH = os.path.join(identity.STATE_DIR, "viewer_session.json")

TIMEOUT = 15
# An access token is refreshed this many seconds before it actually expires, so
# a long-lived dashboard never makes a request with a token that dies in flight.
REFRESH_MARGIN = 120


@dataclass
class Result:
    ok: bool
    data: object = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Backend:
    """A Supabase project, addressed as a reporter or as a viewer."""

    url: str
    anon_key: str
    team_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0

    # -- construction ------------------------------------------------------

    @classmethod
    def from_team_config(cls, cfg: dict | None = None) -> "Backend":
        cfg = cfg or identity.team_config()
        return cls(
            url=str(cfg.get("url") or "").strip().rstrip("/"),
            anon_key=str(cfg.get("anon_key") or "").strip(),
            team_id=str(cfg.get("team_id") or "").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key)

    # -- raw request -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: object = None,
        params: dict | None = None,
        prefer: str = "",
        as_viewer: bool = False,
    ) -> Result:
        if not self.configured:
            return Result(False, None, "not configured")

        target = f"{self.url}{path}"
        if params:
            target += "?" + urllib.parse.urlencode(params, safe="(),.*:")

        token = self.anon_key
        if as_viewer:
            if not self._ensure_token():
                return Result(False, None, "not signed in")
            token = self.access_token

        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer

        request = urllib.request.Request(
            target, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return Result(False, None, f"http {exc.code}: {detail or exc.reason}")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            # Offline, DNS down, captive portal. Expected, not exceptional.
            return Result(False, None, f"offline: {exc}")

        if not raw.strip():
            return Result(True, None, "")
        try:
            return Result(True, json.loads(raw), "")
        except ValueError:
            return Result(True, raw, "")

    # -- auth (viewer) -----------------------------------------------------

    def sign_in(self, email: str, password: str) -> Result:
        result = self._request(
            "POST",
            "/auth/v1/token",
            body={"email": email, "password": password},
            params={"grant_type": "password"},
        )
        if not result.ok:
            return result
        return self._adopt_session(result.data)

    def _adopt_session(self, data: object) -> Result:
        if not isinstance(data, dict) or not data.get("access_token"):
            return Result(False, None, "no access token in response")
        self.access_token = str(data["access_token"])
        self.refresh_token = str(data.get("refresh_token") or "")
        self.expires_at = time.time() + float(data.get("expires_in") or 3600)
        self._save_session(data)
        return Result(True, data.get("user"), "")

    def _ensure_token(self) -> bool:
        """A usable access token, refreshing or restoring one if needed."""
        if self.access_token and time.time() < self.expires_at - REFRESH_MARGIN:
            return True
        if not self.refresh_token:
            self._load_session()
        if self.access_token and time.time() < self.expires_at - REFRESH_MARGIN:
            return True
        if not self.refresh_token:
            return False
        result = self._request(
            "POST",
            "/auth/v1/token",
            body={"refresh_token": self.refresh_token},
            params={"grant_type": "refresh_token"},
        )
        if not result.ok:
            # A rejected refresh token is dead; drop it so the UI asks again
            # rather than retrying a token that will never work.
            self.refresh_token = ""
            self.access_token = ""
            return False
        return bool(self._adopt_session(result.data))

    @property
    def signed_in(self) -> bool:
        return self._ensure_token()

    def _save_session(self, data: dict) -> None:
        email = ""
        user = data.get("user")
        if isinstance(user, dict):
            email = str(user.get("email") or "")
        try:
            os.makedirs(identity.STATE_DIR, exist_ok=True)
            tmp = SESSION_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "refresh_token": self.refresh_token,
                        "access_token": self.access_token,
                        "expires_at": self.expires_at,
                        "email": email or self.viewer_email(),
                    },
                    fh,
                )
            os.replace(tmp, SESSION_PATH)
        except OSError:
            pass

    def _load_session(self) -> None:
        try:
            with open(SESSION_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.refresh_token = str(data.get("refresh_token") or "")
        self.access_token = str(data.get("access_token") or "")
        try:
            self.expires_at = float(data.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            self.expires_at = 0.0

    def viewer_email(self) -> str:
        try:
            with open(SESSION_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            return str(data.get("email") or "") if isinstance(data, dict) else ""
        except (OSError, ValueError):
            return ""

    def sign_out(self) -> None:
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        try:
            os.remove(SESSION_PATH)
        except OSError:
            pass

    # -- writes (reporter) -------------------------------------------------

    def insert(self, table: str, rows: list[dict]) -> Result:
        """Append rows. Duplicates are ignored rather than erroring, so a retry
        after a half-delivered batch cannot double-count a task."""
        if not rows:
            return Result(True, [], "")
        return self._request(
            "POST",
            f"/rest/v1/{table}",
            body=rows,
            prefer="return=minimal,resolution=ignore-duplicates",
        )

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> Result:
        """Replace rows by key. Used for the streams that describe *now* --
        live sessions, machine health -- where only the latest value matters."""
        if not rows:
            return Result(True, [], "")
        return self._request(
            "POST",
            f"/rest/v1/{table}",
            body=rows,
            params={"on_conflict": on_conflict},
            prefer="return=minimal,resolution=merge-duplicates",
        )

    def delete_where(self, table: str, filters: dict) -> Result:
        return self._request("DELETE", f"/rest/v1/{table}", params=filters)

    # -- reads (viewer) ----------------------------------------------------

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: dict | None = None,
        order: str = "",
        limit: int = 0,
    ) -> Result:
        params: dict = {"select": columns}
        if self.team_id:
            params["team_id"] = f"eq.{self.team_id}"
        params.update(filters or {})
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        result = self._request(
            "GET", f"/rest/v1/{table}", params=params, as_viewer=True
        )
        if result.ok and not isinstance(result.data, list):
            return Result(True, [], "")
        return result

    # -- diagnostics -------------------------------------------------------

    def check(self) -> Result:
        """Can this laptop reach the project at all? Used by the Deploy tab so
        a bad URL or key is caught at enrollment, not at first sync.

        A reachable project with a valid key answers the PostgREST root with
        its OpenAPI description; a wrong key answers 401.
        """
        if not self.configured:
            return Result(False, None, "no url or key configured")
        result = self._request("GET", "/rest/v1/")
        if result.ok:
            return Result(True, "reachable", "")
        return result
