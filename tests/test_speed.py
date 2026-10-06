import json
import hashlib
from contextlib import closing
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend import DashboardDB


THREAD = "thread-speed"


def session(provider="openai", workspace="/workspace/a"):
    return {
        "timestamp": "2026-10-05T08:00:00Z",
        "type": "session_meta",
        "payload": {"id": THREAD, "model_provider": provider, "cwd": workspace},
    }


def turn(turn_id, model="gpt-test", timestamp="2026-10-05T08:00:01Z"):
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"turn_id": turn_id, "model": model},
    }


def task(kind, turn_id, timestamp, **extra):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": turn_id, **extra},
    }


def usage(turn_id, response_id, output, timestamp, *, model=None, provider=None, workspace=None):
    payload = {
        "thread_id": THREAD,
        "turn_id": turn_id,
        "response_id": response_id,
        "usage": {
            "input_tokens": 2500,
            "output_tokens": output,
            "reasoning_output_tokens": 500,
            "total_tokens": 2500 + output,
        },
    }
    for key, value in (("model", model), ("model_provider_id", provider), ("cwd", workspace)):
        if value is not None:
            payload[key] = value
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": payload}


def subagent_history():
    return [
        {"timestamp": "2026-10-05T08:00:00Z", "type": "session_meta", "payload": {
            "id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            "model_provider": "openai", "cwd": "/workspace/child",
        }},
        {"timestamp": "2026-10-05T08:00:01Z", "type": "session_meta", "payload": {
            "id": "parent", "model_provider": "openai", "cwd": "/workspace/parent",
        }},
        task("task_started", "parentturn", "2026-10-05T08:00:02Z"),
        {"timestamp": "2026-10-05T08:00:03Z", "type": "turn_context", "payload": {
            "turn_id": "parentturn", "model": "parentmodel",
        }},
        {"timestamp": "2026-10-05T08:00:04Z", "type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_id": "child",
            "thread_settings": {"model": "gpt-6-luna"},
        }},
    ]


def subagent_usage(turn_id, response_id, output, timestamp):
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": {
        "thread_id": "child", "session_id": "parent", "root_turn_id": "parentturn",
        "turn_id": turn_id, "response_id": response_id,
        "usage": {"input_tokens": 9000000, "output_tokens": output, "total_tokens": 9000000 + output},
        "turn_token_usage": {"input_tokens": 9000000, "output_tokens": output,
                             "total_tokens": 9000000 + output},
    }}


