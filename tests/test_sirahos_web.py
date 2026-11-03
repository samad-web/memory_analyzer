"""The /sirahos/* endpoints are the UI's seam to the bridge.

The handler methods themselves are tested through a live HTTP server
elsewhere; here we test the module-level helpers that implement each route,
since they carry the actual logic. The handler is a two-line dispatch.

Three things matter:

1. **Sign-in writes the token exactly once and remembers who signed in**, so
   the password lives in memory for one function call and is never persisted.
2. **Sign-out deletes the token**, leaving config.json alone -- the mapping
   survives a sign-out.
3. **Saving the mapping only accepts (string, string) pairs**, so a browser
   that posts garbage cannot smuggle a dict or a list through to config.json.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ._env import Sandbox


class SignInTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.webserver = self.sb.mod["pwa"].__dict__  # placeholder
        from importlib import import_module, reload
        import webserver as webserver_mod
        self.webserver = reload(webserver_mod)
        # Point CONFIG_PATH into the sandbox so the test's writes do not
        # mutate the real config.json beside this project.
        self._real_cfg = self.webserver.CONFIG_PATH
        self.cfg_path = os.path.join(self.sb.root, "config.json")
        self.webserver.CONFIG_PATH = self.cfg_path
        self.addCleanup(lambda: setattr(self.webserver, "CONFIG_PATH", self._real_cfg))
        # Seed a config with the API URL set.
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"sirahos": {"api_url": "https://sirahos.test",
                                    "folder_project_map": {}}}, fh)

    def test_signin_writes_token_and_remembers_email(self):
        with mock.patch.object(
            self.webserver.sirahos, "login",
            return_value={"accessToken": "jwt-abc",
                          "user": {"email": "me@test"}},
        ):
            result = self.webserver.sirahos_signin({
                "email": "me@test", "password": "secret",
            })
        self.assertEqual(result, {"ok": True})
        # Token on disk.
        self.assertTrue(os.path.exists(self.webserver.bridge.JWT_PATH))
        # Password NOT on disk anywhere.
        with open(self.webserver.bridge.JWT_PATH, "rb") as fh:
            blob = fh.read()
        self.assertNotIn(b"secret", blob)
        # Config remembers the email, nothing more.
        with open(self.cfg_path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["sirahos"]["last_signed_in_as"], "me@test")

    def test_signin_rejects_blank_credentials(self):
        result = self.webserver.sirahos_signin({"email": "", "password": ""})
        self.assertFalse(result["ok"])
        self.assertIn("required", result["error"].lower())

    def test_signin_without_url_fails_cleanly(self):
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"sirahos": {"api_url": ""}}, fh)
        result = self.webserver.sirahos_signin({
            "email": "me@test", "password": "p",
        })
        self.assertFalse(result["ok"])
        self.assertIn("URL", result["error"])

    def test_signin_auth_error_bubbles_up(self):
        with mock.patch.object(
            self.webserver.sirahos, "login",
            side_effect=self.webserver.sirahos.AuthError("bad password",
                                                        status=401),
        ):
            result = self.webserver.sirahos_signin({
                "email": "me@test", "password": "wrong",
            })
        self.assertFalse(result["ok"])
        self.assertIn("bad password", result["error"])

    def test_signout_removes_token(self):
        # Pre-write a token so signout has something to remove.
        os.makedirs(os.path.dirname(self.webserver.bridge.JWT_PATH), exist_ok=True)
        with open(self.webserver.bridge.JWT_PATH, "w", encoding="utf-8") as fh:
            fh.write("placeholder")
        result = self.webserver.sirahos_signout()
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(self.webserver.bridge.JWT_PATH))

    def test_signout_without_token_is_fine(self):
        result = self.webserver.sirahos_signout()
        self.assertTrue(result["ok"])


class MappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        from importlib import reload
        import webserver as webserver_mod
        self.webserver = reload(webserver_mod)
        self._real_cfg = self.webserver.CONFIG_PATH
        self.cfg_path = os.path.join(self.sb.root, "config.json")
        self.webserver.CONFIG_PATH = self.cfg_path
        self.addCleanup(lambda: setattr(self.webserver, "CONFIG_PATH", self._real_cfg))
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"sirahos": {"api_url": "https://sirahos.test",
                                    "folder_project_map": {}}}, fh)

    def test_save_accepts_string_only_pairs(self):
        result = self.webserver.sirahos_save_mapping({
            "folder_project_map": {
                "voicetranscriber": "clabc",
                "WealthOS-Mobile": "clxyz",
            },
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        with open(self.cfg_path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["sirahos"]["folder_project_map"],
                         {"voicetranscriber": "clabc", "WealthOS-Mobile": "clxyz"})

    def test_save_drops_non_string_values(self):
        """A browser that posts a list or a dict in a value is probably buggy;
        drop the bad entry rather than refusing the whole call."""
        result = self.webserver.sirahos_save_mapping({
            "folder_project_map": {
                "good": "clabc",
                "bad1": {"nested": "dict"},
                "bad2": ["list"],
                "bad3": 42,
            },
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        with open(self.cfg_path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["sirahos"]["folder_project_map"], {"good": "clabc"})

    def test_save_rejects_non_object_body(self):
        result = self.webserver.sirahos_save_mapping({
            "folder_project_map": "not an object",
        })
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
