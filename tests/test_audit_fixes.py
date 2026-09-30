import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB, DashboardService


def write_jsonl(path, events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def session(thread="thread-a"):
    return {
        "timestamp": "2026-09-18T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": thread, "model_provider": "moonshot", "cwd": "/workspace/a"},
    }


def turn(identifier="turn-a"):
    return {
        "timestamp": "2026-09-18T00:01:00Z",
        "type": "turn_context",
        "payload": {"turn_id": identifier, "model": "model-a"},
    }


def modern(response="response-a", total=100, timestamp="2026-09-18T00:02:00Z"):
    return {
        "timestamp": timestamp,
        "type": "token_usage_record",
        "payload": {
            "thread_id": "thread-a",
            "response_id": response,
            "usage": {"input_tokens": total - 10, "output_tokens": 10, "total_tokens": total},
        },
    }


def legacy(last=None, cumulative=None, timestamp="2026-09-18T00:02:00Z"):
    info = {}
    if last is not None:
        info["last_token_usage"] = {"input_tokens": last - 10, "output_tokens": 10, "total_tokens": last}
    if cumulative is not None:
        info["total_token_usage"] = {
            "input_tokens": cumulative - 10,
            "output_tokens": 10,
            "total_tokens": cumulative,
        }
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": info}}


class AuditFixTests(unittest.TestCase):
    def make_db(self, root, **kwargs):
        return DashboardDB(Path(root).parent / "cache.sqlite3", roots=[root], timezone_name="UTC", **kwargs)

    def test_legacy_session_id_prevents_cross_session_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            write_jsonl(root / "a.jsonl", [session("A"), turn(), legacy(last=100)])
            write_jsonl(root / "b.jsonl", [session("B"), turn(), legacy(last=100)])
            db = self.make_db(root)
            try:
                db.scan()
                self.assertEqual(db.dashboard(days=0)["summary"]["total_tokens"], 200)
                self.assertEqual(db.counts()["usage_records"], 2)
            finally:
                db.close()

    def test_legacy_total_snapshots_are_differenced_and_bound_same_turn_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            # The second request has the same last usage but a larger session
            # total. The third event repeats its snapshot and must dedupe.
            write_jsonl(
                root / "legacy.jsonl",
                [session(), turn(), legacy(last=100, cumulative=100), legacy(last=100, cumulative=200), legacy(last=100, cumulative=200)],
            )
            db = self.make_db(root)
            try:
                db.scan()
                data = db.dashboard(days=0)["summary"]
                self.assertEqual(data["total_tokens"], 200)
                self.assertEqual(data["request_count"], 2)
            finally:
                db.close()

    def test_legacy_without_total_keeps_different_usage_in_one_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            write_jsonl(root / "legacy.jsonl", [session(), turn(), legacy(last=100), legacy(last=150)])
            db = self.make_db(root)
            try:
                db.scan()
                self.assertEqual(db.dashboard(days=0)["summary"]["total_tokens"], 250)
            finally:
                db.close()

    def test_independent_duplicate_sources_survive_an_unrelated_rescan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            rich = [session(), turn(), modern(total=200)]
            partial = [session(), turn(), modern(total=100)]
            write_jsonl(root / "a.jsonl", rich)
            duplicate = root / "b.jsonl"
            write_jsonl(duplicate, partial)
            db = self.make_db(root)
            try:
                db.scan()
                self.assertEqual(db.dashboard(days=0)["summary"]["total_tokens"], 200)
                write_jsonl(duplicate, partial + [{"type": "unrelated"}])
                db.scan()
                self.assertEqual(db.dashboard(days=0)["summary"]["total_tokens"], 200)
            finally:
                db.close()

    def test_day_window_uses_injected_current_local_date(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            write_jsonl(root / "old.jsonl", [session(), turn(), modern(timestamp="2020-01-01T00:00:00Z")])
            clock = lambda: dt.datetime(2026, 9, 20, 8, tzinfo=dt.timezone.utc)
            db = self.make_db(root, clock=clock)
            try:
                db.scan()
                self.assertEqual(db.dashboard(days=1)["summary"]["total_tokens"], 0)
                self.assertEqual(db.dashboard(days=1)["local_date"], "2026-09-20")
            finally:
                db.close()

    def test_provider_labels_and_aliases_are_resolved_at_query_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            providers = Path(folder) / "providers.json"
            providers.write_text(json.dumps({"providers": {"moonshot": "Old label"}}), encoding="utf-8")
            write_jsonl(root / "one.jsonl", [session(), turn(), modern()])
            db = DashboardDB(Path(folder) / "cache.sqlite3", roots=[root], providers_path=providers, timezone_name="UTC")
            try:
                db.scan()
                providers.write_text(json.dumps({"providers": {"moonshot": "New label"}}), encoding="utf-8")
                data = db.dashboard(days=0, provider="New label")
                self.assertEqual(data["summary"]["total_tokens"], 100)
                self.assertEqual(data["providers"], [{"provider": "moonshot", "provider_label": "New label"}])
            finally:
                db.close()

    def test_migration_keeps_old_cache_for_unavailable_root(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "old.sqlite3"
            offline = Path(folder) / "offline" / "old.jsonl"
            connection = sqlite3.connect(str(db_path))
            connection.executescript(
                """
                CREATE TABLE source_files (path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, scanned_at TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT '');
                CREATE TABLE usage_records (record_key TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_line INTEGER NOT NULL, response_id TEXT, thread_id TEXT, timestamp TEXT NOT NULL, day TEXT NOT NULL, hour INTEGER NOT NULL, provider TEXT NOT NULL, provider_label TEXT NOT NULL, model TEXT NOT NULL, workspace TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL, cache_write_input_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL);
                """
            )
            connection.execute(
                "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("old", str(offline), 3, "response-a", "thread-a", "2026-09-18T00:02:00+00:00", "2026-09-18", 0, "moonshot", "Old", "model-a", "/workspace/a", 90, 10, 0, 0, 0, 100),
            )
            connection.commit()
            connection.close()
            db = DashboardDB(db_path, roots=[offline.parent], timezone_name="UTC")
            try:
                self.assertEqual(db.dashboard(days=0)["summary"]["total_tokens"], 100)
                self.assertEqual(db.counts()["usage_records"], 1)
            finally:
                db.close()

    def test_health_reports_a_sanitized_scanner_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            db = self.make_db(root)
            try:
                service = DashboardService(db)
                service._record_failure(RuntimeError("C:/private/rollout.jsonl"))
                health = service.health()
                self.assertEqual(health["status"], "degraded")
                self.assertEqual(health["scan"]["state"], "error")
                self.assertEqual(health["scan"]["last_error"]["category"], "RuntimeError")
                self.assertNotIn("private", json.dumps(health))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
