"""The Memory view: one query, two renderers, and a bounded payload.

Both dashboards read ``store.observations_page``, so it is tested once here
rather than twice through two UIs. The size invariants matter as much as the
content ones: a page of rows goes down a phone tunnel, and the untrimmed form
was 15 KB a row.
"""

from __future__ import annotations

import json
import time
import unittest

from ._env import Sandbox


class PageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.store = self.sb.store
        self.conn = self.store.connect()
        self.addCleanup(self.conn.close)

    def add(self, **fields) -> int:
        now = fields.pop("ended_at", time.time())
        base = {
            "sid": "s", "proj": "p", "cwd": "C:/repo", "kind": "change",
            "title": "", "narrative": "", "facts": "", "prompt": "",
            "files_read": [], "files_modified": [], "commands": [],
            "tools": [], "tests": "",
        }
        base.update(fields)
        cur = self.conn.execute(
            "insert into observations (sid, proj, cwd, ended_at, kind, title, "
            " narrative, facts, prompt, files_read, files_modified, commands, "
            " tools, tests, created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (base["sid"], base["proj"], base["cwd"], now, base["kind"],
             base["title"], base["narrative"], base["facts"], base["prompt"],
             json.dumps(base["files_read"]), json.dumps(base["files_modified"]),
             json.dumps(base["commands"]), json.dumps(base["tools"]),
             base["tests"], now))
        self.conn.commit()
        return int(cur.lastrowid)

    # -- content ---------------------------------------------------------

    def test_no_query_shows_the_newest_first(self):
        """An empty box is "show me the latest", not an empty result."""
        old = self.add(title="older", ended_at=time.time() - 9000)
        new = self.add(title="newer")
        page = self.store.observations_page(conn=self.conn)
        self.assertEqual([r["id"] for r in page["rows"]], [new, old])

    def test_query_searches(self):
        self.add(title="fixed the fan divisor")
        self.add(title="tidied the installer")
        page = self.store.observations_page(query="divisor", conn=self.conn)
        self.assertEqual(page["matched"], 1)
        self.assertEqual(page["total"], 2)

    def test_narrative_and_commands_are_searchable(self):
        self.add(title="t", narrative="the embedded controller encodes a divisor")
        self.assertEqual(
            self.store.observations_page(query="embedded", conn=self.conn)["matched"], 1)

    def test_project_filter(self):
        self.add(title="a", proj="alpha")
        self.add(title="b", proj="beta")
        page = self.store.observations_page(proj="alpha", conn=self.conn)
        self.assertEqual(page["matched"], 1)
        self.assertEqual(page["total"], 2)

    def test_kind_filter(self):
        self.add(title="a", kind="bugfix")
        self.add(title="b", kind="feature")
        self.assertEqual(
            self.store.observations_page(kind="bugfix", conn=self.conn)["matched"], 1)

    def test_facets_are_offered(self):
        self.add(title="a", proj="alpha", kind="bugfix")
        self.add(title="b", proj="beta", kind="feature")
        page = self.store.observations_page(conn=self.conn)
        self.assertEqual(set(page["projects"]), {"alpha", "beta"})
        self.assertEqual(set(page["kinds"]), {"bugfix", "feature"})

    def test_empty_store_is_not_an_error(self):
        page = self.store.observations_page(conn=self.conn)
        self.assertEqual(page["rows"], [])
        self.assertEqual(page["total"], 0)

    def test_hostile_query_does_not_raise(self):
        self.add(title="ordinary")
        for query in ['a"b', "star*", "(x", "NEAR(", "OR", ""]:
            self.assertIsInstance(
                self.store.observations_page(query=query, conn=self.conn)["rows"],
                list, query)

    # -- shape and size --------------------------------------------------

    def test_json_columns_arrive_as_lists(self):
        self.add(title="t", files_modified=["C:/a.py"], tools=["Edit"])
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        self.assertEqual(row["files_modified"], ["C:/a.py"])
        self.assertEqual(row["tools"], ["Edit"])

    def test_corrupt_json_column_degrades_to_empty(self):
        oid = self.add(title="t")
        self.conn.execute(
            "update observations set files_modified='not json' where id=?", (oid,))
        self.conn.commit()
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        self.assertEqual(row["files_modified"], [])

    def test_page_rows_omit_commands_but_keep_the_count(self):
        """The table draws no commands and they are the heaviest field."""
        self.add(title="t", commands=["cmd %d" % i for i in range(40)])
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        self.assertEqual(row["commands"], [])
        self.assertEqual(row["commands_n"], 40)

    def test_page_rows_trim_file_lists_but_keep_the_count(self):
        self.add(title="t", files_modified=["C:/f%d.py" % i for i in range(40)])
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        self.assertEqual(len(row["files_modified"]), self.store.PAGE_FILES)
        self.assertEqual(row["files_modified_n"], 40)

    def test_page_rows_shorten_the_narrative(self):
        self.add(title="t", narrative="x" * 1200)
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        self.assertLessEqual(len(row["narrative"]), self.store.PAGE_NARRATIVE)

    def test_a_page_row_stays_small(self):
        """15 KB a row became a megabyte and a half at a hundred rows."""
        self.add(title="t" * 150, narrative="n" * 1200,
                 commands=["c" * 300 for _ in range(60)],
                 files_modified=["C:/some/long/path/file%d.py" % i for i in range(60)],
                 files_read=["C:/another/long/path/file%d.py" % i for i in range(60)],
                 tools=["Tool%d" % i for i in range(20)])
        row = self.store.observations_page(conn=self.conn)["rows"][0]
        size = len(json.dumps(row))
        self.assertLess(size, 4096, "a page row grew to %d bytes" % size)

    # -- detail ----------------------------------------------------------

    def test_detail_is_untrimmed(self):
        oid = self.add(title="t", commands=["cmd %d" % i for i in range(40)],
                       narrative="x" * 1200)
        row = self.store.observation(oid, conn=self.conn)
        self.assertEqual(len(row["commands"]), 40)
        self.assertEqual(len(row["narrative"]), 1200)

    def test_detail_counts_agree_with_the_page(self):
        oid = self.add(title="t", commands=["c"] * 25,
                       files_modified=["C:/f%d" % i for i in range(30)])
        page_row = self.store.observations_page(conn=self.conn)["rows"][0]
        full = self.store.observation(oid, conn=self.conn)
        self.assertEqual(page_row["commands_n"], full["commands_n"])
        self.assertEqual(page_row["files_modified_n"], full["files_modified_n"])

    def test_detail_for_an_unknown_id_is_empty_not_an_error(self):
        self.assertEqual(self.store.observation(999999, conn=self.conn), {})

    def test_detail_for_a_nonsense_id_is_empty_not_an_error(self):
        self.assertEqual(self.store.observation("nope", conn=self.conn), {})


class WebPayloadTest(unittest.TestCase):
    """The HTTP layer must not be able to 500 a view."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        import importlib

        import webserver

        self.webserver = importlib.reload(webserver)
        self.server = self.webserver.DashboardServer(
            {"web_port": 0, "web_bind": "127.0.0.1", "web_token": ""})

    def test_memory_payload_has_the_expected_shape(self):
        payload = self.server.memory_payload()
        for key in ("rows", "matched", "total", "projects", "kinds",
                    "query", "project", "kind"):
            self.assertIn(key, payload)

    def test_memory_payload_on_an_empty_store(self):
        payload = self.server.memory_payload()
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["total"], 0)

    def test_memory_detail_payload_rejects_a_bad_id_gracefully(self):
        payload = self.server.memory_detail_payload("not-a-number")
        self.assertEqual(payload["row"], {})
        self.assertIn("error", payload)

    def test_memory_detail_payload_for_a_missing_row(self):
        self.assertEqual(self.server.memory_detail_payload("424242")["row"], {})


if __name__ == "__main__":
    unittest.main()
