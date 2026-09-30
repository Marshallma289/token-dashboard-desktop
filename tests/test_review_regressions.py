import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import DashboardDB, default_roots


def write_lines(path, rows):
    path.write_text("\n".join(json.dumps(row) if isinstance(row, dict) else row for row in rows) + "\n", encoding="utf-8")


def session(thread="thread-a", provider="openai", cwd="/workspace/a"):
    return {"timestamp": "2026-09-18T16:00:00Z", "type": "session_meta", "payload": {"id": thread, "model_provider": provider, "cwd": cwd}}


def turn(model, turn_id="turn-a"):
    return {"timestamp": "2026-09-18T16:01:00Z", "type": "turn_context", "payload": {"turn_id": turn_id, "model": model}}


def usage(thread, response, total, timestamp="2026-09-18T16:30:00Z"):
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": {"thread_id": thread, "response_id": response, "usage": {"input_tokens": total - 10, "output_tokens": 10, "total_tokens": total}}}


class ReviewRegressionTests(unittest.TestCase):
    def test_timezone_change_invalidates_unchanged_file_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            write_lines(root / "one.jsonl", [session(), turn("gpt-test"), usage("thread-a", "r1", 100)])
            db_path = Path(tmp) / "cache.sqlite3"
            first = DashboardDB(db_path, roots=[root], timezone_name="Asia/Shanghai")
            try:
                first.scan()
                self.assertEqual(first.dashboard(30)["daily_activity"][0]["date"], "2026-09-19")
            finally:
                first.close()
            second = DashboardDB(db_path, roots=[root], timezone_name="UTC")
            try:
                result = second.scan()
                self.assertEqual(result.files_scanned, 1)
                self.assertEqual(second.dashboard(30)["daily_activity"][0]["date"], "2026-09-18")
            finally:
                second.close()

    def test_duplicate_legacy_snapshot_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            legacy = {"timestamp": "2026-09-18T16:30:00Z", "type": "event_msg", "payload": {"type": "token_count", "turn_id": "turn-a", "info": {"last_token_usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}}
            duplicate = json.loads(json.dumps(legacy))
            duplicate["timestamp"] = "2026-09-18T16:30:01Z"
            write_lines(root / "legacy.jsonl", [session(), turn("legacy-model"), legacy, duplicate])
            db = DashboardDB(Path(tmp) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                self.assertEqual(db.counts()["usage_records"], 1)
                self.assertEqual(db.dashboard(30)["summary"]["total_tokens"], 100)
            finally:
                db.close()

    def test_session_thread_state_prevents_cross_thread_model_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            settings = {"timestamp": "2026-09-18T16:02:00Z", "type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_id": "thread-b", "thread_settings": {"model": "model-b", "model_provider_id": "provider-b", "cwd": "/workspace/b"}}}
            rows = [
                session("thread-a"), turn("model-a", "turn-a"), usage("thread-a", "a1", 100),
                settings, usage("thread-b", "b1", 200), turn("model-a2", "turn-a2"), usage("thread-a", "a2", 300),
            ]
            write_lines(root / "threads.jsonl", rows)
            db = DashboardDB(Path(tmp) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                rows_b = db.dashboard(30, workspace="/workspace/b")["daily_model_usage"]
                self.assertEqual([(row["provider"], row["model"], row["total_tokens"]) for row in rows_b], [("provider-b", "model-b", 200)])
                rows_a = db.dashboard(30, workspace="/workspace/a")["daily_model_usage"]
                seen_a = {(row["provider"], row["model"], row["total_tokens"]) for row in rows_a}
                self.assertIn(("openai", "model-a2", 300), seen_a)
            finally:
                db.close()

    def test_later_usage_correction_replaces_earlier_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            write_lines(root / "correction.jsonl", [session(), turn("gpt-test"), usage("thread-a", "same", 100), usage("thread-a", "same", 120, "2026-09-18T16:31:00Z")])
            db = DashboardDB(Path(tmp) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                self.assertEqual(db.counts()["usage_records"], 1)
                self.assertEqual(db.dashboard(30)["summary"]["total_tokens"], 120)
            finally:
                db.close()

    def test_codex_home_and_malformed_line_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "custom-codex"
            sessions = codex_home / "sessions"
            sessions.mkdir(parents=True)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                self.assertEqual(default_roots()[0], sessions)
            write_lines(sessions / "bad.jsonl", [session(), "{broken-json}", turn("gpt-test"), usage("thread-a", "r1", 100)])
            db = DashboardDB(Path(tmp) / "cache.sqlite3", roots=[sessions], timezone_name="UTC")
            try:
                result = db.scan()
                self.assertEqual(result.parse_errors, 1)
                self.assertEqual(db.counts()["usage_records"], 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
