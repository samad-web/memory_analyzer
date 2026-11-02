"""The hook entry point: what it logs, and what it refuses to log.

Two behaviours are load-bearing here. A ``resume`` must be written only when a
block was actually open, because an unpaired one is read by nothing and used to
be 87% of the log. And a session must keep one identity on the board however far
it wanders from the directory it started in.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from ._env import Sandbox


class NotifyCase(unittest.TestCase):
    """Drives ``notify.main`` with a payload, against a sandboxed mailbox."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.notify = self.sb.notify
        self.alert = self.sb.agent_alert
        self.events = self.sb.events
        self.repo = self.sb.path("repo")
        os.makedirs(os.path.join(self.repo, ".git"), exist_ok=True)
        self._stdin = sys.stdin

    def run_hook(self, action: str, message: str = "", sid: str = "s1",
                 cwd: str | None = None) -> None:
        payload = {"session_id": sid, "cwd": cwd or self.repo,
                   "message": message, "prompt": message}
        self.notify.read_payload = lambda: payload
        sys.stdin = open(os.devnull)
        try:
            self.notify.main(["notify.py", action] + ([message] if message else []))
        finally:
            sys.stdin.close()
            sys.stdin = self._stdin

    def logged(self) -> list[dict]:
        path = self.events.EVENTS_PATH
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def kinds(self) -> list[str]:
        return [e["ev"] for e in self.logged()]


class ResumeTest(NotifyCase):
    def test_clean_turn_logs_no_resume(self):
        """Four turns in five never block; none of them should log a resume."""
        self.run_hook("prompt", "do the thing")
        self.notify.WORKING_FRESH_SECONDS = 0      # force past the no-churn check
        for _ in range(8):
            self.run_hook("working")
        self.run_hook("done", "did it")
        self.assertEqual(self.kinds().count("resume"), 0)
        self.assertEqual(self.kinds(), ["prompt", "done"])

    def test_blocked_turn_logs_exactly_one_resume(self):
        self.run_hook("prompt", "do the thing")
        self.run_hook("needs-input", "may I run git push?")
        self.notify.WORKING_FRESH_SECONDS = 0
        for _ in range(5):
            self.run_hook("working")
        self.run_hook("done", "pushed")
        self.assertEqual(self.kinds().count("resume"), 1)
        self.assertEqual(self.kinds(), ["prompt", "wait", "resume", "done"])

    def test_reply_time_is_still_measurable(self):
        """The one resume that matters is the one still being logged."""
        self.run_hook("prompt", "task")
        self.run_hook("needs-input", "blocked")
        self.notify.WORKING_FRESH_SECONDS = 0
        self.run_hook("working")
        self.run_hook("done", "done")
        replies, tasks = self.sb.stats._pairings(self.logged())
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(tasks), 1)

    def test_mailbox_still_flips_back_to_working(self):
        self.run_hook("prompt", "task")
        self.run_hook("needs-input", "blocked")
        key = self.alert.key_for(self.repo, "s1")
        self.assertEqual(self.alert.read_one(key).kind, self.alert.NEEDS_INPUT)
        self.notify.WORKING_FRESH_SECONDS = 0
        self.run_hook("working")
        self.assertEqual(self.alert.read_one(key).kind, self.alert.WORKING)

    def test_fresh_working_row_is_left_untouched(self):
        self.run_hook("prompt", "task")
        before = self.alert.read_one(self.alert.key_for(self.repo, "s1")).at
        self.run_hook("working")        # fresh, so a no-op
        after = self.alert.read_one(self.alert.key_for(self.repo, "s1")).at
        self.assertEqual(before, after)


class ProjectIdentityTest(NotifyCase):
    """A session should not be renamed by whichever subdirectory it is in."""

    def test_subdirectory_is_named_for_its_repository(self):
        sub = os.path.join(self.repo, "platform")
        os.makedirs(sub, exist_ok=True)
        self.assertEqual(self.alert.project_name(sub), os.path.basename(self.repo))

    def test_one_session_keeps_one_row_across_a_cd(self):
        sub = os.path.join(self.repo, "platform", "deep")
        os.makedirs(sub, exist_ok=True)
        self.assertEqual(self.alert.key_for(self.repo, "s1"),
                         self.alert.key_for(sub, "s1"))

    def test_moving_between_subdirectories_leaves_no_orphan(self):
        a = os.path.join(self.repo, "one")
        b = os.path.join(self.repo, "two")
        os.makedirs(a, exist_ok=True)
        os.makedirs(b, exist_ok=True)
        self.run_hook("prompt", "start in one", cwd=a)
        self.notify.WORKING_FRESH_SECONDS = 0
        self.run_hook("working", cwd=b)
        files = os.listdir(self.alert.ALERT_DIR)
        self.assertEqual(len(files), 1, files)

    def test_nested_checkout_wins_over_its_parent(self):
        """The nearest marker is the project; an inner repo really is one."""
        inner = os.path.join(self.repo, "vendor", "inner")
        os.makedirs(os.path.join(inner, ".git"), exist_ok=True)
        self.assertEqual(self.alert.project_name(inner), "inner")

    def test_directory_without_a_repository_falls_back_to_its_own_name(self):
        plain = self.sb.path("no-repo-here")
        os.makedirs(plain, exist_ok=True)
        self.assertEqual(self.alert.project_name(plain), "no-repo-here")

    def test_two_checkouts_of_the_same_name_do_not_collide(self):
        one = self.sb.path("a", "same")
        two = self.sb.path("b", "same")
        for p in (one, two):
            os.makedirs(os.path.join(p, ".git"), exist_ok=True)
        self.assertEqual(self.alert.project_name(one),
                         self.alert.project_name(two))
        self.assertNotEqual(self.alert.key_for(one, "s1"),
                            self.alert.key_for(two, "s1"))

    def test_project_root_is_bounded(self):
        """A deep tree with no marker must not walk forever."""
        deep = self.sb.path(*(["x"] * 40))
        os.makedirs(deep, exist_ok=True)
        self.assertIsInstance(self.alert.project_root(deep), str)


class LifecycleTest(NotifyCase):
    def test_session_end_clears_the_row(self):
        self.run_hook("prompt", "task")
        self.run_hook("end")
        self.assertEqual(os.listdir(self.alert.ALERT_DIR), [])
        self.assertIn("end", self.kinds())

    def test_unknown_action_does_not_raise(self):
        self.run_hook("not-a-real-action", "x")

    def test_context_action_emits_nothing_for_an_unknown_project(self):
        """Silence is the right output when there is no memory yet."""
        self.assertEqual(self.notify.main(["notify.py", "context"]), 0)


if __name__ == "__main__":
    unittest.main()
