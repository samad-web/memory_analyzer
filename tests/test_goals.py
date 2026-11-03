"""GOAL.md in, milestones out -- and status that only moves on evidence.

The three roles are tested separately because they are deliberately separate:
``sync`` owns structure, ``verify`` owns status, ``export`` writes status back.
"""

from __future__ import annotations

import os
import unittest

from ._env import PROJECT, Sandbox

SAMPLE = """# Goal

Ship the control plane.
It should carry its own end goal.

## Constraints

- No new runtime dependencies. Standard library only, plus what
  the tile already needs.
- Hooks never touch the network.

## Milestones

- [x] Already finished
      check: python -c "raise SystemExit(0)"
- [~] In progress now
      acceptance: a finished turn yields its files and commands
                  without calling a model
      check: python -c "raise SystemExit(0)"
- [ ] Not started
      acceptance: prose only, nothing to run
- [!] Waiting on someone
      blocked: needs a VAPID key
"""


class ParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.parsed = self.sb.goals.parse(SAMPLE)

    def test_statement(self):
        self.assertEqual(
            self.parsed["statement"],
            "Ship the control plane. It should carry its own end goal.")

    def test_wrapped_constraint_is_kept_whole(self):
        """A human wrapping a line means one constraint, not half of one."""
        self.assertEqual(len(self.parsed["constraints"]), 2)
        self.assertTrue(
            self.parsed["constraints"][0].endswith("the tile already needs."),
            self.parsed["constraints"][0])

    def test_wrapped_acceptance_is_kept_whole(self):
        second = self.parsed["milestones"][1]
        self.assertEqual(
            second["acceptance"],
            "a finished turn yields its files and commands without calling a model")

    def test_marks_map_to_statuses(self):
        got = [(m["title"], m["status"]) for m in self.parsed["milestones"]]
        self.assertEqual(got, [
            ("Already finished", "done"),
            ("In progress now", "active"),
            ("Not started", "pending"),
            ("Waiting on someone", "blocked"),
        ])

    def test_blocked_reason_is_captured(self):
        self.assertEqual(self.parsed["milestones"][3]["blocked_by"],
                         "needs a VAPID key")

    def test_ordinals_follow_file_order(self):
        self.assertEqual([m["ordinal"] for m in self.parsed["milestones"]],
                         [0, 1, 2, 3])

    def test_backticked_check_is_unwrapped(self):
        parsed = self.sb.goals.parse(
            "## Milestones\n- [ ] T\n      check: `pytest -q`\n")
        self.assertEqual(parsed["milestones"][0]["check_cmd"], "pytest -q")

    def test_empty_and_junk_input_is_tolerated(self):
        for text in ("", "# Nothing\n", "## Milestones\n", "random prose\n"):
            parsed = self.sb.goals.parse(text)
            self.assertIsInstance(parsed["milestones"], list)

    def test_unknown_section_is_ignored_not_rejected(self):
        parsed = self.sb.goals.parse(
            SAMPLE + "\n## Notes\n- something irrelevant\n")
        self.assertEqual(len(parsed["milestones"]), 4)
        self.assertEqual(len(parsed["constraints"]), 2)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.repo = self.sb.path("repo")
        os.makedirs(self.repo, exist_ok=True)
        self.write(SAMPLE)

    def write(self, text: str) -> None:
        with open(os.path.join(self.repo, "GOAL.md"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_sync_stores_goal_and_milestones(self):
        r = self.sb.goals.sync("p", self.repo)
        self.assertTrue(r["ok"])
        self.assertEqual(r["added"], 4)
        b = self.sb.store.brief("p")
        self.assertEqual(b["total"], 4)
        self.assertEqual(b["done"], 1)
        self.assertEqual(b["active"]["title"], "In progress now")
        self.assertEqual(b["next"]["title"], "Not started")

    def test_sync_is_idempotent(self):
        self.sb.goals.sync("p", self.repo)
        r = self.sb.goals.sync("p", self.repo)
        self.assertEqual(r["added"], 0)
        self.assertEqual(r["updated"], 4)
        self.assertEqual(self.sb.store.brief("p")["total"], 4)

    def test_missing_goal_file_reports_rather_than_raises(self):
        r = self.sb.goals.sync("p", self.sb.path("nowhere"))
        self.assertFalse(r["ok"])
        self.assertIn("GOAL.md", r["error"])

    def test_verified_status_survives_a_resync(self):
        """The file owns structure; the database owns how far along it is."""
        self.sb.goals.sync("p", self.repo)
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        conn.execute("update milestones set status='done', evidence='proved' "
                     "where title='Not started'")
        conn.commit()
        self.sb.goals.sync("p", self.repo, conn=conn)
        row = conn.execute(
            "select status from milestones where title='Not started'").fetchone()
        self.assertEqual(row["status"], "done")

    def test_reopening_by_unticking_is_honoured(self):
        self.sb.goals.sync("p", self.repo)
        self.write(SAMPLE.replace("- [x] Already finished",
                                  "- [ ] Already finished"))
        self.sb.goals.sync("p", self.repo)
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "select status from milestones where title='Already finished'"
        ).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_edited_prose_updates_without_losing_status(self):
        self.sb.goals.sync("p", self.repo)
        self.write(SAMPLE.replace("acceptance: prose only, nothing to run",
                                  "acceptance: rewritten criterion"))
        self.sb.goals.sync("p", self.repo)
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "select acceptance from milestones where title='Not started'"
        ).fetchone()
        self.assertEqual(row["acceptance"], "rewritten criterion")


class VerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.repo = self.sb.path("repo")
        os.makedirs(self.repo, exist_ok=True)

    def goal(self, body: str) -> None:
        with open(os.path.join(self.repo, "GOAL.md"), "w", encoding="utf-8") as fh:
            fh.write("# Goal\nx\n\n## Milestones\n" + body)
        self.sb.goals.sync("p", self.repo)

    def test_passing_check_marks_done_with_evidence(self):
        self.goal('- [ ] Works\n      check: python -c "raise SystemExit(0)"\n')
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "passed")
        self.assertEqual(rows[0]["status"], "done")
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        ev = conn.execute("select evidence from milestones").fetchone()["evidence"]
        self.assertIn("passed", ev)

    def test_failing_check_does_not_mark_done(self):
        self.goal('- [ ] Broken\n      check: python -c "raise SystemExit(3)"\n')
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "failed")
        self.assertNotEqual(rows[0]["status"], "done")

    def test_a_failing_check_reopens_a_done_milestone(self):
        """Regressions should come back, not stay ticked."""
        self.goal('- [x] Was fine\n      check: python -c "raise SystemExit(1)"\n')
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "failed")
        self.assertEqual(rows[0]["status"], "active")

    def test_milestone_without_a_check_is_unverifiable_not_done(self):
        """Saying so beats quietly calling it finished."""
        self.goal("- [ ] Needs a human\n      acceptance: looks right\n")
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "unverifiable")
        self.assertNotEqual(rows[0]["status"], "done")

    def test_unrunnable_command_is_a_failure_not_a_crash(self):
        self.goal("- [ ] Bad\n      check: this-command-does-not-exist-xyz\n")
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "failed")

    def test_only_filter_limits_what_runs(self):
        self.goal('- [ ] Alpha\n      check: python -c "raise SystemExit(0)"\n'
                  '- [ ] Beta\n      check: python -c "raise SystemExit(0)"\n')
        rows = self.sb.goals.verify("p", self.repo, only="alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Alpha")


    def probe(self, name: str, body: str) -> str:
        """Write a check script into the repo and return the command to run it.

        A real file rather than a ``python -c`` one-liner: the quoting of a
        nested SQL string inside a shell string inside a Markdown field is a
        way to test the escaping rather than the behaviour.
        """
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.path.insert(0, %r)\nimport store\n" % PROJECT)
            fh.write(body)
        return "python %s" % name

    def test_a_check_command_can_touch_the_store(self):
        """Verify must not hold a write transaction across a check command.

        The results used to be committed once at the end, which left a write
        transaction open from the first milestone onward. Any later check that
        opened the store -- and the interesting checks all do, since the store
        is what this project builds -- died on a locked database and was
        recorded as a failure it never had.
        """
        writer = self.probe("probe_write.py", (
            "conn = store.connect()\n"
            "conn.execute(\"insert into decisions (proj, title, decided_at) \"\n"
            "             \"values ('p', 'probe', 0)\")\n"
            "conn.commit()\n"
            "conn.close()\n"))
        self.goal(
            '- [ ] First\n      check: python -c "raise SystemExit(0)"\n'
            "- [ ] Second\n      check: %s\n" % writer)
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[0]["result"], "passed")
        self.assertEqual(rows[1]["result"], "passed", rows[1]["detail"])

    def test_each_result_is_committed_before_the_next_check_runs(self):
        """A later check sees the earlier milestone's recorded result."""
        reader = self.probe("probe_read.py", (
            "conn = store.connect()\n"
            "n = conn.execute(\"select count(*) n from milestones \"\n"
            "                 \"where status='done'\").fetchone()[0]\n"
            "conn.close()\n"
            "raise SystemExit(0 if n == 1 else 9)\n"))
        self.goal(
            '- [ ] First\n      check: python -c "raise SystemExit(0)"\n'
            "- [ ] Second\n      check: %s\n" % reader)
        rows = self.sb.goals.verify("p", self.repo)
        self.assertEqual(rows[1]["result"], "passed", rows[1]["detail"])


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.repo = self.sb.path("repo")
        os.makedirs(self.repo, exist_ok=True)
        self.path = os.path.join(self.repo, "GOAL.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE)
        self.sb.goals.sync("p", self.repo)

    def read(self) -> str:
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_status_is_written_back_into_the_checkboxes(self):
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        conn.execute("update milestones set status='done' where title='Not started'")
        conn.commit()
        r = self.sb.goals.export("p", self.repo)
        self.assertTrue(r["ok"])
        self.assertEqual(r["changed"], 1)
        self.assertIn("- [x] Not started", self.read())

    def test_prose_is_left_exactly_as_written(self):
        before = self.read()
        self.sb.goals.export("p", self.repo)
        after = self.read()
        self.assertEqual(
            [l for l in before.splitlines() if "[" not in l],
            [l for l in after.splitlines() if "[" not in l])

    def test_no_change_means_no_write(self):
        r = self.sb.goals.export("p", self.repo)
        self.assertEqual(r["changed"], 0)

    def test_round_trip_is_stable(self):
        self.sb.goals.export("p", self.repo)
        first = self.read()
        self.sb.goals.sync("p", self.repo)
        self.sb.goals.export("p", self.repo)
        self.assertEqual(first, self.read())


class ScaffoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.repo = self.sb.path("repo")
        os.makedirs(self.repo, exist_ok=True)

    def test_init_writes_a_parseable_template(self):
        r = self.sb.goals.scaffold(self.repo)
        self.assertTrue(r["ok"])
        with open(r["path"], encoding="utf-8") as fh:
            parsed = self.sb.goals.parse(fh.read())
        self.assertEqual(len(parsed["milestones"]), 1)

    def test_init_refuses_to_clobber(self):
        self.sb.goals.scaffold(self.repo)
        r = self.sb.goals.scaffold(self.repo)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
