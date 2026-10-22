import datetime as dt
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen
from unittest.mock import patch

from backend import DashboardDB
from backend import DashboardService, create_server


def make_hermes(path, **overrides):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT,
          started_at REAL, model TEXT, billing_provider TEXT, cwd TEXT, profile_name TEXT);
        CREATE TABLE session_model_usage(session_id TEXT, model TEXT, billing_provider TEXT,
          billing_base_url TEXT, billing_mode TEXT, task TEXT, api_call_count INTEGER,
          input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
          cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL,
          actual_cost_usd REAL, cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL);
    ''')
    row = dict(session_id='session-a', model='gpt-6.1-sol', billing_provider='custom',
               billing_base_url='https://example.test/v1', billing_mode='', task='',
               api_call_count=7, input_tokens=1000, output_tokens=200, cache_read_tokens=100,
               cache_write_tokens=50, reasoning_tokens=80, estimated_cost_usd=0.,
               actual_cost_usd=0., cost_status='unknown', cost_source='none',
               first_seen=1792598400.25, last_seen=1792686600.5)
    row.update(overrides)
    connection.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
        (row['session_id'], 'weixin', 'test', row['first_seen'], row['model'], 'custom', '', 'default'))
    connection.execute('INSERT INTO session_model_usage VALUES (%s)' % ','.join('?' * len(row)), tuple(row.values()))
    connection.commit()
    connection.close()
    return row


class HermesSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'state.db'

    def source_module(self):
        self.assertIsNotNone(importlib.util.find_spec('hermes_source'), 'HermesSource reader is missing')
        import hermes_source
        return hermes_source

    def test_read_aggregate_and_local_day(self):
        make_hermes(self.path)
        source = self.source_module().HermesSource(self.path, dt.timezone(dt.timedelta(hours=8)))
        rows, watermark = source.read()
        self.assertEqual(watermark, (1792686600.5, 1))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['api_call_count'], row['total_tokens']), (7, 1350))
        self.assertEqual((row['cached_input_tokens'], row['cache_write_input_tokens'], row['reasoning_output_tokens']), (100, 50, 80))
        self.assertEqual(row['day'], '2026-10-23')
        self.assertEqual((row['workspace'], row['workspace_source']), ('weixin', 'sessions.source'))
        self.assertEqual((row['thread_key'], row['task_id'], row['agent_role']), ('session-a', '', 'main'))
        self.assertIsNone(row['ttft_ms'])
        self.assertEqual(row['speed_exclusion_reason'], 'hermes_aggregate_no_request_boundary')

    def test_paths_precedence_profiles_and_missing(self):
        module = self.source_module()
        explicit = self.home / 'other' / 'state.db'
        profile = explicit.parent / 'profiles' / 'p1' / 'state.db'
        make_hermes(profile)
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            self.assertEqual(module.resolve_hermes_paths(explicit), [explicit, profile])
            self.assertEqual(module.resolve_hermes_paths(), [self.path])
        self.assertEqual(module.HermesSource(explicit).read(), ([], (None, 0)))

    def test_readonly_wal_without_new_sidecars_or_source_changes(self):
        make_hermes(self.path)
        writer = sqlite3.connect(self.path)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.close()  # closed WAL database has no sidecars
        before = (self.path.stat().st_mtime_ns, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertFalse(Path(str(self.path) + '-wal').exists())
        rows, _ = self.source_module().HermesSource(self.path).read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(before, (self.path.stat().st_mtime_ns, hashlib.sha256(self.path.read_bytes()).hexdigest()))
        self.assertFalse(Path(str(self.path) + '-wal').exists())
        self.assertFalse(Path(str(self.path) + '-shm').exists())

    def test_live_wal_commits_are_visible_without_touching_source(self):
        make_hermes(self.path)
        writer = sqlite3.connect(self.path)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('UPDATE session_model_usage SET api_call_count=9')
            writer.commit()
            paths = [self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm')]
            before = [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
            rows, _ = self.source_module().HermesSource(self.path).read()
            self.assertEqual(rows[0]['api_call_count'], 9)
            self.assertEqual(before, [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths])
        finally:
            writer.close()


class HermesMigrationTests(unittest.TestCase):
    def test_old_ledger_defaults_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.db'
            root = Path(folder) / 'empty'
            root.mkdir()
            (root / 'old.jsonl').write_text('''{"timestamp":"2026-10-22T12:00:00Z","type":"token_usage_record","payload":{"response_id":"old","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}''', encoding='utf-8')
            db = DashboardDB(path, roots=[root])
            db.scan()
            db.close()
            connection = sqlite3.connect(path)
            columns = {r[1] for r in connection.execute('PRAGMA table_info(history_snapshots)')}
            # Build the previous release schema even after this release gains columns.
            connection.execute('DROP VIEW usage_records')
            for name in ('source_kind', 'api_call_count', 'hermes_metadata'):
                if name in columns:
                    connection.execute('ALTER TABLE history_snapshots DROP COLUMN ' + name)
            connection.commit()
            connection.close()
            db = DashboardDB(path, roots=[Path(folder) / 'empty'])
            try:
                cols = {r[1]: r[4] for r in db._connection.execute('PRAGMA table_info(history_snapshots)')}
                self.assertIn('source_kind', cols, 'old ledger must gain source_kind')
                self.assertEqual(cols['source_kind'], "'codex'")
                self.assertEqual(cols['api_call_count'], '1')
                self.assertTrue(Path(str(path) + '.pre-hermes.bak').exists())
                self.assertIn('source_kind', [r[1] for r in db._connection.execute('PRAGMA table_info(usage_records)')])
                self.assertEqual(db.dashboard(0, source_kind='codex')['summary']['request_count'], 1)
                self.assertEqual(db.dashboard(0)['summary']['total_tokens'], 15)
            finally:
                db.close()


class HermesScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / 'hermes' / 'state.db'
        make_hermes(self.source)

    def database(self):
        import inspect
        self.assertIn('hermes_db', inspect.signature(DashboardDB).parameters, 'Hermes scan is not connected')
        db = DashboardDB(self.home / 'usage.db', roots=[self.home / 'empty'], hermes_db=self.source)
        self.addCleanup(db.close)
        return db

    def test_scan_watermark_and_update_replaces_aggregate(self):
        db = self.database()
        first = db.scan()
        rows = db._connection.execute('SELECT * FROM usage_records').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['source_kind'], rows[0]['api_call_count']), ('hermes', 7))
        self.assertEqual(first.hermes_rows_read, 1)
        second = db.scan()
        self.assertEqual(second.hermes_rows_read, 0)
        writer = sqlite3.connect(self.source)
        writer.execute('UPDATE session_model_usage SET api_call_count=9,input_tokens=900,last_seen=last_seen+10')
        writer.commit()
        writer.close()
        db.scan()
        rows = db._connection.execute('SELECT * FROM usage_records').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['api_call_count'], rows[0]['input_tokens']), (9, 900))

    def test_profiles_and_missing_source_preserve_history(self):
        make_hermes(self.source.parent / 'profiles/p1/state.db', api_call_count=3)
        db = self.database()
        db.scan()
        self.assertEqual(db.counts()['usage_records'], 2)
        self.source.unlink()
        db.scan()
        self.assertEqual(db.counts()['usage_records'], 2)
        self.assertEqual(db.scan().hermes_unavailable, 1)

    def test_older_session_update_below_global_watermark_is_imported(self):
        writer = sqlite3.connect(self.source)
        writer.execute("INSERT INTO session_model_usage SELECT 'session-b',model,billing_provider,billing_base_url,billing_mode,task,api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,cost_status,cost_source,first_seen,last_seen+10000 FROM session_model_usage")
        writer.commit()
        writer.close()
        db = self.database()
        db.scan()
        writer = sqlite3.connect(self.source)
        writer.execute("UPDATE session_model_usage SET api_call_count=9,input_tokens=2000,last_seen=last_seen+1 WHERE session_id='session-a'")
        writer.commit()
        writer.close()
        db.scan()
        self.assertEqual(db.dashboard(0)['summary']['request_count'], 16, 'global max timestamp cannot hide an older session update')
        self.assertEqual(db.dashboard(0)['summary']['input_tokens'], 3000)


