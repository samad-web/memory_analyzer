"""The extractor: a turn's transcript in, an observation out, no model called.

The transcripts here are synthetic but shaped exactly like the real ones --
``message.role``, a content list of typed blocks, ``tool_use`` carrying an id
that a later ``tool_result`` refers back to, ``isSidechain`` on subagent lines.
"""

from __future__ import annotations

import os
import unittest

from ._env import Sandbox, assistant, text, tool_result, tool_use, transcript, user


PYTEST_PASS = """
============================= test session starts =============================
collected 7 items
tests/test_x.py .......                                                  [100%]
============================== 7 passed in 0.42s ==============================
"""

PYTEST_FAIL = """
collected 7 items
tests/test_x.py ..F....                                                  [100%]
=========================== short test summary info ===========================
FAILED tests/test_x.py::test_thing - AssertionError
========================= 1 failed, 6 passed in 0.51s =========================
"""

JEST_FAIL = """
Tests:       1 failed, 4 passed, 5 total
Snapshots:   0 total
"""

CARGO_PASS = "test result: ok. 12 passed; 0 failed; 0 ignored"


class ExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.observe = self.sb.observe

    def make(self, *entries: dict, name: str = "t.jsonl") -> str:
        return self.sb.write(name, transcript(*entries))

    def test_files_commands_and_tools(self):
        path = self.make(
            user(text("make the fan read right")),
            assistant(
                tool_use("Read", "t1", file_path="C:/repo/msi_ec.py"),
                tool_use("Edit", "t2", file_path="C:/repo/msi_ec.py",
                         old_string="a", new_string="b"),
                tool_use("Write", "t3", file_path="C:/repo/notes.md", content="x"),
                tool_use("Bash", "t4", command="git diff --stat",
                         description="show the diff"),
                text("Fixed the divisor."),
            ),
        )
        f = self.observe.extract(path, "s1")
        self.assertEqual(f["files_modified"], ["C:/repo/msi_ec.py", "C:/repo/notes.md"])
        self.assertEqual(f["files_read"], ["C:/repo/msi_ec.py"])
        self.assertEqual(f["commands"], ["git diff --stat"])
        self.assertEqual(set(f["tools"]), {"Read", "Edit", "Write", "Bash"})
        self.assertEqual(f["narrative"], "Fixed the divisor.")
        self.assertEqual(f["branch"], "master")

    def test_last_assistant_text_wins_as_narrative(self):
        path = self.make(
            assistant(text("First I will look around.")),
            assistant(text("Done: the divisor was wrong.")),
        )
        self.assertEqual(
            self.observe.extract(path, "s1")["narrative"],
            "Done: the divisor was wrong.")

    def test_subagent_lines_are_skipped_by_default(self):
        path = self.make(
            assistant(tool_use("Edit", "t1", file_path="C:/repo/main.py"),
                      text("main agent")),
            assistant(tool_use("Edit", "t2", file_path="C:/repo/sub.py"),
                      sidechain=True),
        )
        f = self.observe.extract(path, "s1")
        self.assertEqual(f["files_modified"], ["C:/repo/main.py"])

    def test_subagent_lines_can_be_included(self):
        path = self.make(
            assistant(tool_use("Edit", "t1", file_path="C:/repo/main.py")),
            assistant(tool_use("Edit", "t2", file_path="C:/repo/sub.py"),
                      sidechain=True),
        )
        f = self.observe.extract(path, "s1", main_only=False)
        self.assertEqual(len(f["files_modified"]), 2)

    def test_incremental_read_advances(self):
        path = self.make(assistant(tool_use("Edit", "t1", file_path="C:/a.py")))
        first = self.observe.extract(path, "s1")
        self.assertEqual(first["files_modified"], ["C:/a.py"])
        # Nothing new yet.
        self.assertEqual(self.observe.extract(path, "s1"), {})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(transcript(assistant(tool_use("Edit", "t2",
                                                   file_path="C:/b.py"))))
        second = self.observe.extract(path, "s1")
        self.assertEqual(second["files_modified"], ["C:/b.py"])

    def test_advance_false_does_not_consume(self):
        path = self.make(assistant(tool_use("Edit", "t1", file_path="C:/a.py")))
        self.observe.extract(path, "s1", advance=False)
        again = self.observe.extract(path, "s1", advance=False)
        self.assertEqual(again["files_modified"], ["C:/a.py"])

    def test_missing_transcript_returns_blank(self):
        self.assertEqual(self.observe.extract("C:/nope.jsonl", "s1"), {})
        self.assertEqual(self.observe.extract("", "s1"), {})

    def test_duplicate_paths_collapse(self):
        path = self.make(assistant(
            tool_use("Edit", "t1", file_path="C:/a.py"),
            tool_use("Edit", "t2", file_path="C:/a.py"),
            tool_use("Edit", "t3", file_path="C:/a.py"),
        ))
        self.assertEqual(self.observe.extract(path, "s1")["files_modified"],
                         ["C:/a.py"])


class TestCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.observe = self.sb.observe

    def verdict(self, command: str, output: str) -> str:
        path = self.sb.write(
            "t.jsonl",
            transcript(
                assistant(tool_use("Bash", "t1", command=command)),
                user(tool_result("t1", output)),
            ))
        return self.observe.extract(path, "s-%d" % id(output))["tests"]

    def test_pytest_pass(self):
        self.assertEqual(self.verdict("python -m pytest -q", PYTEST_PASS),
                         "7 passed")

    def test_pytest_failure_is_reported(self):
        """pytest prints "1 failed, 6 passed". A passed-first pattern matches
        the "6 passed" inside that and calls a broken run green, which is the
        one way this field must never be wrong."""
        v = self.verdict("pytest tests/", PYTEST_FAIL)
        self.assertIn("1 failed", v)
        self.assertNotEqual(v, "6 passed")

    def test_no_runner_output_is_ever_read_as_a_pass(self):
        """Across every shape of failing output, the verdict must not say
        "passed" without also saying "failed"."""
        failing = [
            ("pytest", PYTEST_FAIL),
            ("npm test", JEST_FAIL),
            ("python -m unittest", "FAILED (failures=2)"),
            ("go test ./...", "FAIL\tgithub.com/x/y\t0.01s"),
            ("cargo test", "test result: FAILED. 3 passed; 2 failed"),
            ("pytest", "=== 2 failed, 5 passed in 1.2s ==="),
            ("python -m unittest", "Ran 4 tests\n\nFAILED (errors=1)"),
        ]
        for cmd, out in failing:
            v = self.verdict(cmd, out)
            self.assertTrue(
                ("fail" in v.lower()) or ("unparsed" in v),
                "%s -> %r must not read as a clean pass" % (cmd, v))

    def test_go_summary_is_not_applied_to_other_runners(self):
        """"ok 1 - something" from a TAP run, or "OK installed" from a package
        manager, used to be read as a go-style pass."""
        v = self.verdict("pytest", "ok 1 - set up fixtures\nok 2 - tore down")
        self.assertNotEqual(v, "ok")
        self.assertIn("unparsed", v)

    def test_unittest_ok_still_reads_as_a_pass(self):
        self.assertEqual(
            self.verdict("python -m unittest discover", "Ran 9 tests\n\nOK"),
            "ok")

    def test_unittest_counted_line_is_preferred(self):
        self.assertEqual(
            self.verdict("python -m unittest", "=== 9 passed in 0.5s ==="),
            "9 passed")

    def test_jest_failed_first_ordering(self):
        self.assertEqual(self.verdict("npm test", JEST_FAIL), "1 failed, 4 passed")

    def test_cargo(self):
        self.assertIn("12 passed", self.verdict("cargo test", CARGO_PASS))

    def test_cd_prefix_still_recognised(self):
        self.assertEqual(
            self.verdict('cd "C:/repo" && python -m pytest -q', PYTEST_PASS),
            "7 passed")

    def test_non_test_command_yields_nothing(self):
        self.assertEqual(self.verdict("git status", "On branch master"), "")

    def test_unrecognised_output_says_so_rather_than_claiming_a_pass(self):
        """Silence would read as "no tests", which is a different claim."""
        v = self.verdict("pytest", "something unexpected happened")
        self.assertIn("unparsed", v)
        self.assertNotIn("passed", v)

    def test_unparsed_output_mentioning_failure_is_flagged(self):
        v = self.verdict("pytest", "Traceback ... AssertionError: boom")
        self.assertIn("failure", v)


class ClassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.classify = self.sb.observe.classify

    def test_prompt_wording_drives_the_kind(self):
        cases = {
            "fix the broken fan reading": "bugfix",
            "the sync is failing again": "bugfix",
            "refactor sync.py into two modules": "refactor",
            "add a memory tab to the dashboard": "feature",
            "why is the GPU temperature wrong": "discovery",
            "should we use sqlite or json here": "decision",
        }
        for prompt, want in cases.items():
            self.assertEqual(self.classify(prompt, ["a.py"], [], ""), want, prompt)

    def test_read_only_turn_is_discovery(self):
        self.assertEqual(self.classify("", [], ["a.py"], ""), "discovery")

    def test_unremarkable_turn_is_change(self):
        self.assertEqual(self.classify("update the thing", ["a.py"], [], ""),
                         "change")


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_record_writes_a_searchable_row(self):
        path = self.sb.write("t.jsonl", transcript(
            assistant(
                tool_use("Edit", "t1", file_path="C:/repo/msi_ec.py"),
                text("The fan divisor was wrong; corrected it."),
            )))
        oid = self.sb.observe.record(
            "s1", path, "C:/repo", "repo", prompt="fix the fan divisor",
            tok={"in": 100, "out": 50, "cc": 0, "cr": 0}, model="claude-opus-5")
        self.assertTrue(oid)

        rows = self.sb.store.search("divisor")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bugfix")
        self.assertEqual(rows[0]["tokens"], 150)
        self.assertEqual(rows[0]["source"], "extract")
        self.assertIn("msi_ec.py", rows[0]["files_modified"])

    def test_empty_turn_is_not_recorded(self):
        path = self.sb.write("t.jsonl", transcript(
            assistant(tool_use("TodoWrite", "t1", todos=[]))))
        self.assertEqual(self.sb.observe.record("s1", path, "C:/repo", "repo"), 0)

    def test_record_never_raises(self):
        """It runs from a hook, so every failure path must stay quiet."""
        self.assertEqual(
            self.sb.observe.record("s1", "C:/does-not-exist.jsonl", "C:/r", "r"), 0)
        self.assertEqual(self.sb.observe.record("", "", "", ""), 0)

    def test_prompt_is_recovered_from_the_event_log(self):
        """By the time a turn ends, the payload no longer carries its prompt."""
        self.sb.events.log("prompt", "s7", "C:/repo", "repo",
                           msg="fix the broken thing")
        path = self.sb.write("t.jsonl", transcript(
            assistant(tool_use("Edit", "t1", file_path="C:/repo/a.py"),
                      text("done"))))
        oid = self.sb.observe.record("s7", path, "C:/repo", "repo")
        self.assertTrue(oid)
        conn = self.sb.store.connect()
        self.addCleanup(conn.close)
        row = conn.execute("select prompt, kind from observations where id=?",
                           (oid,)).fetchone()
        self.assertEqual(row["prompt"], "fix the broken thing")
        self.assertEqual(row["kind"], "bugfix")


if __name__ == "__main__":
    unittest.main()
