"""Hermes Flash aliases, disjoint usage buckets, and provider price overrides."""
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import DashboardDB
from pricing import PricingCatalog, canonical_model
from test_hermes import make_hermes


class HermesFlashPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'state.db'
        make_hermes(self.source, model='deepseek-v4.1-flash',
                    input_tokens=1000, cache_read_tokens=20000,
                    cache_write_tokens=50, output_tokens=100,
                    last_seen=1792645200.0)  # 2026-10-22 05:00 UTC, off-peak

    def database(self, name='usage.db'):
        db = DashboardDB(self.root / name, roots=[self.root / 'empty'],
                         hermes_db=self.source)
        self.addCleanup(db.close)
        return db

    def update_source(self, **values):
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute('UPDATE session_model_usage SET ' +
                               ','.join(name + '=?' for name in values),
                               tuple(values.values()))

    def test_aliases_resolve_to_flash_reference(self):
        for model in ('deepseek-v4.1-flash', 'deepseek/deepseek-v4.1-flash'):
            with self.subTest(model=model):
                self.assertEqual(canonical_model(model), 'deepseek-flash')
                quote = PricingCatalog().quote(model, provider='custom',
                    timestamp='2026-10-22T05:00:00Z', input_tokens=1000000)
                self.assertTrue(quote.is_priced)
                self.assertEqual(quote.estimated_cost_usd, 0.15)

    def test_hermes_alias_has_full_pricing_coverage_and_nonzero_cost(self):
        db = self.database()
        db.scan()
        summary = db.dashboard(0, source_kind='hermes')['summary']
        self.assertGreater(summary['pricing_coverage'], 0)
        self.assertEqual(summary['pricing_coverage'], 1)
        self.assertGreater(summary['estimated_cost_usd'], 0)

    def test_disjoint_input_cache_read_and_cache_write_are_each_billed_once(self):
        # Use an existing alias so this catches the input bug independently.
        self.update_source(model='deepseek-flash')
        db = self.database()
        db.scan()
        row = db.requests(days=0, source_kind='hermes')['data'][0]
        # (1000*.15 + 20000*.003 + 50*.15 + 100*.60) / 1M
        self.assertAlmostEqual(row['cost_after_discount_usd'], 0.0002775, places=12)
        self.assertEqual(row['cost_breakdown_before_discount'], {
            'input': 0.00015, 'cached_input': 0.00006,
            'cache_write': 0.0000075, 'output': 0.00006,
        })
        self.assertEqual(row['input_tokens'], 1000)
        self.assertEqual(row['cached_input_tokens'], 20000)
        self.assertEqual(row['cache_write_input_tokens'], 50)

    def test_peak_and_off_peak_use_correct_disjoint_counts(self):
        db = self.database()
        for timestamp, expected in (('2026-10-22T02:00:00Z', 0.000555),
                                    ('2026-10-22T05:00:00Z', 0.0002775)):
            with self.subTest(timestamp=timestamp):
                quote = db._quote_for_usage('deepseek-v4.1-flash', 'custom',
                    timestamp, 1000, 20000, 50, 100, aggregate=True)
                self.assertEqual(quote['pricing_status'], 'priced')
                self.assertAlmostEqual(quote['cost_after_discount_usd'], expected, places=12)

    def test_custom_provider_rule_overrides_reference_and_aliases(self):
        db = self.database()
        db.set_pricing_rules([{
            'id': 'relay-flash', 'provider': 'custom', 'model': 'deepseek-v4.1-flash',
            'effective_at': '2026-01-01T00:00:00Z', 'input': 1, 'cached_input': 0.1,
            'cache_write': 2, 'output': 3, 'multiplier': 0.5,
        }])
        db.scan()
        row = db.requests(days=0, source_kind='hermes')['data'][0]
        self.assertEqual(row['rate_snapshot']['id'], 'relay-flash')
        self.assertEqual(row['rate_snapshot']['rate_band'], 'custom')
        self.assertAlmostEqual(row['cost_before_discount_usd'], 0.0034, places=12)
        self.assertAlmostEqual(row['cost_after_discount_usd'], 0.0017, places=12)
        quote = db._quote_for_usage('deepseek/deepseek-v4.1-flash', 'custom',
            '2026-10-22T05:00:00Z', 1000, 20000, 50, 100, aggregate=True)
        self.assertEqual(quote['rate_snapshot']['id'], 'relay-flash')
        self.assertAlmostEqual(quote['cost_after_discount_usd'], 0.0017, places=12)
        other = db._quote_for_usage('deepseek-v4.1-flash', 'other',
            '2026-10-22T05:00:00Z', 1000, 20000, 50, 100, aggregate=True)
        self.assertEqual(other['rate_snapshot']['rate_band'], 'off_peak')
        self.assertAlmostEqual(other['cost_after_discount_usd'], 0.0002775, places=12)

    def test_frozen_snapshot_update_and_reprice_preserve_input_semantics(self):
        db = self.database()
        db.scan()
        self.update_source(input_tokens=2000, last_seen=1792645201.0)
        db.scan()
        row = db.requests(days=0, source_kind='hermes')['data'][0]
        self.assertEqual(row['pricing_status'], 'priced')
        self.assertAlmostEqual(row['cost_after_discount_usd'], 0.0004275, places=12)
        preview = db.reprice_preview(days=0, source_kind='hermes')
        self.assertAlmostEqual(preview['cost_after_discount_usd'], 0.0004275, places=12)
        result = db.reprice_apply(preview['preview_token'])
        self.assertEqual(result['updated_count'], 7)
        row = db.requests(days=0, source_kind='hermes')['data'][0]
        self.assertAlmostEqual(row['cost_after_discount_usd'], 0.0004275, places=12)

    def test_scan_and_reprice_leave_source_and_sidecars_unchanged(self):
        def evidence():
            return {suffix: (p.stat().st_mtime_ns, p.stat().st_size,
                             hashlib.sha256(p.read_bytes()).hexdigest()) if p.exists() else None
                    for suffix in ('', '-wal', '-shm')
                    for p in [Path(str(self.source) + suffix)]}
        before = evidence()
        db = self.database()
        db.scan()
        preview = db.reprice_preview(days=0, source_kind='hermes')
        db.reprice_apply(preview['preview_token'])
        self.assertEqual(before, evidence())


if __name__ == '__main__':
    unittest.main()