class HermesAggregationTests(HermesScanTests):
    def test_cache_ratio_uses_combined_denominator_for_hermes(self):
        """Hermes 的 cache_read 与 input 并列，分母须相加，比例仍然有定义。

        旧版本按 Codex 语义（input 已含 cache_read）只拿 input 当分母，
        Hermes 场景下 cached > input 会被保护条件打成 None，界面上永远是「—」。
        """
        writer = sqlite3.connect(self.source)
        writer.execute('UPDATE session_model_usage SET cache_read_tokens=2000')
        writer.commit()
        writer.close()
        db = self.database()
        db.scan()
        summary = db.dashboard(0)['summary']
        self.assertEqual(summary['cached_input_tokens'], 2000)
        self.assertEqual(summary['input_tokens'], 1000)
        self.assertIsNotNone(summary['cache_hit_rate'],
                             'Hermes 缓存命中率应计入并列的 input，而不是返回 None')
        self.assertAlmostEqual(summary['cache_hit_rate'], 2000 / 3050, places=4)
        self.assertLessEqual(summary['cache_hit_rate'], 1.0)

    def test_weighted_counts_in_every_group_and_record_pagination(self):
        db = self.database()
        db.scan()
        data = db.dashboard(days=0)
        self.assertEqual(data['summary']['request_count'], 7, 'requests must be weighted, not aggregate row count')
        self.assertEqual(data['summary']['priced_request_count'], 7)
        self.assertEqual(data['summary']['cache_read_available_count'], 7)
        for key in ('daily_model_usage', 'daily_activity', 'daily_active', 'workspace_distribution'):
            self.assertEqual(sum(r['request_count'] for r in data[key]), 7, key)
        self.assertEqual(db.sessions(days=0)['data'][0]['request_count'], 7)
        self.assertEqual(db.tasks(days=0)['data'][0]['summary']['request_count'], 7)
        listing = db.requests(days=0)
        self.assertEqual((listing['total'], listing['request_count']), (1, 7))
        self.assertEqual(listing['data'][0]['api_call_count'], 7)

    def test_source_kind_filter_and_profile_session_isolation(self):
        db = self.database()
        root = self.home / 'empty'
        root.mkdir()
        (root / 'codex.jsonl').write_text('''{"timestamp":"2026-10-22T12:00:00Z","type":"token_usage_record","payload":{"response_id":"one","thread_id":"session-a","model":"gpt-6.1-sol","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}''', encoding='utf-8')
        db.scan()
        self.assertEqual(db.dashboard(0)['summary']['request_count'], 8)
        self.assertEqual(db.dashboard(0, source_kind='codex')['summary']['request_count'], 1)
        self.assertEqual(db.dashboard(0, source_kind='hermes')['summary']['request_count'], 7)
        self.assertEqual(db.requests(days=0, source_kind='codex')['total'], 1)
        self.assertEqual(db.sessions(days=0, source_kind='hermes')['total'], 1)
        with self.assertRaises(ValueError):
            db.dashboard(0, source_kind='other')

    def test_management_and_repricing_counts_are_weighted(self):
        db = self.database()
        db.scan()
        self.assertEqual(db.data_settings()['request_count'], 7)
        preview = db.reprice_preview(days=0)
        self.assertEqual(preview['request_count'], 7)

        result = db.reprice_apply(preview['preview_token'])
        self.assertEqual(result['updated_count'], 7)
        with db._lock:
            preview = db._cleanup_preview_locked(1, dt.date(2030, 1, 1))
        self.assertEqual(preview['request_count'], 7)

    def test_repricing_respects_source_filter(self):
        db = self.database()
        db.scan()
        self.assertEqual(db.reprice_preview(days=0, source_kind='codex')['request_count'], 0)

    def test_reprice_preview_detects_call_count_only_update(self):
        db = self.database()
        db.scan()
        preview = db.reprice_preview(days=0)
        writer = sqlite3.connect(self.source)
        writer.execute('UPDATE session_model_usage SET api_call_count=9')
        writer.commit()
        writer.close()
        db.scan()
        with self.assertRaisesRegex(ValueError, 'usage data changed'):
            db.reprice_apply(preview['preview_token'])

    def test_request_metrics_exclude_hermes_and_state_reason(self):
        db = self.database()
        db.scan()
        data = db.dashboard(0, source_kind='hermes')
        self.assertEqual(data['request_token_distribution']['request_count'], 0, 'aggregate is not a request-size sample')
        self.assertIsNone(data['request_token_distribution']['p50'])
        self.assertEqual(data['request_metrics']['scope'], 'not_applicable')
        self.assertIn('未提供', data['request_metrics']['reason'])
        self.assertEqual(db._connection.execute('SELECT COUNT(*) FROM thread_relationships').fetchone()[0], 0)
        root = self.home / 'empty'
        root.mkdir()
        (root / 'one.jsonl').write_text('''{"timestamp":"2026-10-22T12:00:00Z","type":"token_usage_record","payload":{"response_id":"one","thread_id":"t","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}''', encoding='utf-8')
        db.scan()
        data = db.dashboard(0)
        self.assertEqual(data['summary']['request_count'], 8)
        self.assertEqual(data['request_token_distribution']['request_count'], 1)
        self.assertEqual(data['request_token_distribution']['p50'], 15)
        self.assertEqual(data['request_metrics']['scope'], 'codex_only')

    def test_source_labels_and_raw_session_drilldown(self):
        db = self.database()
        db.scan()
        listing = db.requests(days=0, source_kind='hermes', thread_key='session-a')
        self.assertEqual(listing['request_count'], 7)
        self.assertEqual(listing['data'][0]['provider_label'], 'Hermes 自定义供应商')
        self.assertEqual(listing['data'][0]['task_id'], '')
        self.assertEqual(db.sessions(days=0, source_kind='hermes')['data'][0]['first_seen'], '2026-10-21T16:00:00.250000+00:00')

    def test_profile_session_drilldown_and_task_classification(self):
        make_hermes(self.source.parent / 'profiles/p1/state.db', api_call_count=3)
        db = self.database()
        db.scan()
        sessions = db.sessions(days=0, source_kind='hermes')['data']
        self.assertEqual(len(sessions), 2)
        selected = next(s for s in sessions if s['request_count'] == 3)
        self.assertIn('source_identity', selected)
        import inspect
        self.assertIn('source_identity', inspect.signature(db.requests).parameters, 'profile drilldown must keep source identity')
        rows = db.requests(days=0, source_kind='hermes', thread_key=selected['thread_key'], source_identity=selected['source_identity'])
        self.assertEqual(rows['request_count'], 3)
        task = db.tasks(days=0, source_kind='hermes')['data'][0]
        self.assertEqual(task['source_kind'], 'hermes')
        self.assertFalse(task['relationship_available'])


