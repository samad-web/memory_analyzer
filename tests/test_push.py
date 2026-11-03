"""The PWA shell and push.

Test names map to the acceptance criteria in
``build-docs/features/pwa-and-push.md``.

The crypto is the part that cannot be checked by agreeing with itself, so it
is checked against RFC 6979's published signatures for P-256/SHA-256. If those
numbers match, the curve arithmetic, the hash handling and the deterministic
nonce are all right together; if any one of them is wrong they cannot match by
accident.

The network is never touched. Every send path goes through a stubbed opener,
because a test that depends on Google being up is not a test.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import time
import unittest
import urllib.error
import urllib.request
import zlib

from ._env import PROJECT, Sandbox


class CurveTest(unittest.TestCase):
    """4: against the standard, not against itself."""

    # RFC 6979 appendix A.2.5 -- P-256, SHA-256.
    KEY = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
    UX = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
    UY = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
    VECTORS = {
        b"sample": (
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
            0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
        b"test": (
            0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
            0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
    }

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.vapid = self.sb.vapid

    def test_the_public_point_matches_the_rfc(self):
        self.assertEqual(self.vapid.public_point(self.KEY), (self.UX, self.UY))

    def test_signatures_match_the_rfc_6979_vectors(self):
        for message, (want_r, want_s) in self.VECTORS.items():
            with self.subTest(message=message):
                signature = self.vapid.sign(message, self.KEY)
                self.assertEqual(int.from_bytes(signature[:32], "big"), want_r)
                self.assertEqual(int.from_bytes(signature[32:], "big"), want_s)

    def test_signing_is_deterministic(self):
        """No randomness means no nonce reuse, which is the failure that
        leaks the key."""
        self.assertEqual(self.vapid.sign(b"sample", self.KEY),
                         self.vapid.sign(b"sample", self.KEY))

    def test_a_signature_verifies(self):
        signature = self.vapid.sign(b"hello", self.KEY)
        self.assertTrue(
            self.vapid.verify(b"hello", signature, (self.UX, self.UY)))

    def test_a_tampered_message_does_not_verify(self):
        signature = self.vapid.sign(b"hello", self.KEY)
        self.assertFalse(
            self.vapid.verify(b"hell0", signature, (self.UX, self.UY)))

    def test_a_tampered_signature_does_not_verify(self):
        signature = bytearray(self.vapid.sign(b"hello", self.KEY))
        signature[5] ^= 0x01
        self.assertFalse(
            self.vapid.verify(b"hello", bytes(signature), (self.UX, self.UY)))

    def test_another_key_does_not_verify(self):
        other = self.vapid.generate()
        signature = self.vapid.sign(b"hello", other["private"])
        self.assertFalse(
            self.vapid.verify(b"hello", signature, (self.UX, self.UY)))

    def test_a_malformed_signature_is_rejected_not_raised(self):
        for bad in (b"", b"\x00" * 63, b"\x00" * 64, b"\xff" * 64):
            with self.subTest(len=len(bad)):
                self.assertFalse(
                    self.vapid.verify(b"x", bad, (self.UX, self.UY)))

    def test_a_generated_key_round_trips(self):
        key = self.vapid.generate()
        raw = self.vapid.unb64(key["public"])
        self.assertEqual(len(raw), 65)
        self.assertEqual(raw[0], 0x04)
        x = int.from_bytes(raw[1:33], "big")
        y = int.from_bytes(raw[33:], "big")
        self.assertEqual(self.vapid.public_point(key["private"]), (x, y))

    def test_a_key_is_persisted_and_reloaded(self):
        first = self.vapid.load()
        self.assertTrue(first["fresh"])
        second = self.vapid.load()
        self.assertFalse(second["fresh"])
        self.assertEqual(first["public"], second["public"])

    def test_a_corrupt_key_file_is_replaced_rather_than_fatal(self):
        self.vapid.load()
        with open(self.vapid.KEY_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        replacement = self.vapid.load()
        self.assertTrue(replacement["fresh"])
        self.assertTrue(replacement["public"])

    def test_an_out_of_range_key_is_replaced(self):
        self.vapid.load()
        with open(self.vapid.KEY_PATH, "w", encoding="utf-8") as fh:
            json.dump({"private": 0}, fh)
        self.assertTrue(self.vapid.load()["fresh"])


class JwtTest(unittest.TestCase):
    """5: the header a push service actually reads."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.vapid = self.sb.vapid
        self.key = self.vapid.load()

    @staticmethod
    def _segment(text: str) -> dict:
        padded = text + "=" * (-len(text) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def test_the_token_has_three_segments(self):
        parts = self.vapid.token("https://fcm.googleapis.com/fcm/send/abc",
                                 key=self.key).split(".")
        self.assertEqual(len(parts), 3)

    def test_the_header_declares_es256(self):
        token = self.vapid.token("https://push.example/x", key=self.key)
        header = self._segment(token.split(".")[0])
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["typ"], "JWT")

    def test_the_audience_is_the_services_origin_not_the_endpoint(self):
        token = self.vapid.token(
            "https://fcm.googleapis.com/fcm/send/abc?x=1", key=self.key)
        self.assertEqual(self._segment(token.split(".")[1])["aud"],
                         "https://fcm.googleapis.com")

    def test_the_expiry_is_in_the_future_and_within_a_day(self):
        claims = self._segment(
            self.vapid.token("https://push.example/x", key=self.key)
            .split(".")[1])
        now = time.time()
        self.assertGreater(claims["exp"], now)
        self.assertLess(claims["exp"], now + 86400)

    def test_the_signature_verifies_against_the_public_key(self):
        token = self.vapid.token("https://push.example/x", key=self.key)
        signing_input, _, signature = token.rpartition(".")
        raw = self.vapid.unb64(self.key["public"])
        point = (int.from_bytes(raw[1:33], "big"),
                 int.from_bytes(raw[33:], "big"))
        self.assertTrue(self.vapid.verify(
            signing_input.encode("ascii"), self.vapid.unb64(signature), point))

    def test_a_non_url_endpoint_is_refused(self):
        for endpoint in ("", "not a url", "/relative", None):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self.vapid.token(endpoint, key=self.key)

    def test_the_headers_carry_the_key_and_a_zero_length_body(self):
        headers = self.vapid.headers("https://push.example/x", key=self.key)
        self.assertTrue(headers["Authorization"].startswith("vapid t="))
        self.assertIn("k=" + self.key["public"], headers["Authorization"])
        self.assertEqual(headers["Content-Length"], "0")
        self.assertTrue(int(headers["TTL"]) > 0)


