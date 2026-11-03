r"""A local web dashboard, served from inside the widget's own process.

The Qt dashboard is for whoever is sitting at this machine. This is the same
data as a web page, which buys two things the Qt window cannot: it opens on a
phone or another laptop on the same network, and it can be left on a second
screen without a Python window in the alt-tab list.

Running *inside* the widget process is what makes the tile controls work. A
separate server would have to shout at the tile across process boundaries --
a pipe, a file watcher, a port -- and each of those is a thing to get wrong.
In-process, the handler emits a Qt signal and the tile's own
``apply_setting`` runs on the UI thread, which is the same path the tray menu
and the Qt dashboard take.

Served on ``127.0.0.1`` by default, where it needs no authentication because
reaching it already means running code on this machine. Binding wider is
opt-in (``web_bind`` in ``config.json``) and then a token is required on every
request, because the page shows prompt text and can resize someone's tile.

Built on ``http.server``. It is enough for a handful of clients polling a few
JSON endpoints, and the alternative is adding a web framework to a project
whose entire dependency list is three lines.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import events
import identity
import push
import pwa
import stats
import sync
import tokens
import vapid
import viz

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

DEFAULT_PORT = 8765
DEFAULT_BIND = "127.0.0.1"

# Aggregating the log on every poll would be wasteful when three browser tabs
# are open on the same window. Results are cached for this long, keyed by the
# query that produced them.
CACHE_TTL = 3.0

# Served without a token. The shell only: markup, stylesheet, code, the
# manifest and the service worker, all of which the browser fetches before any
# page has run. Nothing here carries a prompt, a project name or a number --
# everything that does lives under /api/ and is scoped. A test asserts this
# exact set, so widening it has to be deliberate.
_PUBLIC_PATHS = frozenset({
    "/", "/index.html", "/style.css", "/theme.css", "/app.js",
    "/manifest.webmanifest", "/sw.js", "/favicon.ico",
})


# Headers that mean "this did not come from where it looks like it came from".
# Any one of them withdraws the loopback trust, so a tunnel cannot inherit it.
_FORWARD_HEADERS = (
    "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
    "X-Real-IP", "Forwarded", "Via",
    "Ngrok-Trace-Id", "Cf-Connecting-Ip", "Cf-Ray",
    "X-Original-Forwarded-For", "X-Client-Ip",
)


def _is_icon(path: str) -> bool:
    return path.startswith("/icon-") and path.endswith(".png")


_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        with self._lock:
            entry = self._entries.get(key)
        if entry and (time.time() - entry[0]) < CACHE_TTL:
            return entry[1]
        return None

    def put(self, key: str, value) -> None:
        with self._lock:
            self._entries[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class DashboardServer:
    """Owns the HTTP thread and the handle back to the tile."""

    def __init__(self, cfg: dict, tile=None, apply_hook=None) -> None:
        self.cfg = cfg
        self.tile = tile
        # Called as apply_hook(key, value) from the HTTP thread. The widget
        # supplies one that marshals onto the UI thread; standalone callers
        # leave it None and settings are written to config.json instead.
        self.apply_hook = apply_hook
        self.cache = _Cache()
        self.port = int(cfg.get("web_port") or DEFAULT_PORT)
        self.bind = str(cfg.get("web_bind") or DEFAULT_BIND)
        self.token = str(cfg.get("web_token") or "")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._watcher = None

    @property
    def local_only(self) -> bool:
        return self.bind in ("127.0.0.1", "localhost", "::1")

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.local_only else self.bind
        suffix = f"?token={self.token}" if self.token and not self.local_only else ""
        return f"http://{host}:{self.port}/{suffix}"

    def start(self) -> str:
        """Start serving. Returns the URL, or "" if the port could not be taken."""
        if self._server is not None:
            return self.url
        if not self.local_only and not self.token:
            # Never expose the board to the network without one. Generated and
            # persisted so the URL stays valid across restarts.
            self.token = secrets.token_urlsafe(18)
            self.cfg["web_token"] = self.token
            try:
                import widget as widget_module

                widget_module.save_config(self.cfg)
            except Exception:  # noqa: BLE001
                pass

        server_self = self

        class Handler(_DashboardHandler):
            owner = server_self

        for attempt in range(6):
            try:
                self._server = ThreadingHTTPServer((self.bind, self.port + attempt), Handler)
                self.port = self.port + attempt
                break
            except OSError:
                continue
        if self._server is None:
            return ""

        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="dashboard-http", daemon=True
        )
        self._thread.start()
        self._start_watcher()
        return self.url

    def _start_watcher(self) -> None:
        """Watch the alert mailbox and push when an agent blocks.

        It lives here and not in the hook because "hooks never touch the
        network and never block a turn" is a constraint, and a push is a
        network call. This process is already long-running, so slowness here
        costs nobody a turn.

        Only when there is something to notify: with no subscriptions the
        thread would be a timer that wakes up to do nothing forever.
        """
        if self._watcher is not None:
            return
        try:
            if not push.listing():
                return
            self._watcher = push.Watcher()
            self._watcher.start()
        except Exception:  # noqa: BLE001
            # Notifications are a convenience. The dashboard must still serve
            # if the store is unreadable.
            self._watcher = None

    def stop(self) -> None:
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:  # noqa: BLE001
                pass
            self._watcher = None
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        self._thread = None

    def vapid_key(self) -> str:
        """The public key a browser subscribes with.

        A new keypair invalidates every subscription made against the old one,
        so they are cleared in the same breath rather than left to fail one at
        a time over the following week.
        """
        try:
            key = vapid.load()
            if key.get("fresh"):
                push.clear()
            return key["public"]
        except Exception:  # noqa: BLE001
            return ""

    @property
    def running(self) -> bool:
        return self._server is not None

    # -- data ------------------------------------------------------------

    def rows_for(self, period: int, scope: str) -> list[dict]:
        key = f"rows:{period}:{scope}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        start = stats.window_start(period)
        if scope == "team":
            rows = [
                r
                for r in sync.read_cache().get("tasks", [])
                if float(r.get("ended_at") or 0) >= start
            ]
        else:
            rows = stats.task_rows(events.load(), start)
        self.cache.put(key, rows)
        return rows

    def summary_payload(self, period: int, scope: str) -> dict:
        rows = self.rows_for(period, scope)
        summary = stats.summarize_tasks(rows)
        reply_label = "avg reply"
        reply_value = summary["reply_mean"]
        if scope == "me":
            try:
                exact = stats.summarize(events.load(), stats.window_start(period))
                reply_value = exact["reply_median"]
                reply_label = "median reply"
            except Exception:  # noqa: BLE001
                pass
        return {
            "period": period,
            "scope": scope,
            "tasks": summary["tasks"],
            "tokens": summary["tokens"],
            "tokens_h": viz.fmt_tokens(summary["tokens"]),
            "cost": round(summary["cost"], 2),
            "cost_h": viz.fmt_cost(summary["cost"]),
            "task_median": summary["task_median"],
            "task_median_h": viz.fmt_duration(summary["task_median"]),
            "reply": reply_value,
            "reply_h": viz.fmt_duration(reply_value),
            "reply_label": reply_label,
            "reply_max_h": viz.fmt_duration(summary["reply_max"]),
            "tools_per_task": round(summary["tools_per_task"], 1),
            "projects": summary["projects"],
            "tok": summary["tok"],
            "daily": stats.daily_series(rows, period or 1),
            "hourly": stats.hourly_series(rows),
            "models": [
                {**m, "tokens_h": viz.fmt_tokens(m["tokens"]),
                 "color": viz.model_color(m["name"])}
                for m in summary["model_rows"]
            ],
            "projects_rows": [
                {**p, "tokens_h": viz.fmt_tokens(p["tokens"]),
                 "cost_h": viz.fmt_cost(p["cost"]),
                 "task_median_h": viz.fmt_duration(p["task_median"]),
                 "reply_h": viz.fmt_duration(p["reply_mean"])}
                for p in summary["project_rows"]
            ],
            "members_rows": [
                {**m, "tokens_h": viz.fmt_tokens(m["tokens"]),
                 "cost_h": viz.fmt_cost(m["cost"]),
                 "task_median_h": viz.fmt_duration(m["task_median"]),
                 "reply_h": viz.fmt_duration(m["reply_mean"])}
                for m in summary["member_rows"]
            ],
        }

    def tasks_payload(self, period: int, scope: str, limit: int = 500) -> dict:
        rows = self.rows_for(period, scope)
        return {
            "total": len(rows),
            "tasks": [
                {
                    "ended_at": r.get("ended_at"),
                    "when": time.strftime(
                        "%d %b %H:%M", time.localtime(r.get("ended_at") or 0)
                    ),
                    "member": r.get("member", "me"),
                    "project": r.get("project", ""),
                    "prompt": r.get("prompt", ""),
                    "duration_s": r.get("duration_s"),
                    "duration_h": viz.fmt_duration(r.get("duration_s")),
                    "blocked_h": viz.fmt_duration(r.get("blocked_s") or None),
                    "tokens": r.get("tokens", 0),
                    "tokens_h": viz.fmt_tokens(r.get("tokens")),
                    "cost": round(r.get("cost") or 0, 4),
                    "cost_h": viz.fmt_cost(r.get("cost")),
                    "tools": r.get("tools", 0),
                    "model": stats.model_family(r.get("model", "")),
                    "model_color": viz.model_color(
                        stats.model_family(r.get("model", ""))
                    ),
                }
                for r in rows[:limit]
            ],
        }

    def memory_payload(
        self, query: str = "", proj: str = "", kind: str = "",
        limit: int = 100,
    ) -> dict:
        """What the Memory view shows: observations, searched or recent.

        Not cached. The cache here is keyed on period and scope, and a memory
        query is neither; caching per query string would fill it with
        single-use entries. The query is an indexed FTS lookup against a local
        file, so it is cheap enough to just run.
        """
        try:
            import store

            return store.observations_page(
                query=query, proj=proj, kind=kind, limit=limit)
        except Exception as exc:  # noqa: BLE001 - a view must not 500
            return {"rows": [], "matched": 0, "total": 0, "projects": [],
                    "kinds": [], "query": query, "project": proj,
                    "kind": kind, "error": str(exc)}

    def memory_detail_payload(self, oid: str) -> dict:
        """One observation with nothing trimmed, for the detail pane."""
        try:
            import store

            return {"row": store.observation(int(oid))}
        except (TypeError, ValueError):
            return {"row": {}, "error": "id must be a number"}
        except Exception as exc:  # noqa: BLE001
            return {"row": {}, "error": str(exc)}

    def team_payload(self) -> dict:
        cache = sync.read_cache()
        state = sync.status()
        devices = sync.team_devices(cache)
        sessions = sync.live_sessions(cache)
        return {
            "status": state,
            "fetched_at": cache.get("fetched_at", 0),
            "fetched_h": viz.fmt_ago(
                time.time() - (cache.get("fetched_at") or time.time())
            ),
            "error": cache.get("error", ""),
            "online": sum(1 for d in devices if d["online"]),
            "working": sum(1 for s in sessions if s.get("state") == "working"),
            "waiting": sum(1 for s in sessions if s.get("state") == "needs_input"),
            "devices": [
                {
                    **d,
                    "last_seen_h": viz.fmt_ago(d.get("offline_s")),
                    "sessions": [
                        {
                            **s,
                            "state_label": viz.STATUS_LABEL.get(
                                s.get("state", ""), s.get("state", "")
                            ),
                            "state_color": viz.status_color(s.get("state", "")),
                            "age_h": viz.fmt_duration(s.get("age_s")),
                        }
                        for s in d.get("sessions", [])
                    ],
                }
                for d in devices
            ],
        }

    def tile_payload(self) -> dict:
        cfg = self.tile.cfg if self.tile is not None else self.cfg
        keys = (
            "scale", "opacity", "bg_alpha", "fan_mode", "fan_max_rpm",
            "interval_ms", "slow_every", "click_through", "hide_from_capture",
            "pop_on_alert", "show_stats", "locked", "auto_trim",
            "auto_trim_pct", "auto_trim_gb",
        )
        return {
            "live": self.tile is not None,
            "settings": {k: cfg.get(k) for k in keys},
        }

    def apply_tile(self, key: str, value) -> tuple[bool, str]:
        if self.apply_hook is not None:
            # Queued onto the UI thread; the page re-reads /api/tile to see the
            # result rather than this call waiting for one.
            self.apply_hook(key, value)
            return True, "queued"
        try:
            import widget as widget_module

            if key not in widget_module.DEFAULTS:
                return False, f"unknown setting {key}"
            self.cfg[key] = value
            widget_module.save_config(self.cfg)
            return True, "saved to config.json"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


class _DashboardHandler(BaseHTTPRequestHandler):
    owner: DashboardServer = None  # set on the subclass in start()

    server_version = "AgentDashboard/1.0"

    def log_message(self, fmt, *args) -> None:
        """Silence. The access log would go to the widget's console, where it
        is noise that buries an actual traceback."""

    # -- helpers ---------------------------------------------------------

    def _supplied_token(self) -> str:
        header = self.headers.get("X-Dashboard-Token", "")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return str(header or (query.get("token") or [""])[0])

    def _forwarded(self) -> bool:
        """Did this request reach us through a proxy or a tunnel?

        This is the difference between "nobody but me can reach this" and
        "anybody on the internet can". A tunnel -- ngrok, cloudflared,
        ``ssh -R`` -- runs on this machine and connects to the loopback
        interface, so a tunnelled request arrives looking exactly like one
        typed on this keyboard. Trusting the bind address alone would hand
        every scope to the whole internet the moment a tunnel is opened,
        which is precisely when the dashboard is most exposed.

        Every one of those tools announces itself in a header. Absence of a
        header is not proof of anything, but it does not need to be: this only
        ever *removes* trust, so the worst a forged header can do is demand a
        token that the caller then has to actually have.
        """
        return any(self.headers.get(name) for name in _FORWARD_HEADERS)

    def _from_loopback(self) -> bool:
        try:
            host = (self.client_address or ("",))[0]
        except Exception:  # noqa: BLE001
            return False
        return str(host) in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _scopes(self) -> list[str]:
        """What the caller is allowed to do. Empty means "nothing".

        The legacy ``config.json`` token carries everything. Breaking a working
        remote dashboard to tidy up the auth model would be the wrong trade, so
        it keeps working and ``tokens.py list`` reports it as legacy.
        """
        owner = self.owner
        # Three conditions, not one: bound to loopback, *arrived* on loopback,
        # and not forwarded. See `_forwarded`.
        if owner.local_only and self._from_loopback() and not self._forwarded():
            return list(tokens.SCOPES)
        supplied = self._supplied_token()
        if not supplied:
            return []
        if owner.token and secrets.compare_digest(supplied, owner.token):
            return list(tokens.SCOPES)
        return tokens.verify(supplied)

    def _authorised(self, scope: str = tokens.READ) -> bool:
        return scope in self._scopes()

    def _deny(self, scope: str) -> None:
        """401 when there is no usable credential, 403 when there is.

        Conflating the two makes a client retry forever against a permission it
        will never be granted, and tells a caller holding a read-only token
        that their token is broken when it is working exactly as issued.
        """
        if self._scopes():
            self._json({"error": "this token does not carry '%s'" % scope}, 403)
        else:
            self._json({"error": "token required"}, 401)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is same-origin and the API is not meant for other sites.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        raw = urllib.parse.parse_qs(parsed.query)
        return {k: v[0] for k, v in raw.items()}

    def _period(self) -> int:
        try:
            value = int(self._query().get("period", 7))
        except (TypeError, ValueError):
            return 7
        return value if value in (0, 7, 30) else 7

    def _scope(self) -> str:
        return "team" if self._query().get("scope") == "team" else "me"

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        # The shell is public; the data is not.
        #
        # This changed when the dashboard became installable. A manifest and a
        # service worker are fetched by the browser itself, before any page
        # has run and before a token exists anywhere, and an installed app
        # launches at "/" with no query string -- so a gate on the shell means
        # the icon on a home screen opens a 401 and nothing else, forever.
        #
        # Moving the gate is safe because the shell is markup, stylesheet and
        # code: it holds no prompt text, no project names and no numbers.
        # Everything that holds those is under /api/, which still demands a
        # scope on every single request. `_PUBLIC_PATHS` is asserted by a test
        # so this cannot quietly widen.
        if path not in _PUBLIC_PATHS and not _is_icon(path):
            if not self._authorised(tokens.READ):
                self._deny(tokens.READ)
                return

        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path == "/theme.css":
            self._send(200, viz.theme_css().encode("utf-8"), _MIME[".css"])
            return
        if path == "/manifest.webmanifest":
            self._send(200,
                       json.dumps(pwa.manifest()).encode("utf-8"),
                       "application/manifest+json")
            return
        if _is_icon(path):
            self._icon(path)
            return
        if path.startswith("/api/"):
            self._api_get(path)
            return
        self._static(path.lstrip("/"))

    def _icon(self, path: str) -> None:
        try:
            size = int(path[len("/icon-"):-len(".png")])
        except ValueError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, pwa.icon(size), "image/png")

    def _api_get(self, path: str) -> None:
        owner = self.owner
        if path == "/api/summary":
            self._json(owner.summary_payload(self._period(), self._scope()))
        elif path == "/api/tasks":
            self._json(owner.tasks_payload(self._period(), self._scope()))
        elif path == "/api/team":
            self._json(owner.team_payload())
        elif path == "/api/tile":
            self._json(owner.tile_payload())
        elif path == "/api/escalations":
            # Straight from the mailbox, uncached: this is what the service
            # worker reads to write a notification, and a stale answer there
            # tells someone an agent is waiting when it is not.
            rows = push.escalations()
            self._json({"rows": rows, "count": len(rows)})
        elif path == "/api/push/key":
            self._json({"key": owner.vapid_key(),
                        "subscriptions": len(push.listing())})
        elif path == "/api/memory":
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)

            def one(name: str) -> str:
                return (params.get(name) or [""])[0].strip()[:200]

            oid = one("id")
            if oid:
                self._json(owner.memory_detail_payload(oid))
            else:
                self._json(owner.memory_payload(
                    query=one("q"), proj=one("project"), kind=one("kind")))
        elif path == "/api/meta":
            self._json(
                {
                    "member": identity.member_name(),
                    "enrolled": identity.enrolled(),
                    "models": viz.model_color_map(),
                    "events": len(events.load()),
                    "generated_at": time.time(),
                }
            )
        else:
            self._json({"error": "no such endpoint"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        # Most POSTs change this machine's state -- the tile, or a sync that
        # pushes activity upstream -- so read alone is not enough. A new POST
        # route inherits that by default rather than having to remember to
        # ask; subscribing is the one exception, and it is named rather than
        # assumed.
        #
        # Asking to be notified about the board is a read channel: it grants
        # the caller nothing they could not already see, and requiring
        # `operate` for it would mean the token on a phone had to be able to
        # resize the tile in order to buzz.
        needed = tokens.READ if path == "/api/push/subscribe" else tokens.OPERATE
        if not self._authorised(needed):
            self._deny(needed)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(min(length, 65536)) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._json({"error": "body must be JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"error": "body must be a JSON object"}, 400)
            return

        owner = self.owner
        if path == "/api/tile":
            key = str(body.get("key") or "")
            if not key:
                self._json({"error": "key is required"}, 400)
                return
            ok, note = owner.apply_tile(key, body.get("value"))
            owner.cache.clear()
            self._json({"ok": ok, "note": note}, 200 if ok else 400)
            return
        if path == "/api/push/subscribe":
            self._json(push.subscribe(body.get("subscription") or body,
                                      label=str(body.get("label") or "")))
            return
        if path == "/api/push/unsubscribe":
            endpoint = str(body.get("endpoint") or body.get("id") or "")
            self._json({"ok": push.unsubscribe(endpoint)})
            return
        if path == "/api/push/test":
            # A real notification to every subscriber, so "is this wired up?"
            # is answerable without blocking an agent to find out.
            self._json(push.send_all())
            return
        if path == "/api/sync":
            # Done on the calling thread: this is an HTTP worker thread, so a
            # slow network blocks only this request, not the page.
            report = sync.once(days=self._period() or 7)
            owner.cache.clear()
            self._json(report)
            return
        self._json({"error": "no such endpoint"}, 404)

    # -- static ----------------------------------------------------------

    def _static(self, relative: str) -> None:
        # Resolve and confirm the result is still under web/, so a crafted
        # path cannot walk out of it and serve config.json or team.json.
        safe = os.path.normpath(os.path.join(WEB_DIR, relative))
        if not safe.startswith(WEB_DIR + os.sep) and safe != WEB_DIR:
            self._send(403, b"forbidden", "text/plain")
            return
        if not os.path.isfile(safe):
            self._send(404, b"not found", "text/plain")
            return
        try:
            with open(safe, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(500, b"could not read", "text/plain")
            return
        extension = os.path.splitext(safe)[1].lower()
        self._send(200, body, _MIME.get(extension, "application/octet-stream"))


def main() -> int:
    """Serve the dashboard without the tile, for a quick look or a headless box."""
    import webbrowser

    import widget as widget_module

    cfg = widget_module.load_config()
    server = DashboardServer(cfg, tile=None)
    url = server.start()
    if not url:
        print("could not bind a port")
        return 1
    print(f"serving the dashboard at {url}")
    print("tile settings will be written to config.json (no tile is running)")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
