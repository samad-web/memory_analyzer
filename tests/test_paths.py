"""Frozen-build path resolution: user config beside the exe, bundled web
assets from PyInstaller's _internal (sys._MEIPASS). Getting this wrong made
every dashboard route 404 in the team build."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest import mock

import paths


class FrozenPathsTest(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(paths)   # back to the real (source) layout

    def test_frozen_splits_config_and_bundle(self):
        exe_dir = os.path.join("C:\\", "Sirahlyzer")
        bundle = os.path.join(exe_dir, "_internal")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", bundle, create=True), \
                mock.patch.object(sys, "executable", os.path.join(exe_dir, "Sirahlyzer.exe")):
            importlib.reload(paths)
            self.assertEqual(paths.CONFIG_PATH, os.path.join(exe_dir, "config.json"))
            self.assertEqual(paths.WEB_DIR, os.path.join(bundle, "web"))

    def test_source_web_dir_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(paths.WEB_DIR, "index.html")))


if __name__ == "__main__":
    unittest.main()
