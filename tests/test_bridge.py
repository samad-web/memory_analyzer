"""The bridge's two load-bearing promises.

1. **Aggregation groups turns by the right day.** The IST calendar day boundary
   is what SirahOS stores against, so a turn ending 23:30 IST and one ending
   00:30 IST the next calendar day land in different buckets even though they
   are 60 minutes apart. A turn ending 18:30 UTC on 2026-11-03 is 00:00 IST
   on 2026-11-04 -- the day boundary case.

2. **A push is idempotent on (date, folder).** A retry that reaches SirahOS
   twice (first return packet lost, say) must leave one WorkLog, not two,
   and must leave ``sirahos_pushes`` with one row -- the freshest id.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from unittest import mock

from ._env import Sandbox


def _ts(y: int, m: int, d: int, hh: int = 0, mm: int = 0, tz: str = "ist") -> float:
    """Timestamp of a wall-clock moment in either UTC or IST."""
    naive = dt.datetime(y, m, d, hh, mm)
    if tz == "ist":
        utc = naive - dt.timedelta(hours=5, minutes=30)
    else:
        utc = naive
    return (utc - dt.datetime(1970, 1, 1)).total_seconds()


def _turn(**fields) -> dict:
    """A fake ``stats.task_rows`` row: minimum fields the bridge reads."""
    base = {
        "task_key": "s1-100",
        "sid": "s1",
        "project": "voicetranscriber",
        "cwd": "C:/repo",
        "prompt": "did the thing",
        "started_at": None,
        "ended_at": 0.0,
        "duration_s": 60.0,
        "tokens": 100,
        "model": "claude-opus-5",
    }
    base.update(fields)
    return base


class AggregationTest(unittest.TestCase):
    """``group_by_day_and_project`` is the shape SirahOS WorkLogs want."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.bridge = self.sb.mod["bridge"]

    def test_one_bucket_per_day_and_folder(self):
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), duration_s=3600,
                  project="voicetranscriber"),
            _turn(ended_at=_ts(2026, 11, 3, 14, 0), duration_s=1800,
                  project="voicetranscriber"),
            _turn(ended_at=_ts(2026, 11, 3, 15, 0), duration_s=600,
                  project="WealthOS-Mobile"),
            _turn(ended_at=_ts(2026, 11, 4, 9, 0), duration_s=1200,
                  project="voicetranscriber"),
        ]
        groups = self.bridge.group_by_day_and_project(rows)
        self.assertEqual(set(groups.keys()), {
            ("2026-11-03", "voicetranscriber"),
            ("2026-11-03", "WealthOS-Mobile"),
            ("2026-11-04", "voicetranscriber"),
        })
        self.assertEqual(groups[("2026-11-03", "voicetranscriber")]["task_count"], 2)
        self.assertAlmostEqual(
            groups[("2026-11-03", "voicetranscriber")]["duration_s"], 5400.0
        )

    def test_ist_day_boundary(self):
        """00:30 IST on 2026-11-04 (= 19:00 UTC on 2026-11-03) files under Nov 4."""
        rows = [
            # 23:30 IST on 2026-11-03 == 18:00 UTC on 2026-11-03
            _turn(ended_at=_ts(2026, 11, 3, 23, 30), duration_s=600,
                  project="auratel"),
            # 00:30 IST on 2026-11-04 == 19:00 UTC on 2026-11-03
            _turn(ended_at=_ts(2026, 11, 4, 0, 30), duration_s=600,
                  project="auratel"),
        ]
        groups = self.bridge.group_by_day_and_project(rows)
        self.assertEqual(set(groups.keys()), {
            ("2026-11-03", "auratel"),
            ("2026-11-04", "auratel"),
        })

    def test_folder_name_is_the_grouping_key(self):
        """``voicetranscriber`` on disk is distinct from any SirahOS name
        the user might eventually map it to -- we group by what's on disk."""
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), project="voicetranscriber"),
            _turn(ended_at=_ts(2026, 11, 3, 11, 0), project="VoiceTranscriber"),
        ]
        groups = self.bridge.group_by_day_and_project(rows)
        # Case matters: the folder name is read as-is.
        self.assertIn(("2026-11-03", "voicetranscriber"), groups)
        self.assertIn(("2026-11-03", "VoiceTranscriber"), groups)

    def test_turns_with_no_duration_still_count(self):
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), duration_s=None,
                  project="voicetranscriber"),
            _turn(ended_at=_ts(2026, 11, 3, 11, 0), duration_s=1800,
                  project="voicetranscriber"),
        ]
        groups = self.bridge.group_by_day_and_project(rows)
        bucket = groups[("2026-11-03", "voicetranscriber")]
        self.assertEqual(bucket["task_count"], 2)
        self.assertAlmostEqual(bucket["duration_s"], 1800.0)


class NotesBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.bridge = self.sb.mod["bridge"]

    def test_header_names_turn_count_and_duration(self):
        bucket = {
            "task_count": 3, "duration_s": 2 * 3600 + 15 * 60,
            "prompts": ["a", "b", "c"],
        }
        notes = self.bridge.build_notes(bucket)
        self.assertTrue(notes.startswith("3 Claude Code turns, 2h 15m total."))
        self.assertIn("- a", notes)
        self.assertIn("- c", notes)

    def test_long_prompt_list_truncates_with_count(self):
        bucket = {
            "task_count": 100, "duration_s": 3600,
            "prompts": ["x" * 150 for _ in range(100)],
        }
        notes = self.bridge.build_notes(bucket)
        # SirahOS caps notes at 5000 chars -- stay strictly under
        self.assertLessEqual(len(notes), self.bridge.NOTES_LIMIT)
        self.assertIn("and ", notes)
        self.assertIn("more", notes)

    def test_share_prompts_off_hides_text_but_keeps_count(self):
        bucket = {
            "task_count": 4, "duration_s": 7200,
            "prompts": ["sensitive!"],
        }
        notes = self.bridge.build_notes(bucket, share_prompts=False)
        self.assertIn("4 Claude Code turns", notes)
        self.assertIn("(prompt hidden)", notes)
        self.assertNotIn("sensitive!", notes)