def write_lines(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class SpeedStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.log = self.sessions / "one.jsonl"
        self.db_path = self.root / "usage.sqlite3"
        fixed_now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        self.db = DashboardDB(
            self.db_path,
            roots=[self.sessions],
            timezone_name="UTC",
            clock=lambda: fixed_now,
        )

    def tearDown(self):
        if self.db is not None:
            self.db.close()
        self.temp.cleanup()

    def dashboard(self, **filters):
        self.db.scan()
        return self.db.dashboard(days=0, **filters)

    def assert_speed(self, metrics, output, duration_ms, samples, rate):
        self.assertEqual(metrics["speed_output_tokens"], output)
        self.assertEqual(metrics["speed_duration_ms"], duration_ms)
        self.assertEqual(metrics["speed_sample_count"], samples)
        if rate is None:
            self.assertIsNone(metrics["output_tokens_per_second"])
        else:
            self.assertAlmostEqual(metrics["output_tokens_per_second"], rate, delta=0.0001)

    def test_speed_uses_output_tokens_and_duration_ms(self):
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            usage("t1", "r1", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "t1", "2026-10-05T08:05:00Z", duration_ms=20000),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 1000, 20000, 1, 50)
        self.assertEqual(len(data["daily_model_usage"]), 1)
        self.assert_speed(data["daily_model_usage"][0], 1000, 20000, 1, 50)

    def test_subagent_speed_uses_child_thread_after_inherited_parent_session(self):
        write_lines(self.log, subagent_history() + [
            task("task_started", "childturn", "2026-10-05T08:00:06Z"),
            {"timestamp": "2026-10-05T08:00:07Z", "type": "turn_context", "payload": {
                "turn_id": "childturn", "model": "gpt-6-luna",
            }},
            subagent_usage("childturn", "child-r1", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "childturn", "2026-10-05T08:00:26Z", duration_ms=20000),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 1000, 20000, 1, 50)
        self.assertEqual([(row["model"], row["speed_sample_count"]) for row in data["daily_model_usage"]], [
            ("gpt-6-luna", 1),
        ])

    def test_subagent_followup_turn_gets_an_independent_speed_sample(self):
        write_lines(self.log, subagent_history() + [
            task("task_started", "childturn1", "2026-10-05T08:00:06Z"),
            {"timestamp": "2026-10-05T08:00:07Z", "type": "turn_context", "payload": {
                "turn_id": "childturn1", "model": "gpt-6-luna",
            }},
            subagent_usage("childturn1", "child-r1", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "childturn1", "2026-10-05T08:00:26Z", duration_ms=20000),
            task("task_started", "childturn2", "2026-10-05T08:00:28Z"),
            {"timestamp": "2026-10-05T08:00:29Z", "type": "turn_context", "payload": {
                "turn_id": "childturn2", "model": "gpt-6-luna",
            }},
            subagent_usage("childturn2", "child-r2", 500, "2026-10-05T08:00:32Z"),
            task("task_complete", "childturn2", "2026-10-05T08:00:38Z", duration_ms=10000),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 1500, 30000, 2, 50)
        self.assertEqual([(row["model"], row["speed_sample_count"]) for row in data["daily_model_usage"]], [
            ("gpt-6-luna", 2),
        ])

    def test_multiple_requests_and_duplicate_response_make_one_turn_sample(self):
        first = usage("t1", "r1", 300, "2026-10-05T08:00:05Z")
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            first, dict(first), usage("t1", "r2", 700, "2026-10-05T08:00:12Z"),
            task("task_complete", "t1", "2026-10-05T08:00:20Z", duration_ms=20000),
        ])
        data = self.dashboard()
        self.assertEqual(data["summary"]["request_count"], 2)
        self.assert_speed(data["summary"], 1000, 20000, 1, 50)

    def test_turn_speeds_aggregate_by_total_output_over_total_duration(self):
        write_lines(self.log, [
            session(),
            turn("slow", timestamp="2026-10-05T08:00:01Z"),
            task("task_started", "slow", "2026-10-05T08:00:00Z"),
            usage("slow", "r1", 1000, "2026-10-05T08:00:30Z"),
            task("task_complete", "slow", "2026-10-05T08:01:40Z", duration_ms=100000),
            turn("fast", timestamp="2026-10-05T08:02:01Z"),
            task("task_started", "fast", "2026-10-05T08:02:00Z"),
            usage("fast", "r2", 9000, "2026-10-05T08:02:05Z"),
            task("task_complete", "fast", "2026-10-05T08:02:10Z", duration_ms=10000),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 10000, 110000, 2, 10000 / 110)

    def test_missing_completion_and_nonpositive_or_unpaired_durations_have_no_speed(self):
        write_lines(self.log, [
            session(),
            turn("open"), task("task_started", "open", "2026-10-05T08:00:00Z"),
            usage("open", "r1", 100, "2026-10-05T08:00:01Z"),
            turn("zero"), task("task_started", "zero", "2026-10-05T08:01:00Z"),
            usage("zero", "r2", 100, "2026-10-05T08:01:01Z"),
            task("task_complete", "zero", "2026-10-05T08:01:02Z", duration_ms=0),
            turn("negative"), task("task_started", "negative", "2026-10-05T08:02:00Z"),
            usage("negative", "r3", 100, "2026-10-05T08:02:01Z"),
            task("task_complete", "negative", "2026-10-05T08:02:02Z", duration_ms=-1),
            turn("no-start"), usage("no-start", "r4", 100, "2026-10-05T08:03:01Z"),
            task("task_complete", "no-start", "2026-10-05T08:03:02Z", duration_ms=1000),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 0, 0, 0, None)
        self.assertEqual(data["summary"]["output_tokens"], 400)

    def test_iso_and_event_timestamp_fallbacks(self):
        write_lines(self.log, [
            session(),
            turn("event-times"), task("task_started", "event-times", "2026-10-05T08:00:00Z"),
            usage("event-times", "r1", 300, "2026-10-05T08:00:01Z"),
            task("task_complete", "event-times", "2026-10-05T08:00:03Z"),
            turn("iso-times"),
            task("task_started", "iso-times", "2026-10-05T08:01:00Z", started_at="2026-10-05T08:01:00Z"),
            usage("iso-times", "r2", 200, "2026-10-05T08:01:02Z"),
            task("task_complete", "iso-times", "2026-10-05T08:01:09Z", completed_at="2026-10-05T08:01:04Z"),
        ])
        data = self.dashboard()
        self.assert_speed(data["summary"], 500, 7000, 2, 500 / 7)

    def test_speed_is_grouped_by_last_usage_date_and_obeys_date_model_provider_filters(self):
        write_lines(self.log, [
            session(), turn("old", "old-model", "2026-10-03T10:00:01Z"),
            task("task_started", "old", "2026-10-03T10:00:00Z"),
            usage("old", "r-old", 100, "2026-10-03T10:00:05Z"),
            task("task_complete", "old", "2026-10-03T10:00:10Z", duration_ms=10000),
            {"timestamp": "2026-10-04T10:01:00Z", "type": "event_msg", "payload": {
                "type": "thread_settings_applied", "thread_id": THREAD,
                "thread_settings": {"model": "fresh-model", "model_provider_id": "moonshot", "cwd": "/workspace/b"},
            }},
            turn("new", "fresh-model", "2026-10-04T10:01:01Z"),
            task("task_started", "new", "2026-10-04T10:01:00Z"),
            usage("new", "r-new-1", 100, "2026-10-04T10:01:10Z"),
            usage("new", "r-new-2", 200, "2026-10-05T10:01:10Z"),
            task("task_complete", "new", "2026-10-05T10:01:20Z", duration_ms=20000),
        ])
        self.db.scan()
        recent = self.db.dashboard(days=1, provider="moonshot", model="fresh-model")
        self.assert_speed(recent["summary"], 300, 20000, 1, 15)
        rows = recent["daily_model_usage"]
        self.assertEqual([(row["date"], row["provider"], row["model"]) for row in rows], [
            ("2026-10-05", "moonshot", "fresh-model"),
        ])
        self.assert_speed(rows[0], 300, 20000, 1, 15)
        excluded = self.db.dashboard(days=1, provider="openai", model="old-model")
        self.assert_speed(excluded["summary"], 0, 0, 0, None)

    def test_copying_log_and_repeated_scans_do_not_increase_speed(self):
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            usage("t1", "r1", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "t1", "2026-10-05T08:00:20Z", duration_ms=20000),
        ])
        self.db.scan()
        shutil.copyfile(self.log, self.sessions / "copy.jsonl")
        for _ in range(3):
            self.db.scan()
        self.assert_speed(self.db.dashboard(days=0)["summary"], 1000, 20000, 1, 50)

    def test_appending_completion_updates_previously_missing_speed(self):
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            usage("t1", "r1", 1000, "2026-10-05T08:00:10Z"),
        ])
        initial = self.dashboard()["summary"]
        self.assert_speed(initial, 0, 0, 0, None)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(task("task_complete", "t1", "2026-10-05T08:00:20Z", duration_ms=20000)) + "\n")
        updated = self.dashboard()["summary"]
        self.assert_speed(updated, 1000, 20000, 1, 50)

    def test_reopen_and_log_deletion_preserve_speed_history(self):
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            usage("t1", "r1", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "t1", "2026-10-05T08:00:20Z", duration_ms=20000),
        ])
        expected = self.dashboard()["summary"]
        self.db.close()
        self.db = None
        self.log.unlink()
        self.db = DashboardDB(
            self.db_path,
            roots=[self.sessions],
            timezone_name="UTC",
            clock=lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        )
        self.db.scan()
        self.assert_speed(self.db.dashboard(days=0)["summary"],
                          expected["speed_output_tokens"], expected["speed_duration_ms"],
                          expected["speed_sample_count"], expected["output_tokens_per_second"])

    def test_turn_with_model_provider_or_workspace_change_keeps_usage_but_excludes_speed(self):
        write_lines(self.log, [
            session(), turn("mixed", "model-a"),
            task("task_started", "mixed", "2026-10-05T08:00:00Z"),
            usage("mixed", "r1", 100, "2026-10-05T08:00:05Z"),
            {"timestamp": "2026-10-05T08:00:06Z", "type": "event_msg", "payload": {
                "type": "thread_settings_applied", "thread_id": THREAD,
                "thread_settings": {"model": "model-b", "model_provider_id": "moonshot", "cwd": "/workspace/b"},
            }},
            usage("mixed", "r2", 200, "2026-10-05T08:00:10Z"),
            task("task_complete", "mixed", "2026-10-05T08:00:20Z", duration_ms=20000),
        ])
        data = self.dashboard()
        self.assertEqual(data["summary"]["output_tokens"], 300)
        self.assertEqual(data["summary"]["request_count"], 2)
        self.assert_speed(data["summary"], 0, 0, 0, None)

    def test_inconsistent_turn_usage_total_keeps_requests_but_excludes_speed(self):
        record = usage("t1", "r1", 100, "2026-10-05T08:00:05Z")
        record["payload"]["turn_token_usage"] = {"output_tokens": 999}
        write_lines(self.log, [
            session(), turn("t1"), task("task_started", "t1", "2026-10-05T08:00:00Z"),
            record, task("task_complete", "t1", "2026-10-05T08:00:20Z", duration_ms=20000),
        ])
        data = self.dashboard()
        self.assertEqual(data["summary"]["output_tokens"], 100)
        self.assertEqual(data["summary"]["request_count"], 1)
        self.assert_speed(data["summary"], 0, 0, 0, None)

    def test_numeric_seconds_and_milliseconds_are_distinct(self):
        for fields in ({"started_at": 1791187200, "completed_at": 1791187202},
                       {"started_at_ms": 1791187200000, "completed_at_ms": 1791187202000}):
            with self.subTest(fields=fields):
                write_lines(self.log, [session(), turn("numeric"),
                    task("task_started", "numeric", "2026-10-05T08:00:00Z"),
                    usage("numeric", "numeric-r", 100, "2026-10-05T08:00:01Z"),
                    task("task_complete", "numeric", "2026-10-05T08:00:02Z", **fields)])
                self.assert_speed(self.dashboard()["summary"], 100, 2000, 1, 50)

    def test_invalid_duration_does_not_fall_back_to_timestamps(self):
        for index, invalid in enumerate((True, "invalid", "NaN", "Infinity")):
            with self.subTest(invalid=invalid):
                tid = "invalid-" + str(index)
                write_lines(self.log, [session(), turn(tid),
                    task("task_started", tid, "2026-10-05T08:00:00Z"),
                    usage(tid, "invalid-r", 100, "2026-10-05T08:00:01Z"),
                    task("task_complete", tid, "2026-10-05T08:00:02Z", duration_ms=invalid)])
                self.assert_speed(self.dashboard()["summary"], 0, 0, 0, None)

    def test_old_history_schema_is_backed_up_and_rescanned_without_losing_usage(self):
        write_lines(self.log, [session(), turn("upgrade"),
            task("task_started", "upgrade", "2026-10-05T08:00:00Z"),
            usage("upgrade", "upgrade-r", 1000, "2026-10-05T08:00:10Z"),
            task("task_complete", "upgrade", "2026-10-05T08:00:20Z", duration_ms=20000)])
        self.db.scan()
        self.db.close()
        self.db = None
        with closing(sqlite3.connect(self.db_path)) as old, old:
            # Reproduce the old snapshot fingerprint, which excluded timing.
            for row in old.execute("""SELECT s.id,r.logical_key,c.provider,c.model,c.workspace,s.timestamp,
                    s.input_tokens,s.output_tokens,s.cached_input_tokens,s.cache_write_input_tokens,
                    s.reasoning_output_tokens,s.total_tokens FROM history_requests r
                    JOIN history_snapshots s ON s.id=r.snapshot_id JOIN history_contexts c ON c.id=s.context_id""").fetchall():
                fingerprint = hashlib.sha256(row[1] + json.dumps(list(row[2:]), ensure_ascii=False).encode("utf-8")).digest()
                old.execute("UPDATE history_snapshots SET fingerprint=? WHERE id=?", (fingerprint, row[0]))
            old.execute("DROP VIEW usage_records")
            for field in ("speed_group_key", "speed_output_tokens", "speed_duration_ms"):
                old.execute("ALTER TABLE history_snapshots DROP COLUMN " + field)
            old.execute("UPDATE source_files SET parser_version=3")
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC")
        self.db.scan()
        self.assert_speed(self.db.dashboard(0)["summary"], 1000, 20000, 1, 50)
        self.assertEqual(self.db.dashboard(0)["summary"]["request_count"], 1)
        backup = Path(str(self.db_path) + ".pre-speed.bak")
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as original:
            self.assertEqual(original.execute("SELECT SUM(output_tokens) FROM history_snapshots").fetchone()[0], 1000)
            self.assertNotIn("speed_group_key", [x[1] for x in original.execute("PRAGMA table_info(history_snapshots)")])


if __name__ == "__main__":
    unittest.main()
