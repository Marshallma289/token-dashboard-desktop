"""Hermes cache buckets are independent input, not subsets of fresh input."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB
from hermes_source import HermesSource
from test_hermes import make_hermes


class HermesTokenTotalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'state.db'
        make_hermes(self.source, input_tokens=1000, output_tokens=200,
                    cache_read_tokens=20000, cache_write_tokens=50,
                    reasoning_tokens=80, first_seen=1792686300.5)
        self.history = self.root / 'usage.db'

    def database(self):
        return DashboardDB(self.history, roots=[self.root / 'empty'], hermes_db=self.source)

    def test_source_includes_cache_once_and_reasoning_stays_output_subset(self):
        rows, _ = HermesSource(self.source).read()
        self.assertEqual(rows[0]['total_tokens'], 21250)
        self.assertEqual(rows[0]['input_tokens'], 1000)

    def test_totals_flow_through_all_views_without_multiplying_by_calls(self):
        with closing(self.database()) as db:
            db.scan()
            data = db.dashboard(0, source_kind='hermes')
            self.assertEqual(data['summary']['total_tokens'], 21250)
            self.assertEqual(data['summary']['fresh_input_tokens'], 1000)
            self.assertEqual(data['summary']['cache_read_input_tokens'], 21050)
            self.assertAlmostEqual(data['summary']['cache_hit_rate'], 20000 / 21050)
            for name in ('daily_model_usage', 'daily_activity', 'workspace_distribution'):
                self.assertEqual(sum(row['total_tokens'] for row in data[name]), 21250, name)
            self.assertEqual(db.sessions(days=0)['data'][0]['total_tokens'], 21250)
            self.assertEqual(db.tasks(days=0)['data'][0]['summary']['total_tokens'], 21250)
            row = db.requests(days=0)['data'][0]
            self.assertEqual(row['total_tokens'], 21250)
            self.assertEqual(row['fresh_input_tokens'], 1000)
            self.assertEqual(row['cache_read_input_tokens'], 21050)
            self.assertAlmostEqual(row['cache_hit_rate'], 20000 / 21050)
            self.assertEqual(data['summary']['request_count'], 7)

    def test_old_history_repaired_even_without_source_and_preserves_prices(self):
        with closing(self.database()) as db:
            db.scan()
            before = db.requests(days=0)['data'][0]
        with closing(sqlite3.connect(self.history)) as conn:
            conn.execute("UPDATE history_snapshots SET total_tokens=input_tokens+output_tokens, fingerprint=randomblob(32) WHERE source_kind='hermes'")
            conn.commit()
        self.source.unlink()
        for _ in range(2):
            with closing(self.database()) as db:
                row = db.requests(days=0)['data'][0]
                self.assertEqual(row['total_tokens'], 21250)
                self.assertEqual(row['request_id'], before['request_id'])
                self.assertEqual(row['cost_after_discount_usd'], before['cost_after_discount_usd'])
                self.assertEqual(db.counts()['usage_records'], 1)
                self.assertEqual(db._connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unchanged_source_after_upgrade_does_not_leave_stale_totals(self):
        with closing(self.database()) as db:
            db.scan()
        with closing(sqlite3.connect(self.history)) as conn:
            conn.execute("UPDATE history_snapshots SET total_tokens=input_tokens+output_tokens, fingerprint=randomblob(32) WHERE source_kind='hermes'")
            conn.commit()
        with closing(self.database()) as db:
            db.scan()
            self.assertEqual(db.dashboard(0)['summary']['total_tokens'], 21250)
            self.assertEqual(db.counts()['usage_records'], 1)

    def test_codex_input_already_contains_cache_and_is_not_added_again(self):
        root = self.root / 'empty'
        root.mkdir()
        (root / 'one.jsonl').write_text('{"timestamp":"2026-10-22T12:00:00Z","type":"token_usage_record","payload":{"response_id":"one","usage":{"input_tokens":1000,"output_tokens":200,"cached_input_tokens":800,"total_tokens":1200}}}', encoding='utf-8')
        with closing(self.database()) as db:
            db.scan()
            self.assertEqual(db.dashboard(0, source_kind='codex')['summary']['total_tokens'], 1200)
            self.assertEqual(db.dashboard(0)['summary']['total_tokens'], 22450)

    def test_repair_keeps_reported_cost_when_model_becomes_priced(self):
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("UPDATE session_model_usage SET model='unknown-hermes-model', actual_cost_usd=1.25,cost_status='actual',cost_source='provider_cost_api'")
            conn.commit()
        with closing(self.database()) as db:
            db.scan()
            before = db.requests(days=0)['data'][0]
            self.assertEqual(before['cost_after_discount_usd'], 1.25)
        with closing(sqlite3.connect(self.history)) as conn:
            # Model unknown to the old importer is recognized by today's catalog.
            conn.execute("UPDATE history_contexts SET model='gpt-6.1-sol'")
            conn.execute("UPDATE history_snapshots SET total_tokens=input_tokens+output_tokens, fingerprint=randomblob(32) WHERE source_kind='hermes'")
            conn.commit()
        self.source.unlink()
        with closing(self.database()) as db:
            after = db.requests(days=0)['data'][0]
            self.assertEqual(after['total_tokens'], 21250)
            for key in ('cost_after_discount_usd', 'cost_before_discount_usd', 'cost_multiplier',
                        'rate_snapshot', 'pricing_status', 'cost_breakdown_before_discount'):
                self.assertEqual(after[key], before[key], key)