class HermesPricingTests(HermesScanTests):
    def test_frozen_reference_note_is_idempotent_across_updates(self):
        db = self.database()
        db.scan()
        for _ in range(2):
            writer = sqlite3.connect(self.source)
            writer.execute('UPDATE session_model_usage SET last_seen=last_seen+1')
            writer.commit()
            writer.close()
            db.scan()
        note = db.requests(days=0)['data'][0]['rate_snapshot']['note']
        self.assertEqual(note.count('无法判断是否触发长上下文阶梯价'), 1)

    def test_long_context_aggregate_uses_standard_reference_rates(self):
        writer = sqlite3.connect(self.source)
        writer.execute('UPDATE session_model_usage SET input_tokens=1000000,cache_read_tokens=0,cache_write_tokens=0,output_tokens=0')
        writer.commit()
        writer.close()
        db = self.database()
        db.scan()
        item = db.requests(days=0)['data'][0]
        self.assertAlmostEqual(item['cost_before_discount_usd'], 2.0)
        self.assertIn('无法判断是否触发长上下文阶梯价', item['rate_snapshot']['note'])
        preview = db.reprice_preview(days=0)
        self.assertAlmostEqual(preview['cost_before_discount_usd'], 2.0)

    def test_self_report_fallback_requires_positive_valid_status(self):
        writer = sqlite3.connect(self.source)
        writer.execute("UPDATE session_model_usage SET model='unknown-model',estimated_cost_usd=1.25,cost_status='estimated',cost_source='provider'")
        writer.commit()
        writer.close()
        db = self.database()
        db.scan()
        item = db.requests(days=0)['data'][0]
        self.assertIsNotNone(item['cost_before_discount_usd'], 'valid positive Hermes estimate must be used')
        self.assertAlmostEqual(item['cost_before_discount_usd'], 1.25)
        self.assertEqual(item['rate_snapshot']['cost_origin'], 'hermes_reported')
        self.assertEqual(item['hermes_estimated_cost_usd'], 1.25)
        writer = sqlite3.connect(self.source)
        writer.execute("UPDATE session_model_usage SET cost_status='unknown',last_seen=last_seen+1")
        writer.commit()
        writer.close()
        db.scan()
        item = db.requests(days=0)['data'][0]
        self.assertEqual(item['pricing_status'], 'unpriced')
        self.assertIsNone(item['cost_before_discount_usd'])
        self.assertEqual(item['hermes_estimated_cost_usd'], 1.25)


