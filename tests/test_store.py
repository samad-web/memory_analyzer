"""The store's two promises: an overlapping ingest is free, and FTS stays true.

Both are load-bearing. The first is what lets the watermark be sloppy without
double-counting a turn; the second is what makes the memory searchable at all.
"""

from __future__ import annotations

import json
import os
import time
import unittest

from ._env import Sandbox


def line(**fields) -> str:
    return json.dumps(fields, separators=(",", ":")) + "\n"


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.store = self.sb.store
        self.log = self.sb.events.EVENTS_PATH
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        with open(self.log, "w", encoding="utf-8") as fh:
            for i in range(50):
                fh.write(line(t=1000.0 + i, ev="prompt", sid="s%d" % (i % 3),
                              proj="proj", cwd="C:/repo", msg="task %d" % i))
        self.conn = self.store.connect()
        self.addCleanup(self.conn.close)

    def rows(self) -> int:
        return self.conn.execute("select count(*) n from events").fetchone()["n"]

    def test_ingest_reads_every_line(self):
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), 50)

    def test_full_rewind_adds_nothing(self):
        self.store.ingest(self.conn)
        before = self.rows()
        self.store._meta_set(self.conn, "ingest_offset", 0)
        self.conn.commit()
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), before)

    def test_partial_rewind_adds_nothing(self):
        r = self.store.ingest(self.conn)
        before = self.rows()
        self.store._meta_set(self.conn, "ingest_offset", max(0, r["to"] // 2))
        self.conn.commit()
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), before)

    def test_appended_lines_are_picked_up(self):
        self.store.ingest(self.conn)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(line(t=2000.0, ev="done", sid="s9", proj="proj"))
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), 51)

    def test_torn_final_line_waits_for_the_rest(self):
        """A half-written line must not be consumed, or its turn is lost."""
        self.store.ingest(self.conn)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write('{"t":3000.0,"ev":"do')      # no newline: mid-append
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), 50)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write('ne","sid":"s9","proj":"proj"}\n')
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), 51)

    def test_truncated_log_starts_a_new_generation(self):
        """Offsets restart at zero after a rotation, so the key must not be the
        offset alone -- otherwise the new line overwrites an unrelated old row
        while the rest of the old generation stays beside it."""
        first = self.store.ingest(self.conn)
        self.assertEqual(first["gen"], 0)
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write(line(t=1.0, ev="prompt", sid="x", proj="p", msg="after"))
        second = self.store.ingest(self.conn)
        self.assertEqual(second["gen"], 1)
        # Nothing was overwritten: 50 from generation 0, one from generation 1.
        self.assertEqual(self.rows(), 51)
        self.assertEqual(
            self.conn.execute(
                "select count(*) n from events where gen=0").fetchone()["n"], 50)
        self.assertEqual(
            self.conn.execute(
                "select msg from events where gen=1").fetchone()["msg"], "after")

    def test_generation_survives_further_appends(self):
        self.store.ingest(self.conn)
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write(line(t=1.0, ev="prompt", sid="x", proj="p"))
        self.store.ingest(self.conn)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(line(t=2.0, ev="done", sid="x", proj="p"))
        r = self.store.ingest(self.conn)
        self.assertEqual(r["gen"], 1)
        self.assertEqual(self.rows(), 52)

    def test_unparseable_line_is_skipped_not_fatal(self):
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
            fh.write(line(t=4000.0, ev="end", sid="s9", proj="proj"))
        self.store.ingest(self.conn)
        self.assertEqual(self.rows(), 51)

    def test_missing_log_is_not_an_error(self):
        os.remove(self.log)
        r = self.store.ingest(self.conn)
        self.assertEqual(r["inserted"], 0)


class SearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.store = self.sb.store
        self.conn = self.store.connect()
        self.addCleanup(self.conn.close)

    def add(self, **fields) -> int:
        now = time.time()
        base = {"proj": "p", "ended_at": now, "created_at": now,
                "title": "", "narrative": "", "facts": "", "prompt": ""}
        base.update(fields)
        cur = self.conn.execute(
            "insert into observations (proj, ended_at, created_at, title, "
            " narrative, facts, prompt) values (?,?,?,?,?,?,?)",
            (base["proj"], base["ended_at"], base["created_at"], base["title"],
             base["narrative"], base["facts"], base["prompt"]))
        self.conn.commit()
        return int(cur.lastrowid)

    def test_insert_is_searchable(self):
        self.add(title="Zygomorphic", narrative="unrelated")
        self.assertEqual(len(self.store.search("Zygomorphic", conn=self.conn)), 1)

    def test_update_moves_the_index(self):
        oid = self.add(title="Zygomorphic")
        self.conn.execute("update observations set title=? where id=?",
                          ("Replaced", oid))
        self.conn.commit()
        self.assertEqual(len(self.store.search("Zygomorphic", conn=self.conn)), 0)
        self.assertEqual(len(self.store.search("Replaced", conn=self.conn)), 1)

    def test_delete_clears_the_index(self):
        oid = self.add(title="Zygomorphic")
        self.conn.execute("delete from observations where id=?", (oid,))
        self.conn.commit()
        self.assertEqual(len(self.store.search("Zygomorphic", conn=self.conn)), 0)

    def test_project_filter(self):
        self.add(title="shared term", proj="alpha")
        self.add(title="shared term", proj="beta")
        self.assertEqual(len(self.store.search("shared", conn=self.conn)), 2)
        self.assertEqual(
            len(self.store.search("shared", proj="alpha", conn=self.conn)), 1)

    def test_hostile_input_does_not_raise(self):
        """A user's words are not FTS syntax, and must not be treated as it."""
        self.add(title="ordinary row")
        for query in ['has"quote', "star*", "OR AND NOT", "(unbalanced",
                      "NEAR(a b", "", "   ", "^caret", "col:on"]:
            self.assertIsInstance(
                self.store.search(query, conn=self.conn), list, query)


class BriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.conn = self.sb.store.connect()
        self.addCleanup(self.conn.close)

    def test_empty_project_is_safe(self):
        b = self.sb.store.brief("nothing-here", conn=self.conn)
        self.assertIsNone(b["goal"])
        self.assertEqual(b["total"], 0)
        self.assertEqual(b["recent"], [])


if __name__ == "__main__":
    unittest.main()
