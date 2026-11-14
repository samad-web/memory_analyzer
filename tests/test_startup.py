"""'Start with Windows' must launch the packaged exe in a frozen build, and
fall back to a non-elevated task when the user can't create an elevated one."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

import widget


def _done(code: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, "", "")


class StartupTaskTest(unittest.TestCase):
    def test_frozen_task_runs_the_exe(self):
        exe = r"C:\Sirahlyzer\Sirahlyzer.exe"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", exe), \
                mock.patch.object(widget, "_schtasks", return_value=_done(0)) as st:
            self.assertTrue(widget.set_startup(True))
        args = st.call_args_list[0].args[0]
        self.assertEqual(args[args.index("/TR") + 1], f'"{exe}"')

    def test_falls_back_when_elevated_task_is_refused(self):
        with mock.patch.object(widget, "_schtasks",
                               side_effect=[_done(1), _done(0)]) as st:
            self.assertTrue(widget.set_startup(True))
        first, second = (c.args[0] for c in st.call_args_list)
        self.assertIn("HIGHEST", first)
        self.assertNotIn("/RL", second)


if __name__ == "__main__":
    unittest.main()
