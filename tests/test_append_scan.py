import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import DashboardDB
from tests.test_request_estimates import context, count, event, tokens, usage as timed_usage
from tests.test_review_regressions import session, turn, usage, write_lines


class AppendScanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / 'sessions'
        self.root.mkdir()
        self.path = self.root / 'one.jsonl'
        self.db = self.open_db('dashboard.db')

    def open_db(self, name):
        db = DashboardDB(self.home / name, roots=[self.root], timezone_name='UTC')
        self.addCleanup(db.close)
        return db

    def append(self, *rows):
        with self.path.open('ab') as out:
            for row in rows:
                out.write((json.dumps(row) if not isinstance(row, str) else row).encode() + b'\n')

    def initial(self, rows):
        write_lines(self.path, rows)
        self.db.scan()

    def simple(self, *totals):
        self.initial([session(), turn('gpt-test'), *(usage('thread-a', f'r{i}', total)
                                                       for i, total in enumerate(totals))])

    def selected_rows(self, db):
        return [tuple(row) for row in db._connection.execute('''SELECT * FROM usage_records
            ORDER BY request_id''')]

    def assert_full_equivalence(self):
        full = self.open_db('independent.db')
        full.scan(force=True)
        self.assertEqual(self.selected_rows(self.db), self.selected_rows(full))

    def test_thousand_old_json_lines_not_decoded_and_one_snapshot_written(self):
        self.simple(*([110] * 1000))
        self.append(usage('thread-a', 'new', 121))
        decoded_usage = []
        loads = json.loads

        def tracked_loads(text, *args, **kwargs):
            obj = loads(text, *args, **kwargs)
            if isinstance(obj, dict) and obj.get('type') == 'token_usage_record':
                decoded_usage.append(obj['payload']['response_id'])
            return obj

        with patch('backend.json.loads', side_effect=tracked_loads), \
                patch.object(self.db, '_store_snapshot', wraps=self.db._store_snapshot) as store, \
                patch.object(self.db, '_detach_source', wraps=self.db._detach_source) as detach, \
                patch.object(self.db, '_rebuild_logical_records', wraps=self.db._rebuild_logical_records) as rebuild:
            result = self.db.scan()
            self.assertEqual((result.records_added, store.call_count, decoded_usage), (1, 1, ['new']))
            detach.assert_not_called()
            self.assertEqual(len(rebuild.call_args.args[0]), 1)
        self.assert_full_equivalence()

    def test_late_completion_updates_old_key_complex_append_skips_unaffected(self):
        self.initial(context() + [event(5, 'response_item', type='reasoning'), timed_usage(), count(15, tokens()),
                                  timed_usage(17, 'other')])
        self.append(event(30, 'event_msg', type='task_complete', turn_id='turn-a', duration_ms=30000,
                          time_to_first_token_ms=1500))
        with patch.object(self.db, '_store_snapshot', wraps=self.db._store_snapshot) as store:
            self.assertEqual(self.db.scan().records_added, 2)
        self.assertEqual(store.call_count, 2)
        self.assertEqual(self.db.dashboard(0)['summary']['request_count'], 2)
        self.assertEqual(self.db._connection.execute('SELECT MAX(ttft_ms) FROM usage_records').fetchone()[0], 1500)
        # An event for a new turn should not rewrite either previously completed key.
        self.append(event(32, 'turn_context', turn_id='new-turn', model='gpt-6.1-sol'),
                    event(34, 'token_usage_record', thread_id='thread-a', turn_id='new-turn',
                          response_id='new', usage=tokens()))
        with patch.object(self.db, '_store_snapshot', wraps=self.db._store_snapshot) as store:
            self.assertEqual(self.db.scan().records_added, 1)
        self.assertEqual(store.call_count, 1)
        self.assert_full_equivalence()

    def test_duplicate_response_falls_back_for_ranking_without_rewriting_other_key(self):
        self.simple(200, 300)
        self.append(usage('thread-a', 'r0', 120), usage('thread-a', 'r0', 410))
        with patch.object(self.db, '_detach_source', wraps=self.db._detach_source) as detach, \
                patch.object(self.db, '_rebuild_logical_records', wraps=self.db._rebuild_logical_records) as rebuild:
            self.assertEqual(self.db.scan().records_added, 1)
            detach.assert_not_called()
            self.assertEqual(len(rebuild.call_args.args[0]), 1)
        self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'], 710)
        self.assert_full_equivalence()

    def test_no_change_after_blank_append_does_not_touch_history(self):
        self.simple(110, 120)
        version = self.db._data_version
        self.append('')
        with patch.object(self.db.parser, 'parse', wraps=self.db.parser.parse) as parser, \
                patch.object(self.db, '_store_snapshot') as store, \
                patch.object(self.db, '_rebuild_logical_records') as rebuild:
            self.assertEqual(self.db.scan().records_added, 0)
            self.assertNotIn('simple_events', parser.call_args.kwargs)
            store.assert_not_called()
            rebuild.assert_not_called()
        self.assertEqual(self.db._data_version, version)

    def test_legacy_to_modern_is_a_full_replacement(self):
        self.initial(context() + [count(5, tokens())])
        self.append(timed_usage())
        with patch.object(self.db, '_detach_source', wraps=self.db._detach_source) as detach:
            self.assertEqual(self.db.scan().records_added, 1)
            detach.assert_called_once()
        self.assertEqual(self.db.dashboard(0)['summary']['request_count'], 1)
        self.assert_full_equivalence()

    def test_middle_mutation_then_growth_fails_prefix_verification(self):
        self.simple(110, 220, 330)
        original = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b'"total_tokens": 220', b'"total_tokens": 290'))
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.append(usage('thread-a', 'new', 440))
        with patch.object(self.db, '_store_snapshot', wraps=self.db._store_snapshot) as store:
            self.assertEqual(self.db.scan().records_added, 4)
            self.assertEqual(store.call_count, 4)
        self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'], 1170)
        self.assert_full_equivalence()

    def test_failed_transaction_does_not_publish_checkpoint_and_retries_full(self):
        self.simple(110)
        self.append(usage('thread-a', 'new', 120))
        with patch.object(self.db, '_rebuild_logical_records', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                self.db.scan()
        self.assertFalse(self.db._append_cache)
        self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'], 110)
        self.assertEqual(self.db.scan().records_added, 2)
        self.assert_full_equivalence()

    def test_force_and_record_limit_fall_back(self):
        self.simple(110, 120)
        self.db.scan(force=True)
        self.assertFalse(self.db._append_cache)
        self.append(usage('thread-a', 'new', 130))
        with patch('backend._APPEND_MAX_RECORDS', 2):
            self.assertEqual(self.db.scan().records_added, 3)
        self.assertFalse(self.db._append_cache)
        self.assert_full_equivalence()

    def test_cleanup_invalidates_checkpoint_and_new_scan_honors_cutoff(self):
        self.db._clock = lambda: dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)
        self.simple(110, 120)
        self.assertTrue(self.db._append_cache)
        self.db.cleanup_history(1)
        self.assertFalse(self.db._append_cache)
        self.append(usage('thread-a', 'late-old', 300),
                    usage('thread-a', 'new', 400, '2026-09-21T12:00:00Z'))
        self.assertEqual(self.db.scan().records_added, 1)
        self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'], 400)
