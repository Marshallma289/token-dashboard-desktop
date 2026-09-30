import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB


class FollowupRequirementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codex-dashboard-followup-"))
        self.sessions = self.temp_dir / "sessions"
        self.archive = self.temp_dir / "archived_sessions"
        self.sessions.mkdir()
        self.archive.mkdir()
        self.db = DashboardDB(
            self.temp_dir / "cache.sqlite3",
            roots=[self.sessions, self.archive],
            timezone_name="Asia/Shanghai",
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _record(timestamp, response_id, thread_id="thread-a"):
        return {
            "timestamp": timestamp,
            "type": "token_usage_record",
            "payload": {
                "thread_id": thread_id,
                "response_id": response_id,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        }

    def test_same_response_id_is_scoped_to_thread(self):
        path = self.sessions / "same-id.jsonl"
        lines = [
            {"timestamp": "2026-09-19T00:00:00Z", "type": "session_meta", "payload": {"cwd": "/a", "model_provider": "openai"}},
            {"timestamp": "2026-09-19T00:00:01Z", "type": "turn_context", "payload": {"cwd": "/a", "model": "m"}},
            self._record("2026-09-19T00:00:02Z", "same-response", "thread-a"),
            self._record("2026-09-19T00:00:03Z", "same-response", "thread-b"),
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        self.db.scan()
        self.assertEqual(self.db.counts()["usage_records"], 2)

    def test_move_to_archive_rebuilds_without_losing_record(self):
        source = self.sessions / "moving.jsonl"
        source.write_text(json.dumps(self._record("2026-09-19T08:00:00+00:00", "moving-response")), encoding="utf-8")
        self.db.scan()
        self.assertEqual(self.db.counts()["usage_records"], 1)
        destination = self.archive / source.name
        source.replace(destination)
        self.db.scan()
        self.assertEqual(self.db.counts()["usage_records"], 1)

    def test_timezone_converts_utc_boundary_to_local_date(self):
        path = self.sessions / "timezone.jsonl"
        path.write_text(json.dumps(self._record("2026-09-18T16:30:00Z", "timezone-response")), encoding="utf-8")
        self.db.scan()
        data = self.db.dashboard(days=30)
        self.assertEqual(data["daily_active"][0]["date"], "2026-09-19")
        self.assertEqual(data["daily_active"][0]["hour"], 0)


if __name__ == "__main__":
    unittest.main()
