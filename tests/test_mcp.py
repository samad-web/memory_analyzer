"""The MCP server: the protocol, the tools, and the ways it must not die.

Test names map to the acceptance criteria in
``build-docs/features/mcp-server.md``. Two groups matter most:

* **Protocol survival.** A server that dies takes the agent's memory with it
  for the rest of the session, so a malformed line, an unknown method and a
  raising handler each have to leave it answering the next request.
* **Size.** A tool result is a context cost exactly like an injected payload.
  The dashboards learned this at 15 KB a row; the ceiling is asserted here so
  the regression cannot arrive unnoticed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from ._env import PROJECT, Sandbox


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.mcp = self.sb.mcp_server
        self.store = self.sb.store
        self.conn = self.store.connect()
        self.addCleanup(self.conn.close)
        # The name this repository resolves to, which is the one project the
        # server is allowed to sync GOAL.md for.
        self.local = self.sb.agent_alert.project_name(os.getcwd())

    # -- fixtures ---------------------------------------------------------

    def add_observation(self, **fields) -> int:
        now = fields.pop("ended_at", time.time())
        base = {
            "sid": "s", "proj": "demo", "cwd": "C:/repo", "kind": "change",
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

    def add_milestone(self, title: str, **fields) -> None:
        now = time.time()
        self.conn.execute(
            "insert into milestones (proj, ordinal, title, acceptance, "
            " check_cmd, status, created_at, updated_at) values (?,?,?,?,?,?,?,?)",
            ("demo", fields.get("ordinal", 0), title, fields.get("acceptance", ""),
             fields.get("check_cmd", ""), fields.get("status", "pending"),
             now, now))
        self.conn.commit()

    def add_decision(self, title: str, rationale: str = "") -> None:
        self.conn.execute(
            "insert into decisions (proj, title, rationale, decided_at) "
            "values (?,?,?,?)", ("demo", title, rationale, time.time()))
        self.conn.commit()

    def call(self, name: str, **arguments) -> str:
        result = self.mcp.call_tool(name, arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result["content"][0]["text"]

    # -- 1. handshake and tool list ---------------------------------------

    def test_initialize_declares_a_protocol_and_the_tools_capability(self):
        reply = self.mcp.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": self.mcp.PROTOCOL}})
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], self.mcp.PROTOCOL)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], self.mcp.NAME)

    def test_initialize_answers_a_known_older_protocol_in_its_own_version(self):
        """A version string is not worth a failed handshake."""
        reply = self.mcp.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")

    def test_initialize_falls_back_for_an_unknown_protocol(self):
        reply = self.mcp.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(reply["result"]["protocolVersion"], self.mcp.PROTOCOL)

    def test_tools_list_names_every_tool(self):
        tools = self.mcp.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(
            {t["name"] for t in tools},
            {"search_memory", "project_status", "milestone_state",
             "recent_decisions", "observation_detail"})

    def test_every_tool_has_a_usable_schema_and_a_description(self):
        for tool in self.mcp.handle(
                {"jsonrpc": "2.0", "id": 2,
                 "method": "tools/list"})["result"]["tools"]:
            with self.subTest(tool["name"]):
                self.assertTrue(tool["description"].strip())
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema["properties"], dict)
                for name, prop in schema["properties"].items():
                    self.assertIn(prop["type"], ("string", "integer"), name)
                for required in schema.get("required", []):
                    self.assertIn(required, schema["properties"])

    def test_the_tool_list_carries_no_python_callables(self):
        """The handler lives in the same dict; it must not reach the wire."""
        json.dumps(self.mcp.tool_list())
        self.assertNotIn("handler", self.mcp.tool_list()[0])

    # -- 2. search --------------------------------------------------------

    def test_search_memory_finds_the_matching_turn_and_not_the_others(self):
        self.add_observation(title="fixed the fan divisor", proj="demo")
        self.add_observation(title="tidied the installer", proj="demo")
        out = self.call("search_memory", query="divisor", project="demo")
        self.assertIn("divisor", out)
        self.assertNotIn("installer", out)

    def test_search_memory_searches_the_narrative_too(self):
        self.add_observation(title="a change",
                             narrative="the embedded controller encodes a divisor")
        self.assertIn("embedded",
                      self.call("search_memory", query="embedded", project="demo"))

    def test_search_memory_with_no_match_says_how_much_it_looked_through(self):
        self.add_observation(title="something else")
        out = self.call("search_memory", query="nonexistentterm", project="demo")
        self.assertIn("No match", out)
        self.assertIn("1 remembered turn", out)

    def test_search_memory_on_an_empty_store_explains_rather_than_erroring(self):
        out = self.call("search_memory", query="anything")
        self.assertIn("Nothing remembered yet", out)

    def test_search_memory_without_a_query_points_at_project_status(self):
        self.assertIn("project_status", self.call("search_memory", query="  "))

    def test_search_memory_respects_the_project_filter(self):
        self.add_observation(title="alpha work", proj="alpha")
        self.add_observation(title="beta work", proj="beta")
        out = self.call("search_memory", query="work", project="alpha")
        self.assertIn("alpha work", out)
        self.assertNotIn("beta work", out)

    def test_a_hostile_query_does_not_raise(self):
        """FTS syntax in a user string is an exception waiting to happen."""
        self.add_observation(title="ordinary")
        for query in ['a"b', "star*", "(x", "NEAR(", "OR", "^", "a AND"]:
            with self.subTest(query):
                result = self.mcp.call_tool("search_memory", {"query": query})
                self.assertIsInstance(result["content"][0]["text"], str)

    def test_the_limit_is_clamped(self):
        self.assertEqual(self.mcp._limit(10 ** 9), self.mcp.MAX_LIMIT)
        self.assertEqual(self.mcp._limit(-5), 1)
        self.assertEqual(self.mcp._limit("nonsense"), self.mcp.DEFAULT_LIMIT)
        self.assertEqual(self.mcp._limit(None), self.mcp.DEFAULT_LIMIT)
        self.assertEqual(self.mcp._limit(3), 3)

    def test_a_huge_limit_cannot_widen_the_result(self):
        for i in range(80):
            self.add_observation(title="widget number %d" % i)
        out = self.call("search_memory", query="widget", project="demo",
                        limit=10 ** 6)
        self.assertLessEqual(out.count("#"), self.mcp.MAX_LIMIT)

    # -- 3. size ----------------------------------------------------------

    def test_a_search_result_stays_small_for_a_heavy_turn(self):
        """15 KB a row was a megabyte and a half at a hundred rows."""
        self.add_observation(
            title="heavy " * 20, narrative="n" * 1200,
            commands=["c" * 300 for _ in range(60)],
            files_modified=["C:/some/long/path/file%d.py" % i for i in range(60)],
            files_read=["C:/other/long/path/file%d.py" % i for i in range(60)])
        out = self.call("search_memory", query="heavy", project="demo")
        self.assertLess(len(out), 4096, "a search result grew to %d chars" % len(out))

    def test_a_search_result_says_what_it_hid(self):
        self.add_observation(title="busy turn",
                             commands=["cmd %d" % i for i in range(40)],
                             files_modified=["C:/f%d.py" % i for i in range(40)])
        out = self.call("search_memory", query="busy", project="demo")
        self.assertIn("40 command(s)", out)
        self.assertIn("more", out)
        self.assertIn("observation_detail", out)

    def test_every_result_obeys_the_ceiling(self):
        for i in range(self.mcp.MAX_LIMIT + 10):
            self.add_observation(title="widget %d" % i, narrative="n" * 400)
        out = self.call("search_memory", query="widget", project="demo",
                        limit=self.mcp.MAX_LIMIT)
        self.assertLessEqual(len(out), self.mcp.MAX_RESULT)

    # -- 4. detail --------------------------------------------------------

    def test_observation_detail_is_untrimmed(self):
        oid = self.add_observation(
            title="the whole turn", prompt="make the fan quieter",
            commands=["command-%d" % i for i in range(40)])
        out = self.call("observation_detail", id=oid)
        self.assertIn("command-39", out)
        self.assertIn("make the fan quieter", out)

    def test_observation_detail_for_a_missing_id_is_a_sentence(self):
        self.assertIn("No such", self.call("observation_detail", id=999999))

    def test_observation_detail_for_a_nonsense_id_is_a_sentence(self):
        self.assertIn("No such", self.call("observation_detail", id="not-a-number"))

    # -- 5. status and board ----------------------------------------------

    def test_project_status_on_an_empty_store_is_prose_not_an_error(self):
        out = self.call("project_status", project="demo")
        self.assertIn("demo", out)
        self.assertIn("No turns remembered", out)

    def test_project_status_reports_the_board_and_the_live_milestone(self):
        self.add_milestone("already finished", status="done", ordinal=1)
        self.add_milestone("being worked on", status="active", ordinal=2,
                           acceptance="it passes its check")
        self.add_milestone("not started", status="pending", ordinal=3)
        out = self.call("project_status", project="demo")
        self.assertIn("1 of 3 milestone(s) done", out)
        self.assertIn("active: being worked on", out)
        self.assertIn("next: not started", out)
        self.assertIn("it passes its check", out)

    def test_project_status_lists_what_is_already_settled(self):
        self.add_decision("SQLite, not a server", "one file, no daemon")
        self.assertIn("SQLite, not a server",
                      self.call("project_status", project="demo"))

    def test_project_status_counts_remembered_turns(self):
        self.add_observation(title="one")
        self.add_observation(title="two")
        self.assertIn("2 turn(s) remembered",
                      self.call("project_status", project="demo"))

    def test_milestone_state_shows_what_completion_rests_on(self):
        self.add_milestone("indexed store", status="done",
                           check_cmd="python -m unittest tests.test_store")
        out = self.call("milestone_state", project="demo")
        self.assertIn("[x] indexed store", out)
        self.assertIn("check: python -m unittest tests.test_store", out)

    def test_milestone_state_says_so_when_a_milestone_cannot_be_asserted(self):
        """No check command is "unverifiable", never "probably done"."""
        self.add_milestone("vague milestone", status="pending")
        self.assertIn("cannot be asserted",
                      self.call("milestone_state", project="demo"))

    def test_milestone_state_with_no_board_points_at_goal_md(self):
        self.assertIn("GOAL.md", self.call("milestone_state", project="demo"))

    def test_recent_decisions_carries_the_reason(self):
        self.add_decision("asserted, not inferred", "an agent can talk itself "
                                                    "into having finished")
        out = self.call("recent_decisions", project="demo")
        self.assertIn("asserted, not inferred", out)
        self.assertIn("because: an agent can talk itself", out)

    def test_recent_decisions_when_there_are_none(self):
        self.assertIn("No decisions recorded",
                      self.call("recent_decisions", project="demo"))

    def test_the_project_defaults_to_the_repository_the_server_runs_in(self):
        self.assertEqual(self.mcp._project(""),
                         self.sb.agent_alert.project_name(os.getcwd()))
        self.assertEqual(self.mcp._project("explicit"), "explicit")

    def test_a_malformed_goal_file_does_not_fail_the_tool(self):
        """``_sync`` is best-effort: a bad GOAL.md degrades to "no board"."""
        def boom(*a, **k):
            raise ValueError("unparseable")
        self.mcp.goals.sync = boom
        self.addCleanup(setattr, self.mcp.goals, "sync", self.sb.goals.sync)
        self.add_observation(title="still here", proj=self.local)
        self.assertIn("1 turn(s) remembered",
                      self.call("project_status", project=self.local))

    def test_a_named_other_project_is_never_synced_from_our_goal_file(self):
        """``goals.sync`` resolves GOAL.md against the working directory.

        Syncing on behalf of a project we are not standing in would read this
        repository's board and write it under that project's name -- one read
        tool corrupting another project's milestones.
        """
        calls = []
        self.mcp.goals.sync = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, self.mcp.goals, "sync", self.sb.goals.sync)

        self.call("project_status", project="some-other-project")
        self.assertEqual(calls, [])
        self.call("milestone_state", project="some-other-project")
        self.assertEqual(calls, [])

        self.call("project_status", project=self.local)
        self.assertEqual([a[0] for a in calls], [self.local])

    # -- 6. the server must not die ---------------------------------------

    def test_an_unknown_method_is_an_error_not_a_crash(self):
        reply = self.mcp.handle(
            {"jsonrpc": "2.0", "id": 9, "method": "no/such/method"})
        self.assertEqual(reply["error"]["code"], self.mcp.METHOD_NOT_FOUND)
        self.assertEqual(reply["id"], 9)

    def test_a_raising_handler_comes_back_as_tool_content(self):
        def boom(**kwargs):
            raise RuntimeError("the database fell over")
        original = self.mcp.BY_NAME["project_status"]["handler"]
        self.mcp.BY_NAME["project_status"]["handler"] = boom
        self.addCleanup(
            self.mcp.BY_NAME["project_status"].__setitem__, "handler", original)
        result = self.mcp.call_tool("project_status", {})
        self.assertTrue(result["isError"])
        self.assertIn("the database fell over", result["content"][0]["text"])

    def test_an_unknown_tool_lists_the_real_ones(self):
        result = self.mcp.call_tool("delete_everything", {})
        self.assertTrue(result["isError"])
        self.assertIn("search_memory", result["content"][0]["text"])

    def test_an_invented_argument_is_dropped_not_passed_on(self):
        """Otherwise a client that guesses a parameter gets a TypeError."""
        result = self.mcp.call_tool(
            "project_status", {"project": "demo", "invented": "x"})
        self.assertFalse(result["isError"])

    def test_non_dict_arguments_are_tolerated(self):
        for arguments in (None, [], "string", 7):
            with self.subTest(arguments=arguments):
                self.assertFalse(
                    self.mcp.call_tool("recent_decisions", arguments)["isError"])

    # -- 7. notifications -------------------------------------------------

    def test_a_notification_gets_no_reply(self):
        """Answering a notification is how a handshake ends in a stall."""
        self.assertIsNone(
            self.mcp.handle({"jsonrpc": "2.0",
                             "method": "notifications/initialized"}))

    def test_a_notification_for_an_unknown_method_still_gets_no_reply(self):
        self.assertIsNone(
            self.mcp.handle({"jsonrpc": "2.0", "method": "no/such/method"}))

    def test_ping_is_answered(self):
        reply = self.mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(reply["result"], {})

    def test_unadvertised_capabilities_answer_empty_rather_than_erroring(self):
        for method, key in (("resources/list", "resources"),
                            ("prompts/list", "prompts")):
            reply = self.mcp.handle(
                {"jsonrpc": "2.0", "id": 5, "method": method})
            self.assertEqual(reply["result"][key], [])

    # -- the read loop ----------------------------------------------------

    def test_a_malformed_line_is_answered_and_the_loop_continues(self):
        import io

        out = io.StringIO()
        self.mcp.serve(io.StringIO(
            "{not json\n"
            '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], self.mcp.PARSE_ERROR)
        self.assertEqual(replies[1]["id"], 1)

    def test_a_json_array_is_rejected_without_killing_the_loop(self):
        import io

        out = io.StringIO()
        self.mcp.serve(io.StringIO(
            '[1,2,3]\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], self.mcp.PARSE_ERROR)
        self.assertEqual(len(replies), 2)

    def test_blank_lines_are_skipped(self):
        import io

        out = io.StringIO()
        self.mcp.serve(io.StringIO(
            '\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n'), out)
        self.assertEqual(len(out.getvalue().splitlines()), 1)

    def test_eof_exits_cleanly(self):
        import io

        self.assertEqual(self.mcp.serve(io.StringIO(""), io.StringIO()), 0)

    def test_one_response_per_request_and_none_per_notification(self):
        import io

        out = io.StringIO()
        self.mcp.serve(io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'), out)
        self.assertEqual(len(out.getvalue().splitlines()), 2)


class RegistrationTest(unittest.TestCase):
    """``install`` edits the file that holds the whole Claude Code config."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.mcp = self.sb.mcp_server
        self.path = self.sb.path("claude.json")
        self.mcp.config_path = lambda scope="user": self.path

    def test_install_registers_the_server(self):
        done = self.mcp.install()
        self.assertTrue(done["changed"])
        with open(self.path, encoding="utf-8") as fh:
            config = json.load(fh)
        entry = config["mcpServers"][self.mcp.NAME]
        self.assertEqual(entry["type"], "stdio")
        self.assertTrue(entry["args"][0].endswith("mcp_server.py"))

    def test_the_registered_path_uses_forward_slashes(self):
        """A backslash path in JSON needs escaping twice to survive the file."""
        self.assertNotIn("\\", self.mcp.entry()["args"][0])

    def test_install_is_idempotent(self):
        self.mcp.install()
        self.assertFalse(self.mcp.install()["changed"])
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["mcpServers"]), 1)

    def test_install_keeps_everything_else_in_the_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"theme": "dark", "projects": {"a": {"x": 1}},
                       "mcpServers": {"someone-else": {"command": "node"}}}, fh)
        self.mcp.install()
        with open(self.path, encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["projects"], {"a": {"x": 1}})
        self.assertIn("someone-else", config["mcpServers"])
        self.assertIn(self.mcp.NAME, config["mcpServers"])

    def test_install_backs_the_file_up_first(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"theme": "dark"}, fh)
        self.mcp.install()
        backups = [n for n in os.listdir(self.sb.root)
                   if n.startswith("claude.json.bak-")]
        self.assertEqual(len(backups), 1)

    def test_a_corrupt_config_is_not_silently_discarded_into_a_crash(self):
        """A half-written file should still end up with a working registration."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.mcp.install()
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn(self.mcp.NAME, json.load(fh)["mcpServers"])

    def test_a_non_dict_mcp_servers_value_is_replaced(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": "nonsense"}, fh)
        self.mcp.install()
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn(self.mcp.NAME, json.load(fh)["mcpServers"])

    def test_uninstall_removes_only_our_entry(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"someone-else": {"command": "node"}}}, fh)
        self.mcp.install()
        self.assertTrue(self.mcp.uninstall()["removed"])
        with open(self.path, encoding="utf-8") as fh:
            servers = json.load(fh)["mcpServers"]
        self.assertEqual(list(servers), ["someone-else"])

    def test_uninstall_when_not_installed(self):
        self.assertFalse(self.mcp.uninstall()["removed"])

    def test_status_reports_registration(self):
        self.assertFalse(self.mcp.status()["registered"])
        self.mcp.install()
        self.assertTrue(self.mcp.status()["registered"])


class EndToEndTest(unittest.TestCase):
    """A real subprocess, a real handshake, a real answer on stdout.

    Everything above exercises functions in-process. This is the only test that
    proves the thing Claude Code actually does works: launch the script, write
    JSON-RPC to its stdin, read JSON-RPC off its stdout. It runs against the
    real store, so it asserts shape rather than content.
    """

    def test_a_real_handshake_over_a_pipe(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search_memory",
                        "arguments": {"query": "store", "limit": 2}}},
        ]
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(PROJECT, "mcp_server.py"), "serve"],
            input=stdin, capture_output=True, text=True, timeout=60,
            cwd=PROJECT)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        replies = [json.loads(line) for line in proc.stdout.splitlines() if line]
        self.assertEqual(len(replies), 3, "a notification was answered")
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertIn("protocolVersion", replies[0]["result"])
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)
        self.assertFalse(replies[2]["result"]["isError"],
                         replies[2]["result"]["content"][0]["text"])
        self.assertIsInstance(replies[2]["result"]["content"][0]["text"], str)

    def test_nothing_but_protocol_reaches_stdout(self):
        """One stray print and the client sees a dead server."""
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(PROJECT, "mcp_server.py"), "serve"],
            input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
            capture_output=True, text=True, timeout=60, cwd=PROJECT)
        for line in proc.stdout.splitlines():
            if line.strip():
                json.loads(line)


class BundleTest(unittest.TestCase):
    """The server has to be in the box, or an installed copy is configured
    to launch a file that is not there."""

    def test_the_server_ships_in_both_file_lists(self):
        import deploy

        self.assertIn("mcp_server.py", deploy.REPORTER_FILES)
        with open(os.path.join(PROJECT, "install", "install.ps1"),
                  encoding="utf-8-sig") as fh:
            self.assertIn("mcp_server.py", fh.read())


if __name__ == "__main__":
    unittest.main()