class PushWatermarkTest(unittest.TestCase):
    """A completed push must not post the same day/folder twice."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.store = self.sb.mod["store"]
        self.bridge = self.sb.mod["bridge"]

    def test_record_push_is_unique_per_day_and_folder(self):
        """Second record for same key UPDATES rather than raising."""
        pid1 = self.store.sirahos_record_push(
            "2026-11-03", "voicetranscriber", "cwl1", 2.5, 3, "abc",
        )
        pid2 = self.store.sirahos_record_push(
            "2026-11-03", "voicetranscriber", "cwl2", 2.5, 3, "abc",
        )
        self.assertEqual(pid1, pid2)
        row = self.store.sirahos_push_for("2026-11-03", "voicetranscriber")
        self.assertEqual(row["worklog_id"], "cwl2")

    def test_skip_row_blocks_push(self):
        """A user-set ``skip`` leaves the day alone on the next tick."""
        self.store.sirahos_set_skip("2026-11-03", "voicetranscriber", True)
        row = self.store.sirahos_push_for("2026-11-03", "voicetranscriber")
        self.assertEqual(row["skip"], 1)
        # Setting skip False clears it so the bridge can start pushing again.
        self.store.sirahos_set_skip("2026-11-03", "voicetranscriber", False)
        row = self.store.sirahos_push_for("2026-11-03", "voicetranscriber")
        self.assertEqual(row["skip"], 0)

    def test_error_row_leaves_no_worklog_id(self):
        """A failed push records the error but no id, so the next tick retries."""
        self.store.sirahos_record_error(
            "2026-11-03", "voicetranscriber", "SirahOS said 500",
        )
        row = self.store.sirahos_push_for("2026-11-03", "voicetranscriber")
        self.assertEqual(row["worklog_id"], "")
        self.assertEqual(row["last_error"], "SirahOS said 500")


class PushEndToEndTest(unittest.TestCase):
    """A full ``push()`` with a mocked SirahOS. Checks the integration seams:
    config read, token read, aggregation, HTTP call, watermark write."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.bridge = self.sb.mod["bridge"]
        self.sirahos = self.sb.mod["sirahos"]
        self.stats = self.sb.mod["stats"]

        # A config next to bridge.py pointing at a fake SirahOS, with
        # voicetranscriber mapped to "AuraTel" (project id "clabc").
        self.cfg_path = os.path.join(
            os.path.dirname(self.bridge.__file__), "config.json"
        )
        self._orig_cfg = None
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path) as fh:
                self._orig_cfg = fh.read()
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "sirahos": {
                    "api_url": "https://sirahos.test",
                    "last_signed_in_as": "me@test",
                    "folder_project_map": {"voicetranscriber": "clabc"},
                    "share_prompts": True,
                }
            }, fh)
        self.addCleanup(self._restore_cfg)

        # DPAPI on Windows refuses an unencrypted file, and writing a real
        # DPAPI blob from the test would couple to a Windows API we are not
        # testing. Patch the loader instead -- it is the one seam the push
        # has to the token store, so patching it is honest.
        os.makedirs(os.path.dirname(self.bridge.JWT_PATH), exist_ok=True)
        self._token_patch = mock.patch.object(
            self.bridge, "_load_token", return_value="test-jwt",
        )
        self._token_patch.start()
        self.addCleanup(self._token_patch.stop)

    def _restore_cfg(self):
        if self._orig_cfg is None:
            try:
                os.unlink(self.cfg_path)
            except OSError:
                pass
        else:
            with open(self.cfg_path, "w", encoding="utf-8") as fh:
                fh.write(self._orig_cfg)

    def test_push_creates_one_worklog_per_day_and_folder(self):
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), duration_s=3600,
                  project="voicetranscriber", prompt="first thing"),
            _turn(ended_at=_ts(2026, 11, 3, 14, 0), duration_s=1800,
                  project="voicetranscriber", prompt="second thing"),
            _turn(ended_at=_ts(2026, 11, 3, 15, 0), duration_s=600,
                  project="unmapped-folder", prompt="general work"),
        ]

        created: list[dict] = []
        def fake_create(base, token, **kw):
            created.append(dict(kw, _base=base, _token=token))
            return {"id": "wl%d" % len(created)}

        # now() = IST 2026-11-04 10:00, so 2026-11-03 is finalised
        now_ts = _ts(2026, 11, 4, 10, 0)
        with mock.patch.object(self.sb.mod["stats"], "task_rows",
                               return_value=rows), \
             mock.patch.object(self.sb.mod["bridge"].sirahos,
                               "create_worklog", side_effect=fake_create):
            report = self.bridge.push(now=now_ts)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["pushed"], 2)
        # One with mapped project id, one with no id (General).
        payloads = {c["date"] + "/" + c.get("project_id", ""): c for c in created}
        self.assertIn("2026-11-03/clabc", payloads)
        self.assertIn("2026-11-03/", payloads)
        self.assertAlmostEqual(payloads["2026-11-03/clabc"]["hours"], 1.5)

    def test_second_push_skips_what_is_already_posted(self):
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), duration_s=3600,
                  project="voicetranscriber", prompt="first"),
        ]

        created: list[dict] = []
        def fake_create(base, token, **kw):
            created.append(dict(kw))
            return {"id": "wl1"}

        now_ts = _ts(2026, 11, 4, 10, 0)
        with mock.patch.object(self.sb.mod["stats"], "task_rows",
                               return_value=rows), \
             mock.patch.object(self.sb.mod["bridge"].sirahos,
                               "create_worklog", side_effect=fake_create):
            r1 = self.bridge.push(now=now_ts)
            r2 = self.bridge.push(now=now_ts)

        self.assertEqual(r1["pushed"], 1)
        self.assertEqual(r2["pushed"], 0)
        self.assertEqual(r2["skipped_already"], 1)
        self.assertEqual(len(created), 1)

    def test_push_leaves_today_alone(self):
        """Today's bucket is a moving target -- do not push an in-flight day."""
        now_ts = _ts(2026, 11, 4, 10, 0)
        rows = [
            _turn(ended_at=_ts(2026, 11, 4, 9, 0), duration_s=3600,
                  project="voicetranscriber", prompt="today's work"),
        ]
        created: list[dict] = []
        def fake_create(base, token, **kw):
            created.append(dict(kw))
            return {"id": "wl1"}
        with mock.patch.object(self.sb.mod["stats"], "task_rows",
                               return_value=rows), \
             mock.patch.object(self.sb.mod["bridge"].sirahos,
                               "create_worklog", side_effect=fake_create):
            report = self.bridge.push(now=now_ts)
        self.assertEqual(report["pushed"], 0)
        self.assertEqual(len(created), 0)

    def test_auth_failure_deletes_the_token_and_stops(self):
        # Write a real file so we can watch push() delete it on 401.
        with open(self.bridge.JWT_PATH, "w", encoding="utf-8") as fh:
            fh.write("placeholder")
        rows = [
            _turn(ended_at=_ts(2026, 11, 3, 10, 0), duration_s=3600,
                  project="voicetranscriber"),
        ]
        def bad_auth(base, token, **kw):
            raise self.sirahos.AuthError("session expired", status=401)
        now_ts = _ts(2026, 11, 4, 10, 0)
        with mock.patch.object(self.sb.mod["stats"], "task_rows",
                               return_value=rows), \
             mock.patch.object(self.sb.mod["bridge"].sirahos,
                               "create_worklog", side_effect=bad_auth):
            report = self.bridge.push(now=now_ts)
        self.assertFalse(report["ok"])
        self.assertIn("auth", report["errors"][0])
        self.assertFalse(os.path.exists(self.bridge.JWT_PATH))


if __name__ == "__main__":
    unittest.main()
