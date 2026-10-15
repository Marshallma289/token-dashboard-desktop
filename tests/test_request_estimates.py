"""Request estimates use proven log boundaries without changing turn metrics."""

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import DashboardDB, ProviderCatalog, RolloutParser, DashboardService


def event(at, kind, **payload):
    timestamp = (datetime(2026, 10, 15, tzinfo=timezone.utc) + timedelta(seconds=at)).isoformat()
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def tokens(output=600, input_tokens=100, cached=20):
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output, "total_tokens": input_tokens + output}


def count(at, usage=None, total=None):
    info = {}
    if usage is not None:
        info["last_token_usage"] = usage
    if total is not None:
        info["total_token_usage"] = total
    return event(at, "event_msg", type="token_count", info=info)


def usage(at=9.5, response="response-a", output=600):
    return event(at, "token_usage_record", thread_id="thread-a", turn_id="turn-a",
                 response_id=response, usage=tokens(output))


def context(at=2):
    return [event(0, "session_meta", id="thread-a", model_provider="openai", cwd="/workspace"),
            event(0, "event_msg", type="task_started", turn_id="turn-a"),
            event(1, "response_item", type="message", role="user", content="PRIVATE_PROMPT"),
            event(at, "turn_context", turn_id="turn-a", model="gpt-6.1-sol")]


