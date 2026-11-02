"""What a fresh session is told, and the two ways that could go wrong.

It could say too much -- the budget exists because this is spent on every single
session start. Or it could crash, and a hook that crashes while printing a
rupee sign on a cp1252 console is a hook that worked in testing and broke on a
Tuesday.
"""

from __future__ import annotations

import json
import os
import time
import unittest

from ._env import Sandbox

GOAL = """# Goal

Carry the end goal so nobody has to retype it.

## Constraints

- No new runtime dependencies.

## Milestones

- [x] Finished thing
- [~] Live thing
      acceptance: it works
      check: python -c "raise SystemExit(0)"
- [ ] Future thing
- [!] Stuck thing
      blocked: needs a key
"""


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.repo = self.sb.path("repo")
        os.makedirs(self.repo, exist_ok=True)
        with open(os.path.join(self.repo, "GOAL.md"), "w", encoding="utf-8") as fh:
            fh.write(GOAL)
        self.sb.goals.sync("p", self.repo)

    def obs(self, **fields) -> None:
        now = time.time()
        base = {"proj": "p", "title": "did a thing", "narrative": "",
                "facts": "", "prompt": "", "files_modified": "[]", "tests": ""}
        base.update(fields)
        conn = self.sb.store.connect()
        conn.execute(
            "insert into observations (proj, ended_at, created_at, title, "
            " narrative, facts, prompt, files_modified, tests) "
            "values (?,?,?,?,?,?,?,?,?)",
            (base["proj"], now, now, base["title"], base["narrative"],
             base["facts"], base["prompt"], base["files_modified"],
             base["tests"]))
        conn.commit()
        conn.close()

    def test_goal_and_constraints_appear(self):
        text = self.sb.context.render("p")
        self.assertIn("Carry the end goal", text)
        self.assertIn("No new runtime dependencies", text)

    def test_live_and_pending_milestones_appear(self):
        text = self.sb.context.render("p")
        self.assertIn("Live thing", text)
        self.assertIn("Future thing", text)
        self.assertIn("it works", text)

    def test_finished_milestones_are_a_count_not_a_list(self):
        """Done work is history; what matters is the live one and what is next."""
        text = self.sb.context.render("p")
        self.assertIn("1 of 4 done", text)
        self.assertNotIn("Finished thing", text)

    def test_blocked_reason_appears(self):
        self.assertIn("needs a key", self.sb.context.render("p"))

    def test_recent_work_appears_with_changed_files(self):
        self.obs(title="fixed the divisor",
                 files_modified=json.dumps(["C:/repo/msi_ec.py"]))
        text = self.sb.context.render("p")
        self.assertIn("fixed the divisor", text)
        self.assertIn("msi_ec.py", text)

    def test_test_outcome_appears(self):
        self.obs(title="ran the suite", tests="7 passed")
        self.assertIn("7 passed", self.sb.context.render("p"))

    def test_unknown_project_renders_nothing(self):
        self.assertEqual(self.sb.context.render("never-heard-of-it"), "")

    def test_budget_is_respected(self):
        for i in range(60):
            self.obs(title="observation number %d with some padding text" % i)
        text = self.sb.context.render("p", budget=900)
        self.assertLessEqual(len(text), 900)

    def test_goal_survives_a_tight_budget(self):
        """Trimming takes the oldest summaries, never the reason for the work."""
        for i in range(60):
            self.obs(title="observation number %d" % i)
        text = self.sb.context.render("p", budget=400)
        self.assertIn("Carry the end goal", text)


class SafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_emit_is_valid_json_with_the_expected_shape(self):
        import io
        import sys

        repo = self.sb.path("repo")
        os.makedirs(repo, exist_ok=True)
        with open(os.path.join(repo, "GOAL.md"), "w", encoding="utf-8") as fh:
            fh.write(GOAL)
        self.sb.goals.sync("p", repo)

        captured = io.StringIO()
        real = sys.stdout
        sys.stdout = captured
        try:
            self.sb.context.emit("p")
        finally:
            sys.stdout = real
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "SessionStart")
        self.assertIn("Carry the end goal",
                      payload["hookSpecificOutput"]["additionalContext"])

    def test_characters_outside_cp1252_do_not_crash_the_writer(self):
        """A real prompt contains real characters. The console may not want them."""
        conn = self.sb.store.connect()
        now = time.time()
        conn.execute(
            "insert into observations (proj, ended_at, created_at, title, "
            " narrative, facts, prompt, files_modified, tests) "
            "values (?,?,?,?,?,?,?,?,?)",
            ("p", now, now, "priced it at \u20b9250 \u2014 an em dash too",
             "", "", "", "[]", ""))
        conn.commit()
        conn.close()
        text = self.sb.context.render("p")
        self.assertIn("\u20b9250", text)
        # The writer must survive a stream that cannot encode it.
        import io
        import sys

        class Cp1252Stream(io.TextIOBase):
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, s):               # noqa: D102
                s.encode("cp1252")            # raises on the rupee sign
                return len(s)

        real = sys.stdout
        sys.stdout = Cp1252Stream()
        try:
            self.sb.context.emit("p")         # must not raise
        finally:
            sys.stdout = real

    def test_render_never_raises_on_a_broken_store(self):
        self.assertIsInstance(self.sb.context.render(""), str)

    def test_emit_returns_zero_even_with_nothing_to_say(self):
        self.assertEqual(self.sb.context.emit("nothing"), 0)


if __name__ == "__main__":
    unittest.main()