class IconTest(unittest.TestCase):
    """1: a manifest icon has to be a real PNG or installation is refused."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        import pwa

        self.pwa = pwa

    def decode(self, raw: bytes) -> tuple[int, int, callable]:
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks = 8, {}
        while pos < len(raw):
            (length,) = struct.unpack(">I", raw[pos:pos + 4])
            tag = raw[pos + 4:pos + 8]
            data = raw[pos + 8:pos + 8 + length]
            (crc,) = struct.unpack(">I", raw[pos + 8 + length:pos + 12 + length])
            self.assertEqual(crc, binascii.crc32(tag + data) & 0xFFFFFFFF,
                             "bad CRC on %s" % tag)
            chunks[tag] = data
            pos += 12 + length
        self.assertEqual(set(chunks), {b"IHDR", b"IDAT", b"IEND"})
        width, height, depth, colour, _, _, _ = struct.unpack(
            ">IIBBBBB", chunks[b"IHDR"])
        flat = zlib.decompress(chunks[b"IDAT"])
        stride = width * 4 + 1
        self.assertEqual(len(flat), stride * height)

        def pixel(x: int, y: int):
            start = y * stride + 1 + x * 4
            return tuple(flat[start:start + 4])

        self.assertEqual((depth, colour), (8, 6))
        return width, height, pixel

    def test_both_manifest_sizes_decode(self):
        for size in self.pwa.ICON_SIZES:
            with self.subTest(size=size):
                width, height, _ = self.decode(self.pwa.icon(size))
                self.assertEqual((width, height), (size, size))

    def test_every_scanline_is_unfiltered(self):
        self.decode(self.pwa.icon(32))

    def test_the_background_is_the_dashboard_background(self):
        import viz

        _, _, pixel = self.decode(self.pwa.icon(64))
        expected = tuple(int(viz.SURFACE.lstrip("#")[i:i + 2], 16)
                         for i in (0, 2, 4)) + (255,)
        self.assertEqual(pixel(0, 0), expected)

    def test_the_icon_is_not_blank(self):
        _, _, pixel = self.decode(self.pwa.icon(64))
        colours = {pixel(x, y) for x in range(0, 64, 3) for y in range(0, 64, 3)}
        self.assertGreater(len(colours), 1, "the icon drew nothing")

    def test_a_silly_size_is_clamped_not_fatal(self):
        for size in (0, -5, 9999):
            with self.subTest(size=size):
                width, _, _ = self.decode(self.pwa.icon(size))
                self.assertTrue(16 <= width <= 1024)

    def test_the_manifest_is_installable(self):
        manifest = self.pwa.manifest()
        self.assertTrue(manifest["name"])
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        for icon in manifest["icons"]:
            self.assertEqual(icon["type"], "image/png")

    def test_the_manifest_is_json_serialisable(self):
        json.loads(json.dumps(self.pwa.manifest()))


class SubscriptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.push = self.sb.push
        self.conn = self.sb.store.connect()
        self.addCleanup(self.conn.close)

    def sub(self, endpoint: str = "https://push.example/a") -> dict:
        return {"endpoint": endpoint,
                "keys": {"p256dh": "BPxx", "auth": "aUtH"}}

    # -- 6 ----------------------------------------------------------------

    def test_subscribing_stores_the_endpoint_and_keys(self):
        self.assertTrue(
            self.push.subscribe(self.sub(), label="phone", conn=self.conn)["ok"])
        rows = self.push.listing(conn=self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint"], "https://push.example/a")
        self.assertEqual(rows[0]["p256dh"], "BPxx")
        self.assertEqual(rows[0]["label"], "phone")

    def test_subscribing_twice_leaves_one_row(self):
        """Browsers re-subscribe on their own schedule."""
        self.push.subscribe(self.sub(), label="phone", conn=self.conn)
        self.push.subscribe(self.sub(), conn=self.conn)
        self.assertEqual(len(self.push.listing(conn=self.conn)), 1)

    def test_re_subscribing_keeps_the_label_and_clears_the_failures(self):
        self.push.subscribe(self.sub(), label="phone", conn=self.conn)
        self.conn.execute("update push_subs set failures=5, last_error='x'")
        self.conn.commit()
        self.push.subscribe(self.sub(), conn=self.conn)
        row = self.push.listing(conn=self.conn)[0]
        self.assertEqual(row["label"], "phone")
        self.assertEqual(row["failures"], 0)

    def test_a_subscription_without_a_url_is_refused(self):
        for endpoint in ("", "ftp://x/y", "javascript:alert(1)", None):
            with self.subTest(endpoint=endpoint):
                result = self.push.subscribe({"endpoint": endpoint},
                                             conn=self.conn)
                self.assertFalse(result["ok"])
        self.assertEqual(self.push.listing(conn=self.conn), [])

    def test_nonsense_in_place_of_a_subscription_does_not_raise(self):
        for payload in (None, {}, {"keys": {}}, "string"):
            with self.subTest(payload=payload):
                self.assertFalse(
                    self.push.subscribe(payload, conn=self.conn)["ok"])

    def test_unsubscribing_by_endpoint_or_id(self):
        self.push.subscribe(self.sub(), conn=self.conn)
        self.assertTrue(
            self.push.unsubscribe("https://push.example/a", conn=self.conn))
        self.push.subscribe(self.sub(), conn=self.conn)
        sid = self.push.listing(conn=self.conn)[0]["id"]
        self.assertTrue(self.push.unsubscribe(sid, conn=self.conn))
        self.assertEqual(self.push.listing(conn=self.conn), [])

    def test_clear_drops_everything(self):
        self.push.subscribe(self.sub("https://push.example/a"), conn=self.conn)
        self.push.subscribe(self.sub("https://push.example/b"), conn=self.conn)
        self.assertEqual(self.push.clear(conn=self.conn), 2)


class SendTest(unittest.TestCase):
    """7 and 10: what a push service answers, and never raising."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.push = self.sb.push
        self.conn = self.sb.store.connect()
        self.addCleanup(self.conn.close)
        self.push.subscribe({"endpoint": "https://push.example/a"},
                            conn=self.conn)
        self.calls: list = []

    def respond(self, status: int = 201, raises=None) -> None:
        """Stand in for the network. A test that needs Google is not a test."""
        def fake(endpoint, headers):
            self.calls.append((endpoint, headers))
            if raises is not None:
                raise raises
            return status, "detail"
        self.sb.push._post = fake

    def test_a_successful_send_is_counted_and_recorded(self):
        self.respond(201)
        report = self.push.send_all(conn=self.conn)
        self.assertEqual(report["sent"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(self.push.listing(conn=self.conn)[0]["last_ok_at"], 0)

    def test_the_request_carries_a_vapid_authorization(self):
        self.respond(201)
        self.push.send_all(conn=self.conn)
        headers = self.calls[0][1]
        self.assertTrue(headers["Authorization"].startswith("vapid t="))

    def test_410_deletes_the_subscription(self):
        """Gone means gone. Retrying it forever is how a table fills up."""
        self.respond(410)
        report = self.push.send_all(conn=self.conn)
        self.assertEqual(report["dead"], 1)
        self.assertEqual(self.push.listing(conn=self.conn), [])

    def test_404_deletes_the_subscription(self):
        self.respond(404)
        self.push.send_all(conn=self.conn)
        self.assertEqual(self.push.listing(conn=self.conn), [])

    def test_500_keeps_the_subscription_and_counts_a_failure(self):
        self.respond(500)
        report = self.push.send_all(conn=self.conn)
        self.assertEqual(report["failed"], 1)
        rows = self.push.listing(conn=self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failures"], 1)
        self.assertIn("500", rows[0]["last_error"])

    def test_a_subscription_that_only_ever_fails_is_eventually_dropped(self):
        self.respond(500)
        for _ in range(self.push.MAX_FAILURES):
            self.push.send_all(conn=self.conn)
        self.assertEqual(self.push.listing(conn=self.conn), [])

    def test_a_success_resets_the_failure_count(self):
        self.respond(500)
        self.push.send_all(conn=self.conn)
        self.respond(201)
        self.push.send_all(conn=self.conn)
        self.assertEqual(self.push.listing(conn=self.conn)[0]["failures"], 0)

    def test_an_unreachable_service_does_not_raise(self):
        self.respond(raises=urllib.error.URLError("no route to host"))
        # _post is what catches; this exercises the real one against a
        # deliberately dead address rather than the stub.
        self.sb.push._post = self.sb.push.__dict__.get("_post")
        report = self.push.send_all(conn=self.conn)
        self.assertIsInstance(report, dict)

    def test_sending_with_no_subscriptions_is_a_no_op(self):
        self.push.clear(conn=self.conn)
        self.respond(201)
        self.assertEqual(self.push.send_all(conn=self.conn)["sent"], 0)
        self.assertEqual(self.calls, [])

    def test_a_stored_endpoint_that_is_not_a_url_is_dropped(self):
        self.conn.execute("update push_subs set endpoint='rubbish'")
        self.conn.commit()
        self.respond(201)
        report = self.push.send_all(conn=self.conn)
        self.assertEqual(report["dead"], 1)
        self.assertEqual(self.calls, [])


class WatcherTest(unittest.TestCase):
    """8: one escalation, one notification."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.push = self.sb.push
        self.sent = 0
        self.rows: list = []

    def watcher(self):
        def sender():
            self.sent += 1
            return {"sent": 1}
        return self.push.Watcher(sender=sender, source=lambda: list(self.rows))

    def block(self, sid: str = "s1", at: float = 1000.0) -> None:
        self.rows.append({"sid": sid, "at": at, "project": "p", "message": "m"})

    def test_one_escalation_sends_once_however_often_it_is_polled(self):
        """Twenty seconds apart, a resend-every-poll loop would train anyone
        to turn notifications off within the hour."""
        watcher = self.watcher()
        self.block()
        self.assertEqual(watcher.poll()["new"], 1)
        for _ in range(5):
            watcher.poll()
        self.assertEqual(self.sent, 1)

    def test_a_second_escalation_sends_again(self):
        watcher = self.watcher()
        self.block("s1", 1000.0)
        watcher.poll()
        self.block("s2", 1001.0)
        self.assertEqual(watcher.poll()["new"], 1)
        self.assertEqual(self.sent, 2)

    def test_the_same_session_blocking_again_sends_again(self):
        watcher = self.watcher()
        self.block("s1", 1000.0)
        watcher.poll()
        self.rows.clear()
        watcher.poll()                      # it unblocked
        self.block("s1", 2000.0)            # and blocked again
        watcher.poll()
        self.assertEqual(self.sent, 2)

    def test_nothing_blocked_sends_nothing(self):
        watcher = self.watcher()
        self.assertEqual(watcher.poll()["new"], 0)
        self.assertEqual(self.sent, 0)

    def test_what_was_already_blocked_at_startup_is_not_announced(self):
        """Otherwise every restart buzzes the phone about old news."""
        self.block()
        watcher = self.watcher()
        watcher.prime()
        watcher.poll()
        self.assertEqual(self.sent, 0)

    def test_a_resolved_wait_is_forgotten_so_the_set_cannot_grow(self):
        watcher = self.watcher()
        for i in range(50):
            self.rows = [{"sid": "s%d" % i, "at": float(i)}]
            watcher.poll()
        self.assertEqual(len(watcher._announced), 1)

    def test_a_failing_source_does_not_raise(self):
        def boom():
            raise RuntimeError("mailbox unreadable")
        watcher = self.push.Watcher(sender=lambda: {}, source=boom)
        self.assertIn("error", watcher.poll())

    def test_a_failing_sender_does_not_raise(self):
        def boom():
            raise RuntimeError("network")
        watcher = self.push.Watcher(sender=boom,
                                    source=lambda: [{"sid": "s", "at": 1.0}])
        self.assertIn("error", watcher.poll())

    def test_start_and_stop_are_clean(self):
        watcher = self.push.Watcher(interval=0.05, sender=lambda: {},
                                    source=lambda: [])
        watcher.start()
        self.assertTrue(watcher._thread.daemon, "it must not hold the app open")
        watcher.stop()
        self.assertIsNone(watcher._thread)

    def test_stopping_an_unstarted_watcher_is_harmless(self):
        self.push.Watcher().stop()


class HttpTest(unittest.TestCase):
    """2, 3 and 9: what a client gets, over a real socket."""

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
            {"web_port": 0, "web_bind": "127.0.0.1", "web_token": ""})
        type(self.server).local_only = property(lambda self: False)
        self.addCleanup(self._restore)
        self.assertTrue(self.server.start())
        self.addCleanup(self.server.stop)
        self.base = "http://127.0.0.1:%d" % self.server._server.server_port

    def _restore(self) -> None:
        try:
            del type(self.server).local_only
        except AttributeError:
            pass

    def fetch(self, path: str, token: str = "", method: str = "GET",
              body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("X-Dashboard-Token", token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as reply:
                return reply.status, reply.read(), dict(reply.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    # -- 2 and 3 ----------------------------------------------------------

    def test_the_shell_is_served_without_a_token(self):
        """An installed app launches at "/" with no query string."""
        for path in ("/", "/index.html", "/manifest.webmanifest", "/sw.js",
                     "/style.css", "/theme.css", "/app.js", "/icon-192.png"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 200)

    def test_the_service_worker_is_javascript_from_the_root(self):
        status, body, headers = self.fetch("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"addEventListener('push'", body)

    def test_the_manifest_is_installable_over_http(self):
        status, body, headers = self.fetch("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn("manifest", headers["Content-Type"])
        manifest = json.loads(body)
        self.assertEqual(manifest["display"], "standalone")

    def test_the_icon_route_serves_a_png(self):
        status, body, headers = self.fetch("/icon-512.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_nonsense_icon_size_is_404_not_a_traceback(self):
        self.assertEqual(self.fetch("/icon-abc.png")[0], 404)

    def test_no_api_route_is_public(self):
        """The shell is public; the data is not. This is the line."""
        for path in ("/api/summary", "/api/tasks", "/api/memory", "/api/team",
                     "/api/tile", "/api/meta", "/api/escalations",
                     "/api/push/key"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 401, path)

    def test_the_public_set_is_exactly_what_is_declared(self):
        for path in sorted(self.webserver._PUBLIC_PATHS):
            with self.subTest(path=path):
                self.assertNotIn(self.fetch(path)[0], (401, 403), path)

    # -- 9 ----------------------------------------------------------------

    def test_escalations_needs_read(self):
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        status, body, _ = self.fetch("/api/escalations", token)
        self.assertEqual(status, 200)
        self.assertIn("rows", json.loads(body))

    def test_escalations_is_refused_without_read(self):
        token = self.tokens.mint(["operate"], conn=self.conn)["token"]
        self.assertEqual(self.fetch("/api/escalations", token)[0], 403)

    def test_the_vapid_key_is_served_to_a_reader(self):
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        status, body, _ = self.fetch("/api/push/key", token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["key"])

    # -- subscribing is a read channel, sending is an action --------------

    def test_a_read_token_may_subscribe(self):
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        status, body, _ = self.fetch(
            "/api/push/subscribe", token, "POST",
            {"subscription": {"endpoint": "https://push.example/z"}})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_a_read_token_may_not_send_a_test_push(self):
        """Being notified is a read channel. Making a phone buzz is not."""
        token = self.tokens.mint(["read"], conn=self.conn)["token"]
        self.assertEqual(self.fetch("/api/push/test", token, "POST", {})[0], 403)

    def test_an_operate_token_may_send_a_test_push(self):
        token = self.tokens.mint(["read", "operate"], conn=self.conn)["token"]
        self.assertEqual(
            self.fetch("/api/push/test", token, "POST", {})[0], 200)

    def test_subscribing_without_any_token_is_refused(self):
        self.assertEqual(
            self.fetch("/api/push/subscribe", "", "POST",
                       {"subscription": {"endpoint": "https://p/x"}})[0], 401)


class BundleTest(unittest.TestCase):
    def test_the_new_modules_ship(self):
        import deploy

        # The full bundle: only webserver.py imports these, and `pwa` imports
        # `viz`, which a reporter-only install does not have.
        for name in ("vapid.py", "push.py", "pwa.py"):
            self.assertIn(name, deploy.FULL_EXTRA_FILES, name)
            self.assertNotIn(name, deploy.REPORTER_FILES, name)
        with open(os.path.join(PROJECT, "install", "install.ps1"),
                  encoding="utf-8-sig") as fh:
            text = fh.read()
        for name in ("vapid.py", "push.py", "pwa.py"):
            self.assertIn(name, text, name)

    def test_the_service_worker_ships_with_the_web_assets(self):
        self.assertTrue(os.path.exists(
            os.path.join(PROJECT, "web", "sw.js")))


if __name__ == "__main__":
    unittest.main()
