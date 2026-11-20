"""Antigravity (agy) sessions: hook install, payload mapping, tokens, context.

Test names map to features/antigravity-agents.md acceptance criteria (AC1..AC6).
Payload and transcript shapes are copied from a live agy 1.5.13 run on Windows.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

from ._env import Sandbox
from .test_notify import NotifyCase

import hooks

CID = "33341426-b740-4e0c-9445-45aebe558e5b"


class AgyHooksInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.path = self.sb.path(".gemini", "config", "hooks.json")
        os.makedirs(os.path.dirname(self.path))
        p = mock.patch.object(hooks, "agy_hooks_path", return_value=self.path)
        p.start()
        self.addCleanup(p.stop)

    def _data(self) -> dict:
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_ac1_install_writes_three_events_under_one_named_spec(self):
        hooks.install_agy(backup=False)
        spec = self._data()[hooks.AGY_SPEC]
        self.assertEqual(sorted(spec), ["PreInvocation", "SessionStart", "Stop"])
        self.assertIn("notify.py\" context", spec["SessionStart"][0]["command"])
        self.assertNotIn("PreToolUse", spec)  # it would deny tool calls on stray stdout
        self.assertTrue(hooks.status_agy()["complete"])

    def test_ac1_reinstall_does_not_duplicate_and_keeps_foreign_specs(self):
        foreign = {"Stop": [{"type": "command", "command": "other --ping"}]}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"firstmate": foreign}, fh)
        hooks.install_agy(backup=False)
        report = hooks.install_agy(backup=False)
        data = self._data()
        self.assertEqual(data["firstmate"], foreign)
        self.assertEqual(len(data[hooks.AGY_SPEC]["Stop"]), 1)
        self.assertIn("replaced", report)

    def test_ac1_exe_build_points_hooks_at_the_bridge(self):
        hooks.install_agy(backup=False, exe=r"C:\S\SirahlyzerBridge.exe")
        cmd = self._data()[hooks.AGY_SPEC]["Stop"][0]["command"]
        self.assertEqual(cmd, r'"C:\S\SirahlyzerBridge.exe" notify done')

    def test_non_spec_top_level_key_is_kept_and_warned_about(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1}, fh)
        report = hooks.install_agy(backup=False)
        self.assertEqual(self._data()["version"], 1)
        self.assertTrue(report["warnings"])

    def test_missing_gemini_dir_is_skipped_not_created(self):
        missing = self.sb.path("nohome", ".gemini", "config", "hooks.json")
        with mock.patch.object(hooks, "agy_hooks_path", return_value=missing):
            report = hooks.install_agy(backup=False)
        self.assertIn("skipped", report)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))

    def test_uninstall_removes_only_ours(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"firstmate": {"Stop": []}}, fh)
        hooks.install_agy(backup=False)
        self.assertEqual(hooks.uninstall_agy()["removed"], 1)
        self.assertEqual(self._data(), {"firstmate": {"Stop": []}})


class AgyNotifyTest(NotifyCase):
    def agy(self, action: str, **extra) -> None:
        payload = {"conversationId": CID, "cwd": self.repo, "hookEventName": "x",
                   "lastUserInput": "fix the tile", "workspacePaths": [self.repo], **extra}
        self.notify.read_payload = lambda: payload
        self.notify.main(["notify.py", action])

    def test_ac2_first_invocation_logs_the_prompt_under_the_conversation(self):
        self.agy("prompt", invocationNum=0)
        (ev,) = self.logged()
        self.assertEqual((ev["ev"], ev["sid"], ev["msg"], ev["proj"]),
                         ("prompt", CID[:40], "fix the tile", "repo"))

    def test_ac4_later_invocations_log_nothing(self):
        self.agy("prompt", invocationNum=1)
        self.agy("prompt", invocationNum=7)
        self.assertEqual(self.logged(), [])

    def test_ac2_stop_logs_done_with_agy_tokens_and_model(self):
        transcript = self.sb.write("t/transcript_full.jsonl", "\n".join(json.dumps(x) for x in (
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT"},
            {"step_index": 1, "source": "MODEL", "model": "gemini-3.8-flash-high",
             "input_tokens": 22050, "cache_read_tokens": 0, "output_tokens": 409,
             "tool_calls": [{"name": "read_file", "args": {}}]},
            {"step_index": 3, "source": "MODEL", "model": "gemini-3.8-flash-high",
             "input_tokens": 2548, "cache_read_tokens": 19997, "output_tokens": 58},
        )) + "\n")
        self.agy("done", transcriptPath=transcript, fullyIdle=True)
        (ev,) = self.logged()
        self.assertEqual(ev["ev"], "done")
        self.assertEqual(ev["tok"]["in"], 24598)
        self.assertEqual(ev["tok"]["out"], 467)
        self.assertEqual(ev["tok"]["cr"], 19997)
        self.assertEqual((ev["model"], ev["tools"]), ("gemini-3.8-flash-high", 1))
        # Read once: a second Stop on the same transcript adds nothing.
        tok, _, _ = self.events.tokens_since(CID, transcript)
        self.assertEqual(sum(tok.values()), 0)

    def test_ac3_session_start_prints_inject_steps(self):
        out = io.StringIO()
        with mock.patch.object(self.sb.context, "render", return_value="GOAL ship it"), \
                mock.patch.object(sys, "stdout", out):
            self.agy("context")
        self.assertEqual(json.loads(out.getvalue()),
                         {"injectSteps": [{"systemMessage": {"systemMessage": "GOAL ship it"}}]})

    def test_ac5_claude_payload_and_context_shape_unchanged(self):
        claude = {"session_id": "s", "cwd": "c", "prompt": "p"}
        self.assertIs(self.notify.normalise(claude), claude)
        out = io.StringIO()
        with mock.patch.object(self.sb.context, "render", return_value="X"), \
                mock.patch.object(sys, "stdout", out):
            self.sb.context.emit("p")
        self.assertIn("hookSpecificOutput", json.loads(out.getvalue()))

    def test_prompt_and_done_print_nothing(self):
        """Anything on stdout would be parsed by agy as a hook decision."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.agy("prompt", invocationNum=0)
            self.agy("done")
        self.assertEqual(out.getvalue(), "")

    def test_gemini_is_priced_at_zero(self):
        self.assertEqual(self.sb.stats.estimate_cost({"in": 10**6, "out": 10**6}, "gemini-3.8-flash-high"), 0.0)


class AgyMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.mcp = self.sb.mcp_server
        self.path = self.sb.path("mcp_config.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"other": {"command": "node"}}}, fh)
        p = mock.patch.object(self.mcp, "config_path", return_value=self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_ac6_agy_registration_merges_and_has_no_type_key(self):
        self.mcp.install(scope="agy")
        with open(self.path, encoding="utf-8") as fh:
            servers = json.load(fh)["mcpServers"]
        self.assertIn("other", servers)
        self.assertNotIn("type", servers[self.mcp.NAME])
        self.assertTrue(self.mcp.status(scope="agy")["current"])


if __name__ == "__main__":
    unittest.main()
