"""Hooks wired to the frozen SirahlyzerBridge.exe: re-running setup must not
duplicate them, uninstall must remove them, and foreign hooks must survive."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import hooks

EXE = r"C:\Sirahlyzer\SirahlyzerBridge.exe"
FOREIGN = {"hooks": [{"type": "command", "command": "other-tool --ping"}]}


class ExeHooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [FOREIGN]}}, fh)
        patcher = mock.patch.object(hooks, "settings_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hooks(self) -> dict:
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh).get("hooks", {})

    def test_reinstall_does_not_duplicate(self):
        hooks.install(exe=EXE, backup=False)
        hooks.install(exe=EXE, backup=False)
        for event, *_ in hooks.HOOKS:
            ours = [g for g in self._hooks()[event] if g is not FOREIGN and g != FOREIGN]
            self.assertEqual(len(ours), 1, event)

    def test_uninstall_removes_exe_hooks_keeps_foreign(self):
        hooks.install(exe=EXE, backup=False)
        report = hooks.uninstall()
        self.assertEqual(report["removed"], len(hooks.HOOKS))
        self.assertEqual(self._hooks(), {"Stop": [FOREIGN]})


if __name__ == "__main__":
    unittest.main()
