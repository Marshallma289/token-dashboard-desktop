import datetime as dt
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import DashboardDB, DashboardHandler, DashboardService, ScanResult, main
from tests.test_hermes import make_hermes
from tests.test_review_regressions import session, turn, usage, write_lines


class ScanOptimizationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / 'sessions'
        self.root.mkdir()
        self.path = self.root / 'one.jsonl'
        self.hermes = self.home / 'hermes' / 'state.db'
        self.db = DashboardDB(self.home / 'dashboard.db', roots=[self.root], timezone_name='UTC',
                              clock=lambda: dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc),
                              hermes_db=self.hermes)
        self.addCleanup(self.db.close)

    def write(self, *events):
        write_lines(self.path, [session(), turn('gpt-test'), *events])

    def tokens(self):
        return self.db.dashboard(0)['summary']['total_tokens']

    def test_damaged_modern_only_adds_new_stable_keys_and_retries(self):
        self.write(usage('thread-a', 'old', 200))
        self.db.scan()
        self.db._connection.execute("INSERT INTO history_meta(key,value) VALUES ('cleanup_before','2026-09-18')")
        self.db._connection.commit()
        self.write(usage('thread-a', 'old', 900), '{bad', usage('thread-a', 'new', 110),
                   usage('thread-a', 'new', 110), usage('thread-a', '', 300),
                   usage('thread-a', 'purged', 400, '2026-09-17T00:00:00Z'),
                   usage('thread-a', 'tail', 500))
        self.path.write_bytes(self.path.read_bytes().rstrip(b'\r\n'))
        result = self.db.scan()
        self.assertEqual((result.parse_errors, result.records_added, self.tokens()), (1, 1, 310))
        version = self.db._data_version
        result = self.db.scan()
        self.assertEqual((result.files_scanned, result.records_added, self.tokens()), (1, 0, 310))
        self.assertEqual(self.db._data_version, version)

    def test_damaged_legacy_to_modern_waits_for_clean_confirmation(self):
        legacy = {'type': 'event_msg', 'timestamp': '2026-09-18T16:30:00Z', 'payload': {
            'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 90, 'output_tokens': 10}}}}
        self.write(legacy)
        self.db.scan()
        modern = usage('thread-a', 'r1', 150)
        self.write(legacy, '{bad', modern)
        self.assertEqual(self.db.scan().records_added, 0)
        self.assertEqual(self.tokens(), 100)
        self.write(legacy, modern)
        self.db.scan()
        self.assertEqual(self.tokens(), 150)

    def test_equal_size_and_mtime_replacement_during_parse_is_not_salvaged(self):
        self.write(usage('thread-a', 'old', 110))
        self.db.scan()
        self.write(usage('thread-a', 'old', 110), '{bad', usage('thread-a', 'new', 120))
        replacement = self.root / 'incoming.tmp'
        replacement.write_bytes(self.path.read_bytes())
        stamp = self.path.stat()
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        original = self.db.parser.parse

        def replace_after_parse(path):
            parsed = original(path)
            replacement.replace(path)
            return parsed

        with patch.object(self.db.parser, 'parse', side_effect=replace_after_parse):
            result = self.db.scan()
        self.assertEqual((result.incomplete_files, result.records_added, self.tokens()), (1, 0, 110))
        self.assertEqual((self.db.scan().records_added, self.tokens()), (1, 230))

    def test_same_signature_correction_requires_force_for_both_sources(self):
        make_hermes(self.hermes, input_tokens=1000)
        self.write(usage('thread-a', 'r1', 110))
        self.db.scan()
        stamps = {p: p.stat() for p in (self.path, self.hermes)}
        self.write(usage('thread-a', 'r1', 200))
        with sqlite3.connect(self.hermes) as conn:
            conn.execute('UPDATE session_model_usage SET input_tokens=2000')
        conn.close()
        for path, stamp in stamps.items():
            self.assertEqual(path.stat().st_size, stamp.st_size)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.db.scan().files_unchanged, 2)
        result = DashboardService(self.db).refresh(force=True)
        self.assertEqual((result.files_scanned, result.hermes_rows_read), (2, 1))
        self.assertEqual(self.db.dashboard(0, source_kind='codex')['summary']['total_tokens'], 200)
        self.assertEqual(self.db.dashboard(0, source_kind='hermes')['summary']['total_tokens'], 2350)

    def test_manual_http_and_cli_pass_force(self):
        service = Mock()
        service.refresh.return_value = ScanResult()
        for endpoint in ('/api/scan/now', '/api/refresh'):
            handler = SimpleNamespace(path=endpoint, app=(service, '127.0.0.1'),
                                      _read_mutation_json=lambda: {}, _write_json=Mock())
            DashboardHandler.do_POST(handler)
            service.refresh.assert_called_once_with(force=True)
            service.refresh.reset_mock()
        with patch('backend._database_from_args') as factory, patch('backend._print_json'):
            self.assertEqual(main(['scan']), 0)
            factory.return_value.scan.assert_called_once_with(force=True)

    def test_background_queue_defaults_to_fast_and_coalesces_explicit_force(self):
        for force in (False, True):
            service = DashboardService(self.db)
            service.request_refresh(force=force)
            service.request_refresh()
            with patch.object(service, 'refresh', side_effect=lambda **_: service._stop.set()) as refresh:
                service._run()
            refresh.assert_called_once_with(**({'force': True} if force else {}))

    def test_dated_all_source_secondary_read_is_hermes_only(self):
        today = self.db._local_now().date()
        yesterday = today - dt.timedelta(days=1)
        rows = [dict(source_kind='hermes', total_tokens=40, api_call_count=1, interval=interval)
                for interval in ((None, None), (yesterday, today), (yesterday, yesterday))]
        with patch.object(self.db, '_fetch_rows', side_effect=[[], rows] * 2) as fetch, \
                patch.object(self.db, '_hermes_interval', side_effect=lambda row: row['interval']), \
                patch.object(self.db, '_hermes_undated', return_value=True):
            for source_kind in (None, 'all'):
                data = self.db._build_dashboard(days=1, source_kind=source_kind)
                self.assertEqual(data['time_attribution']['unallocated_records'], 2)
                self.assertTrue(data['time_attribution']['summary_incomplete'])
            self.assertEqual([call.kwargs['source_kind'] for call in fetch.call_args_list],
                             [None, 'hermes', 'all', 'hermes'])
