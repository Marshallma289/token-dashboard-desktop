"""Context-window snapshots are retained only when tied to a request."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timezone
from pathlib import Path

from backend import DashboardDB, ProviderCatalog, RolloutParser


def event(timestamp, kind, **payload):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def usage(input_tokens, output_tokens):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def count(timestamp, request_usage, model_context_window="missing", thread_id="thread-a"):
    info = {"last_token_usage": request_usage}
    if model_context_window != "missing":
        info["model_context_window"] = model_context_window
    return event(timestamp, "event_msg", type="token_count", thread_id=thread_id, info=info)


def write_jsonl(path, events):
    path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")


class ContextWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sessions"
        self.root.mkdir()
        self.path = self.root / "context.jsonl"
        self.parser = RolloutParser(ProviderCatalog(), timezone.utc)
        self.start = "2026-10-16T12:00:00Z"

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, events):
        write_jsonl(self.path, events)
        records, errors = self.parser.parse(self.path)
        self.assertEqual(errors, 0)
        return records

    def test_known_window_and_request_input_are_read_from_token_count(self):
        request_usage = usage(41, 9)
        record, = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            count("2026-10-16T12:00:01Z", request_usage, 262144),
        ])
        self.assertEqual(record.context_window_tokens, 262144)
        self.assertEqual(record.context_input_tokens, 41)
        self.assertEqual(record.context_observed_at, "2026-10-16T12:00:01+00:00")

    def test_unknown_window_remains_null_while_input_snapshot_is_preserved(self):
        record, = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            count("2026-10-16T12:00:01Z", usage(41, 9)),
        ])
        self.assertIsNone(record.context_window_tokens)
        self.assertEqual(record.context_input_tokens, 41)
        self.assertIsNotNone(record.context_observed_at)

    def test_zero_is_a_valid_window_and_input_value(self):
        record, = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            count("2026-10-16T12:00:01Z", usage(0, 0), 0),
        ])
        self.assertEqual(record.context_window_tokens, 0)
        self.assertEqual(record.context_input_tokens, 0)

    def test_distinct_modern_requests_receive_their_matching_context_snapshots(self):
        first = usage(100, 200)
        second = usage(300, 400)
        records = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            event("2026-10-16T12:00:01Z", "turn_context", model="gpt-6.1-sol"),
            event("2026-10-16T12:00:02Z", "response_item", type="reasoning"),
            event("2026-10-16T12:00:03Z", "token_usage_record", thread_id="thread-a",
                  response_id="response-1", usage=first),
            count("2026-10-16T12:00:03.001Z", first, 128000),
            event("2026-10-16T12:00:04Z", "response_item", type="reasoning"),
            event("2026-10-16T12:00:05Z", "token_usage_record", thread_id="thread-a",
                  response_id="response-2", usage=second),
            count("2026-10-16T12:00:05.001Z", second, 256000),
        ])
        self.assertEqual([record.response_id for record in records], ["response-1", "response-2"])
        self.assertEqual([record.context_window_tokens for record in records], [128000, 256000])
        self.assertEqual([record.context_input_tokens for record in records], [100, 300])

    def test_ambiguous_matching_does_not_attach_context_to_either_request(self):
        request_usage = usage(100, 200)
        records = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            event("2026-10-16T12:00:01Z", "turn_context", model="gpt-6.1-sol"),
            event("2026-10-16T12:00:02Z", "response_item", type="reasoning"),
            event("2026-10-16T12:00:03Z", "token_usage_record", thread_id="thread-a",
                  response_id="response-1", usage=request_usage),
            event("2026-10-16T12:00:04Z", "response_item", type="reasoning"),
            event("2026-10-16T12:00:05Z", "token_usage_record", thread_id="thread-a",
                  response_id="response-2", usage=request_usage),
            count("2026-10-16T12:00:05.001Z", request_usage, 128000),
        ])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.context_window_tokens is None for record in records))
        self.assertTrue(all(record.context_input_tokens is None for record in records))

    def test_cumulative_only_count_does_not_invent_a_request_context(self):
        cumulative = usage(2000, 1000)
        record, = self.parse([
            event(self.start, "session_meta", id="thread-a", model_provider="openai"),
            event("2026-10-16T12:00:01Z", "event_msg", type="token_count",
                  info={"total_token_usage": cumulative, "model_context_window": 128000}),
        ])
        self.assertIsNone(record.context_window_tokens)
        self.assertIsNone(record.context_input_tokens)
        self.assertIsNone(record.context_observed_at)

    def test_request_detail_persists_context_fields_and_parser_upgrade_rescans(self):
        request_usage = usage(41, 9)
        write_jsonl(self.path, [
            event(self.start, "session_meta", id="thread-a", model_provider="openai", cwd="/workspace"),
            count("2026-10-16T12:00:01Z", request_usage, 262144),
        ])
        db_path = Path(self.temp.name) / "usage.sqlite3"
        database = DashboardDB(db_path, roots=[self.root], timezone_name="UTC")
        try:
            database.scan()
            summary = database.requests(days=0)["data"][0]
            detail = database.request_detail(summary["request_id"])
            self.assertEqual(detail["context_window_tokens"], 262144)
            self.assertEqual(detail["context_input_tokens"], 41)
            self.assertEqual(detail["context_observed_at"], "2026-10-16T12:00:01+00:00")
        finally:
            database.close()

        # Emulate a prior schema, then verify additive migration and the parser
        # version trigger repopulate metadata from the still available source.
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP VIEW usage_records")
            for column in ("context_window_tokens", "context_input_tokens", "context_observed_at"):
                connection.execute("ALTER TABLE history_snapshots DROP COLUMN " + column)
            connection.execute("UPDATE history_snapshots SET fingerprint=randomblob(32)")
            connection.execute("UPDATE source_files SET parser_version=7")
        database = DashboardDB(db_path, roots=[self.root], timezone_name="UTC")
        try:
            self.assertEqual(database.scan().files_scanned, 1)
            detail = database.request_detail(summary["request_id"])
            self.assertEqual(detail["context_window_tokens"], 262144)
            self.assertEqual(detail["context_input_tokens"], 41)
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