class HermesIntegrationTests(HermesScanTests):
    def test_http_filter_and_session_drilldown(self):
        db = self.database()
        db.scan()
        server = create_server(DashboardService(db), '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:%d' % server.server_port
            def read(route):
                with urlopen(base + route) as response:
                    import json
                    return json.load(response)
            self.assertEqual(read('/api/dashboard?days=0&source_kind=codex')['summary']['request_count'], 0)
            self.assertEqual(read('/api/dashboard?days=0&source_kind=hermes')['summary']['request_count'], 7)
            self.assertEqual(read('/api/requests?days=0&source_kind=hermes&thread_key=session-a')['request_count'], 7)
            self.assertEqual(read('/api/tasks?days=0&source_kind=codex')['data'], [])
            self.assertEqual(read('/api/sessions?days=0&source_kind=codex')['total'], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cli_json_csv_and_desktop_parameter(self):
        import csv
        import io
        import json
        import inspect
        from desktop import DesktopRuntime
        for format in ('json', 'csv'):
            result = subprocess.run([sys.executable, 'backend.py', 'export', '--db', str(self.home / 'export.db'),
                '--root', str(self.home / 'empty'), '--hermes-db', str(self.source), '--source-kind', 'hermes',
                '--days', '0', '--format', format], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)['daily_model_usage'] if format == 'json' else list(csv.DictReader(io.StringIO(result.stdout)))
            self.assertEqual(int(rows[0]['request_count']), 7)
            self.assertEqual(rows[0]['source_kind'], 'hermes')
        self.assertIn('hermes_db', inspect.signature(DesktopRuntime).parameters)