def modern_request():
    return context() + [event(5, "response_item", type="reasoning", summary="PRIVATE_REASONING"),
                        event(9, "response_item", type="function_call", arguments="PRIVATE_TOOL"),
                        usage(), event(40, "response_item", type="function_call_output", output="PRIVATE_RESULT"),
                        count(40.001, tokens(), tokens()),
                        event(45, "event_msg", type="task_complete", turn_id="turn-a",
                              duration_ms=45000, time_to_first_token_ms=1250)]


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class RequestEstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self.path = self.sessions / "one.jsonl"
        self.parser = RolloutParser(ProviderCatalog(), timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, rows):
        write(self.path, rows)
        result, errors = self.parser.parse(self.path)
        self.assertEqual(errors, 0)
        return result

    def test_signature_match_excludes_tool_time_and_preserves_turn_timing_and_ttft(self):
        record, = self.parse(modern_request())
        self.assertEqual(record.request_duration_estimate_ms, 7500)
        self.assertEqual(record.request_output_tokens_per_second_estimate, 80)
        self.assertEqual(record.request_timing_source, "token_usage_record")
        self.assertIsNone(record.request_speed_exclusion_reason)
        self.assertEqual(record.speed_duration_ms, 45000)
        self.assertEqual(record.ttft_ms, 1250)

    def test_legacy_token_count_after_tool_output_stops_at_last_model_output(self):
        record, = self.parse(context() + [
            event(3, "response_item", type="reasoning"),
            event(5, "response_item", type="function_call"),
            event(40, "response_item", type="function_call_output"),
            count(42, tokens(), tokens()),
        ])
        self.assertEqual(record.request_duration_estimate_ms, 3000)
        self.assertEqual(record.request_output_tokens_per_second_estimate, 200)
        self.assertEqual(record.request_timing_source, "last_model_output")

    def test_previous_counter_is_boundary_and_same_flush_output_does_not_start_next_request(self):
        records = self.parse(context(1) + [event(2, "response_item", type="reasoning"),
            count(4, tokens(), tokens()), event(4.001, "response_item", type="message", role="assistant"),
            event(6, "response_item", type="reasoning"), count(7, tokens(300), tokens(900, 200, 40))])
        self.assertEqual([row.request_duration_estimate_ms for row in records], [3000, 3000])
        self.assertEqual([row.request_timing_source for row in records], ["token_count", "token_count"])

    def test_latest_developer_boundary_before_first_output_wins(self):
        record, = self.parse(context(1) + [
            event(3, "response_item", type="message", role="developer"),
            event(5, "response_item", type="reasoning"), usage(7), count(9, tokens())])
        self.assertEqual(record.request_duration_estimate_ms, 4000)

    def test_modern_usage_without_matching_count_or_with_wrong_signature_is_excluded(self):
        for tail, reason in (([], "unmatched_token_count"),
                             ([count(10, tokens(601))], "usage_signature_mismatch")):
            with self.subTest(reason=reason):
                record, = self.parse(context() + [event(3, "response_item", type="reasoning"), usage()] + tail)
                self.assertIsNone(record.request_duration_estimate_ms)
                self.assertIsNone(record.request_output_tokens_per_second_estimate)
                self.assertEqual(record.request_speed_exclusion_reason, reason)

    def test_compression_and_missing_output_or_boundary_are_conservative(self):
        cases = [
            (context() + [event(3, "response_item", type="reasoning"), usage(),
                          event(9.6, "compacted"), count(10, tokens())], "compaction"),
            (context() + [usage(), count(10, tokens())], "missing_model_output"),
            ([event(3, "response_item", type="reasoning"), usage(), count(10, tokens())], "missing_start"),
        ]
        for rows, reason in cases:
            with self.subTest(reason=reason):
                record, = self.parse(rows)
                self.assertIsNone(record.request_duration_estimate_ms)
                self.assertEqual(record.request_speed_exclusion_reason, reason)

    def test_missing_or_invalid_timestamps_never_use_file_mtime_for_speed(self):
        for timestamp, reason in ((None, "missing_timestamp"),
                                  ("2026-10-14T00:00:00Z", "invalid_duration")):
            with self.subTest(timestamp=timestamp):
                row = usage()
                row["timestamp"] = timestamp
                record, = self.parse(context() + [event(3, "response_item", type="reasoning"),
                                                 row, count(10, tokens())])
                self.assertIsNone(record.request_duration_estimate_ms)
                self.assertEqual(record.request_speed_exclusion_reason, reason)

    def test_thresholds_keep_duration_but_suppress_small_speed_samples(self):
        for output, end, reason in ((199, 3, "output_below_200"),
                                   (200, 1.999, "duration_below_1000ms"),
                                   (200, 2, None)):
            with self.subTest(output=output, end=end):
                record, = self.parse(context(1) + [event(1.1, "response_item", type="reasoning"),
                                                   usage(end, output=output), count(4, tokens(output))])
                self.assertAlmostEqual(record.request_duration_estimate_ms, (end - 1) * 1000)
                self.assertEqual(record.request_speed_exclusion_reason, reason)
                if reason:
                    self.assertIsNone(record.request_output_tokens_per_second_estimate)
                else:
                    self.assertEqual(record.request_output_tokens_per_second_estimate, 200)

    def test_duplicate_response_does_not_shift_end_and_ambiguous_signatures_are_rejected(self):
        records = self.parse(context() + [event(3, "response_item", type="reasoning"),
                                          usage(5), usage(6), count(10, tokens()), usage(11)])
        self.assertEqual([row.request_duration_estimate_ms for row in records], [3000] * 3)
        records = self.parse(context() + [event(3, "response_item", type="reasoning"),
                                          usage(5), usage(6, response="different"), count(10, tokens())])
        self.assertEqual([row.request_speed_exclusion_reason for row in records], ["ambiguous_usage_match"] * 2)

    def test_delayed_first_counter_preserves_later_usage_and_in_progress_outputs(self):
        start = [context()[0], event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol")]
        for second_usage_before_count in (True, False):
            with self.subTest(second_usage_before_count=second_usage_before_count):
                rows = start + [event(1, "response_item", type="reasoning"), usage(5),
                                event(6, "response_item", type="function_call_output"),
                                event(7, "response_item", type="reasoning")]
                if second_usage_before_count:
                    rows += [usage(8, response="response-b", output=1000), count(12, tokens())]
                else:
                    rows += [count(12, tokens()), usage(13, response="response-b", output=1000)]
                rows.append(count(15, tokens(1000)))
                first, second = self.parse(rows)
                self.assertEqual(first.request_duration_estimate_ms, 5000)
                self.assertEqual(second.request_duration_estimate_ms, 2000 if second_usage_before_count else 7000)
                self.assertIsNone(second.request_speed_exclusion_reason)

    def test_equal_signatures_in_different_threads_match_only_their_own_counters(self):
        other = usage(8, response="response-b")
        other["payload"]["thread_id"] = "thread-b"
        other_count = count(15, tokens())
        other_count["payload"]["thread_id"] = "thread-b"
        first_count = count(12, tokens())
        first_count["payload"]["thread_id"] = "thread-a"
        first, second = self.parse([context()[0],
            event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol"),
            event(1, "response_item", type="reasoning"), usage(5),
            event(6, "turn_context", thread_id="thread-b", turn_id="turn-b", model="gpt-6.1-sol"),
            event(7, "response_item", type="reasoning"), other, first_count, other_count])
        self.assertEqual(first.request_duration_estimate_ms, 5000)
        self.assertEqual(second.request_duration_estimate_ms, 2000)
        self.assertIsNone(first.request_speed_exclusion_reason)
        self.assertIsNone(second.request_speed_exclusion_reason)

    def test_duplicate_of_a_matched_request_does_not_reset_the_next_output_window(self):
        records = self.parse([context()[0],
            event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol"),
            event(1, "response_item", type="reasoning"), usage(5), count(5.1, tokens()),
            event(6, "response_item", type="function_call_output"),
            event(7, "response_item", type="reasoning"), usage(7.5),
            usage(8, response="response-b", output=1000), count(9, tokens(1000))])
        self.assertEqual([row.request_duration_estimate_ms for row in records], [5000, 5000, 2000])

    def test_final_output_in_same_flush_after_usage_does_not_open_a_false_request(self):
        records = self.parse([context()[0],
            event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol"),
            event(1, "response_item", type="reasoning"), usage(5),
            event(5.01, "response_item", type="message", role="assistant"), count(5.1, tokens()),
            event(6, "response_item", type="function_call_output"),
            event(7, "response_item", type="reasoning"),
            usage(8, response="response-b", output=1000), count(9, tokens(1000))])
        self.assertEqual([row.request_duration_estimate_ms for row in records], [5000, 2000])

    def test_changed_signature_on_replayed_response_does_not_consume_new_outputs(self):
        records = self.parse([context()[0],
            event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol"),
            event(1, "response_item", type="reasoning"), usage(5), count(5.1, tokens()),
            event(6, "response_item", type="function_call_output"),
            event(7, "response_item", type="reasoning"), usage(7.5, output=601),
            usage(8, response="response-b", output=1000), count(9, tokens(1000))])
        self.assertEqual(records[1].request_speed_exclusion_reason, "usage_signature_mismatch")
        self.assertIsNone(records[1].request_output_tokens_per_second_estimate)
        self.assertEqual(records[2].request_duration_estimate_ms, 2000)

    def test_interleaved_legacy_calls_without_assignable_boundaries_never_include_tool_wait(self):
        records = self.parse([context()[0],
            event(0, "turn_context", turn_id="turn-a", model="gpt-6.1-sol"),
            event(1, "response_item", type="reasoning"),
            event(6, "response_item", type="function_call_output"),
            event(7, "response_item", type="reasoning"),
            count(12, tokens(), tokens()), count(15, tokens(1000), tokens(1600, 200, 40))])
        self.assertTrue(all(row.request_output_tokens_per_second_estimate is None for row in records))
        self.assertEqual(records[0].request_speed_exclusion_reason, "interrupted_request")

    def test_cumulative_delta_requires_a_baseline(self):
        records = self.parse(context(1) + [event(2, "response_item", type="reasoning"),
            count(3, total=tokens()), event(4, "response_item", type="reasoning"),
            count(6, total=tokens(1000, 200, 40))])
        self.assertEqual(records[0].request_speed_exclusion_reason, "missing_cumulative_baseline")
        self.assertEqual(records[1].usage.output_tokens, 400)
        self.assertEqual(records[1].request_duration_estimate_ms, 3000)
        self.assertAlmostEqual(records[1].request_output_tokens_per_second_estimate, 400 / 3, places=5)

    def test_history_migration_rescans_and_retains_fields_after_logs_are_removed(self):
        db_path = self.home / "usage.sqlite3"
        write(self.path, modern_request())
        db = DashboardDB(db_path, roots=[self.sessions], timezone_name="UTC")
        try:
            db.scan()
            first = db.requests(days=0)["data"][0]
            self.assertEqual(first["request_duration_estimate_ms"], 7500)
            self.assertEqual(db.request_detail(first["request_id"]), first)
            self.assertEqual(db.counts()["usage_records"], 1)
        finally:
            db.close()
        with closing(sqlite3.connect(db_path)) as original, original:
            original.execute("DROP VIEW usage_records")
            for field in ("request_duration_estimate_ms", "request_output_tokens_per_second_estimate",
                          "request_timing_source", "request_speed_exclusion_reason"):
                original.execute("ALTER TABLE history_snapshots DROP COLUMN " + field)
            original.execute("UPDATE source_files SET parser_version=6")
            # A pre-upgrade fingerprint did not include the new timing fields.
            original.execute("UPDATE history_snapshots SET fingerprint=zeroblob(32)")
        db = DashboardDB(db_path, roots=[self.sessions], timezone_name="UTC")
        try:
            self.assertTrue(Path(str(db_path) + ".pre-request-speed.bak").is_file())
            self.assertEqual(db.counts()["usage_records"], 1)
            self.assertEqual(db.scan().files_scanned, 1)
            row = db.requests(days=0)["data"][0]
            self.assertEqual(row, first)
            self.path.unlink()
            db.scan()
        finally:
            db.close()
        db = DashboardDB(db_path, roots=[self.sessions], timezone_name="UTC")
        try:
            self.assertEqual(db.request_detail(first["request_id"]), first)
            exported = json.dumps(db.request_detail(first["request_id"]))
            self.assertNotIn("PRIVATE_", exported)
            self.assertNotIn(str(self.path), exported)
        finally:
            db.close()


class LiveScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.root = self.home / "sessions"
        self.root.mkdir()
        self.path = self.root / "one.jsonl"
        self.db_path = self.home / "usage.sqlite3"
        self.db = DashboardDB(self.db_path, roots=[self.root], timezone_name="UTC")
        write(self.path, modern_request())
        self.db.scan()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_complete_prefix_is_visible_while_log_grows_and_next_scan_retries(self):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage(50, response="response-b")) + "\n")
        original = self.db.parser.parse

        def grows(path):
            parsed = original(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(usage(55, response="response-c")) + "\n")
            return parsed

        with patch.object(self.db.parser, "parse", grows):
            result = self.db.scan()
        self.assertEqual(result.incomplete_files, 1)
        self.assertEqual(self.db.requests(days=0)["total"], 2)
        self.assertEqual(self.db.scan().files_scanned, 1)
        self.assertEqual(self.db.requests(days=0)["total"], 3)
        self.assertEqual(self.db.scan().files_unchanged, 1)

    def test_partial_tail_does_not_hide_complete_new_requests_or_delete_history(self):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage(50, response="response-b")) + "\n" + '{"type":')
        result = self.db.scan()
        self.assertEqual(result.incomplete_files, 1)
        self.assertEqual(self.db.requests(days=0)["total"], 2)
        self.assertEqual(self.db.scan().files_scanned, 1)

    def test_modern_schema_supersedes_legacy_even_during_continued_growth(self):
        write(self.path, context() + [event(3, "response_item", type="reasoning"), count(5, tokens(), tokens())])
        self.db.scan()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage(5)) + "\n")
        original = self.db.parser.parse

        def grows(path):
            parsed = original(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            return parsed

        with patch.object(self.db.parser, "parse", grows):
            self.db.scan()
        self.assertEqual(self.db.requests(days=0)["total"], 1)

    def test_requests_and_details_do_not_wait_for_parse_or_the_writer_lock(self):
        request_id = self.db.requests(days=0)["data"][0]["request_id"]
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        failures = []
        original = self.db.parser.parse

        def parse(path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test parse timeout")
            return original(path)

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with patch.object(self.db.parser, "parse", parse):
            worker = threading.Thread(target=self.db.scan)
            worker.start()
            self.assertTrue(entered.wait(2))

            def read():
                try:
                    self.assertEqual(self.db.requests(days=0)["total"], 1)
                    self.assertEqual(self.db.request_detail(request_id)["request_id"], request_id)
                except BaseException as error:
                    failures.append(error)
                finally:
                    done.set()

            reader = threading.Thread(target=read)
            try:
                # Also prove WAL reads remain available during the commit lock.
                with self.db._lock:
                    reader.start()
                    self.assertTrue(done.wait(2), "request reads waited for the writer lock")
            finally:
                release.set()
                worker.join(5)
                reader.join(5)
            self.assertEqual(failures, [])
            self.assertFalse(worker.is_alive())

    def test_request_pagination_hydrates_only_selected_rows_and_empty_relations_once(self):
        with patch.object(self.db, "_request_dict", wraps=self.db._request_dict) as hydrate:
            page = self.db.requests(days=0, page_size=1)
        self.assertEqual(hydrate.call_count, 1)
        self.assertEqual(page["total"], 1)
        with patch.object(self.db, "_thread_relationship_map", side_effect=AssertionError("extra read")):
            row = self.db._query_filtered_rows(days=0)[0]
            self.db._request_dict(row, {})

    def test_dashboard_data_version_is_stable_until_a_committed_change(self):
        first = self.db.dashboard(days=0)["data_version"]
        self.db.scan()
        self.assertEqual(self.db.dashboard(days=0)["data_version"], first)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage(50, response="response-b")) + "\n")
        self.db.scan()
        self.assertGreater(self.db.dashboard(days=0)["data_version"], first)


if __name__ == "__main__":
    unittest.main()
