"""Scoped access tokens.

Test names map to the acceptance criteria in
``build-docs/features/scoped-tokens.md``.

Auth has two failure directions and they are not symmetric in how they are
noticed: locking the owner out is reported within a minute, and letting a
read-only token POST is reported by nobody. Both are asserted here, and the
HTTP half goes through a real server over a real socket rather than through
the handler's methods, because the thing being tested is what a client gets.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request

from ._env import PROJECT, Sandbox


class MintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.tokens = self.sb.tokens
        self.conn = self.sb.store.connect()
        self.addCleanup(self.conn.close)

    # -- 1, 2: round trip, and the secret is not kept ---------------------

    def test_a_minted_token_verifies_with_the_scopes_it_was_minted_with(self):
        issued = self.tokens.mint(["read"], label="phone", conn=self.conn)
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn),
                         ["read"])

    def test_several_scopes_round_trip_in_order(self):
        issued = self.tokens.mint("read operate answer", conn=self.conn)
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn),
                         ["read", "operate", "answer"])

    def test_scopes_may_be_comma_separated(self):
        issued = self.tokens.mint("read,operate", conn=self.conn)
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn),
                         ["read", "operate"])

    def test_the_secret_is_nowhere_in_the_database(self):
        """A leaked store.db must not yield a working credential."""
        issued = self.tokens.mint(["read"], conn=self.conn)
        secret = issued["token"].split(".", 1)[1]
        dump = "\n".join(
            str(tuple(r)) for r in self.conn.execute("select * from tokens"))
        self.assertNotIn(secret, dump)
        self.assertNotIn(issued["token"], dump)

    def test_two_tokens_differ(self):
        a = self.tokens.mint(["read"], conn=self.conn)["token"]
        b = self.tokens.mint(["read"], conn=self.conn)["token"]
        self.assertNotEqual(a, b)
        self.assertNotEqual(a.split(".")[0], b.split(".")[0])

    def test_a_token_with_no_valid_scope_is_refused_at_mint(self):
        """Useless rather than unlimited is the safe direction."""
        for scopes in ([], "", "nonsense", ["delete-everything"], None):
            with self.subTest(scopes=scopes):
                with self.assertRaises(ValueError):
                    self.tokens.mint(scopes, conn=self.conn)

    def test_an_unknown_scope_is_dropped_not_kept(self):
        issued = self.tokens.mint("read nonsense", conn=self.conn)
        self.assertEqual(issued["scopes"], ["read"])

    # -- 4: revoked and expired -------------------------------------------

    def test_a_revoked_token_is_refused(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.assertTrue(self.tokens.revoke(issued["tid"], conn=self.conn))
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn), [])

    def test_revoking_twice_reports_the_second_as_a_miss(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.tokens.revoke(issued["tid"], conn=self.conn)
        self.assertFalse(self.tokens.revoke(issued["tid"], conn=self.conn))

    def test_revoking_an_unknown_id_is_false_not_an_error(self):
        self.assertFalse(self.tokens.revoke("deadbeef", conn=self.conn))

    def test_an_expired_token_is_refused(self):
        issued = self.tokens.mint(["read"], days=1, conn=self.conn)
        self.conn.execute("update tokens set expires_at=? where tid=?",
                          (time.time() - 1, issued["tid"]))
        self.conn.commit()
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn), [])

    def test_a_token_without_an_expiry_does_not_expire(self):
        issued = self.tokens.mint(["read"], days=0, conn=self.conn)
        self.assertEqual(issued["expires_at"], 0)
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn),
                         ["read"])

    def test_revocation_beats_an_unreached_expiry(self):
        issued = self.tokens.mint(["read"], days=365, conn=self.conn)
        self.tokens.revoke(issued["tid"], conn=self.conn)
        self.assertEqual(self.tokens.verify(issued["token"], conn=self.conn), [])

    # -- 5: hostile and malformed input -----------------------------------

    def test_a_malformed_token_is_refused_without_raising(self):
        for token in ("", None, "no-separator", ".", "a.", ".b", "..",
                      "x" * 5000, 12345, "\x00.\x00", "../../etc/passwd"):
            with self.subTest(token=token):
                self.assertEqual(self.tokens.verify(token, conn=self.conn), [])

    def test_an_unknown_id_is_refused(self):
        self.assertEqual(
            self.tokens.verify("deadbeef.whatever", conn=self.conn), [])

    def test_the_right_id_with_the_wrong_secret_is_refused(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.assertEqual(
            self.tokens.verify(issued["tid"] + ".wrong", conn=self.conn), [])

    def test_an_oversized_token_is_rejected_before_it_is_hashed(self):
        """Hashing a megabyte on a stranger's behalf is work they chose."""
        self.assertEqual(
            self.tokens.verify("a." + "x" * self.tokens.MAX_TOKEN_CHARS,
                               conn=self.conn), [])

    def test_a_broken_database_denies_rather_than_admits(self):
        def boom(*a, **k):
            raise RuntimeError("no database")
        self.sb.tokens.store.connect = boom
        self.addCleanup(setattr, self.sb.tokens.store, "connect",
                        self.sb.store.connect)
        self.assertEqual(self.tokens.verify("deadbeef.secret"), [])

    # -- listing -----------------------------------------------------------

    def test_listing_never_includes_a_hash(self):
        self.tokens.mint(["read"], label="phone", conn=self.conn)
        rows = self.tokens.listing(conn=self.conn)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("hash", rows[0])
        self.assertEqual(rows[0]["label"], "phone")

    def test_listing_hides_revoked_tokens_unless_asked(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.tokens.revoke(issued["tid"], conn=self.conn)
        self.assertEqual(self.tokens.listing(conn=self.conn), [])
        self.assertEqual(
            len(self.tokens.listing(include_revoked=True, conn=self.conn)), 1)

    def test_listing_flags_an_expired_token(self):
        issued = self.tokens.mint(["read"], days=1, conn=self.conn)
        self.conn.execute("update tokens set expires_at=? where tid=?",
                          (time.time() - 1, issued["tid"]))
        self.conn.commit()
        self.assertTrue(self.tokens.listing(conn=self.conn)[0]["expired"])

    def test_use_is_recorded(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.assertEqual(self.tokens.listing(conn=self.conn)[0]["last_used_at"], 0)
        self.tokens.verify(issued["token"], conn=self.conn)
        self.assertGreater(
            self.tokens.listing(conn=self.conn)[0]["last_used_at"], 0)

    def test_allows_is_the_single_scope_question(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.assertTrue(self.tokens.allows(issued["token"], "read",
                                           conn=self.conn))
        self.assertFalse(self.tokens.allows(issued["token"], "operate",
                                            conn=self.conn))

    # -- 9: migration ------------------------------------------------------

    def test_tokens_survive_a_schema_migration(self):
        """A credential cannot be regenerated from the log the way a row can."""
        issued = self.tokens.mint(["read", "operate"], label="keep me",
                                  conn=self.conn)
        self.conn.execute("pragma user_version=3")
        self.conn.commit()
        self.conn.close()

        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        self.assertEqual(
            int(conn.execute("pragma user_version").fetchone()[0]),
            self.sb.store.SCHEMA_VERSION)
        self.assertEqual(self.tokens.verify(issued["token"], conn=conn),
                         ["read", "operate"])

    def test_a_fresh_database_has_the_tokens_table(self):
        self.assertIsNotNone(self.conn.execute(
            "select name from sqlite_master where type='table' "
            "and name='tokens'").fetchone())


class HttpScopeTest(unittest.TestCase):
    """What a client actually gets, over a real socket.

    Bound to 127.0.0.1 but with ``local_only`` forced off, because otherwise
    the loopback bypass would make every one of these pass for the wrong
    reason.
    """

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        import importlib

        import webserver

        self.webserver = importlib.reload(webserver)
        self.tokens = self.sb.tokens
        self.conn = self.sb.store.connect()
        self.addCleanup(self.conn.close)

        self.server = self.webserver.DashboardServer(
            {"web_port": 0, "web_bind": "127.0.0.1", "web_token": "legacy-secret"})
        type(self.server).local_only = property(lambda self: False)
        self.addCleanup(self._restore_local_only)
        self.url = self.server.start()
        self.addCleanup(self.server.stop)
        self.assertTrue(self.url, "the server did not start")
        self.base = "http://127.0.0.1:%d" % self.server._server.server_port

    def _restore_local_only(self) -> None:
        try:
            del type(self.server).local_only
        except AttributeError:
            pass

    def get(self, path: str, token: str = "") -> tuple[int, dict]:
        return self._request("GET", path, token)

    def post(self, path: str, token: str = "", body: dict | None = None):
        return self._request("POST", path, token, body or {})

    def _request(self, method, path, token, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("X-Dashboard-Token", token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as reply:
                return reply.status, self._body(reply.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._body(exc.read())

    @staticmethod
    def _body(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # -- 8: GET needs read, POST needs operate ----------------------------

    def test_no_token_is_401(self):
        status, body = self.get("/api/summary")
        self.assertEqual(status, 401)
        self.assertIn("token", body.get("error", ""))

    def test_a_read_token_can_read(self):
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        self.assertEqual(self.get("/api/summary", token)[0], 200)

    def test_a_read_token_cannot_operate(self):
        """The one that nobody would notice: a glance-at-a-chart token
        changing the machine it is glancing at."""
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        status, body = self.post("/api/tile", token, {"key": "opacity",
                                                      "value": 0.5})
        self.assertEqual(status, 403)
        self.assertIn("operate", body.get("error", ""))

    def test_an_operate_token_can_operate(self):
        token = self.tokens.mint(["read", "operate"], conn=self.conn)["token"]
        self.assertNotIn(
            self.post("/api/tile", token, {"key": "opacity", "value": 0.5})[0],
            (401, 403))

    def test_an_operate_only_token_still_cannot_read(self):
        """Scopes are a list, not a ladder."""
        token = self.tokens.mint(["operate"], conn=self.conn)["token"]
        self.assertEqual(self.get("/api/summary", token)[0], 403)

    def test_an_answer_token_can_neither_read_nor_operate(self):
        token = self.tokens.mint(["answer"], conn=self.conn)["token"]
        self.assertEqual(self.get("/api/summary", token)[0], 403)
        self.assertEqual(self.post("/api/tile", token, {"key": "x"})[0], 403)

    # -- 3: the two denials are different ---------------------------------

    def test_a_missing_credential_is_401_and_a_missing_scope_is_403(self):
        """Conflating them makes a client retry forever against a permission
        it will never be granted."""
        self.assertEqual(self.get("/api/summary")[0], 401)
        self.assertEqual(self.get("/api/summary", "garbage")[0], 401)
        read = self.tokens.mint(["read"], conn=self.conn)["token"]
        self.assertEqual(self.post("/api/tile", read, {"key": "x"})[0], 403)

    def test_a_revoked_token_stops_working_immediately(self):
        issued = self.tokens.mint(["read"], conn=self.conn)
        self.assertEqual(self.get("/api/summary", issued["token"])[0], 200)
        self.tokens.revoke(issued["tid"], conn=self.conn)
        self.assertEqual(self.get("/api/summary", issued["token"])[0], 401)

    # -- 6: the legacy token ----------------------------------------------

    def test_the_legacy_config_token_still_works_and_carries_everything(self):
        self.assertEqual(self.get("/api/summary", "legacy-secret")[0], 200)
        self.assertNotIn(
            self.post("/api/tile", "legacy-secret",
                      {"key": "opacity", "value": 0.5})[0], (401, 403))

    def test_a_wrong_legacy_token_is_refused(self):
        self.assertEqual(self.get("/api/summary", "legacy-secre")[0], 401)
        self.assertEqual(self.get("/api/summary", "legacy-secretx")[0], 401)

    # -- the static page is behind the same gate --------------------------

    def test_the_shell_is_public_and_the_data_is_not(self):
        """This gate moved when the dashboard became installable.

        It used to be that "/" itself demanded a token. An installed app
        launches at "/" with no query string, and the browser fetches the
        manifest and the service worker before any page has run, so a gate
        there means the icon on a home screen opens a 401 and nothing else,
        forever. The shell is markup, stylesheet and code -- it carries no
        prompt text, no project names and no numbers -- so the gate belongs
        on the data, which is where it now is.
        """
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/api/summary")[0], 401)
        self.assertEqual(self.get("/api/memory")[0], 401)
        self.assertEqual(self.get("/api/tasks")[0], 401)

    def test_the_token_may_travel_in_the_query_string(self):
        """How a phone gets in from a pasted link, before it can set a header."""
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        self.assertEqual(
            self.get("/api/summary?token=" + urllib.parse.quote(token))[0], 200)


class LocalOnlyTest(unittest.TestCase):
    """7: the desktop case must not regress into needing a token."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        import importlib

        import webserver

        self.webserver = importlib.reload(webserver)
        self.server = self.webserver.DashboardServer(
            {"web_port": 0, "web_bind": "127.0.0.1", "web_token": ""})
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        self.base = "http://127.0.0.1:%d" % self.server._server.server_port

    def test_loopback_needs_no_token(self):
        with urllib.request.urlopen(self.base + "/api/summary", timeout=20) as r:
            self.assertEqual(r.status, 200)


class BundleTest(unittest.TestCase):
    def test_tokens_ships_in_both_file_lists(self):
        import deploy

        # The full bundle, not the reporter one: nothing but webserver.py
        # imports it, and a reporter-only install serves no HTTP.
        self.assertIn("tokens.py", deploy.FULL_EXTRA_FILES)
        with open(os.path.join(PROJECT, "install", "install.ps1"),
                  encoding="utf-8-sig") as fh:
            self.assertIn("tokens.py", fh.read())


if __name__ == "__main__":
    unittest.main()
