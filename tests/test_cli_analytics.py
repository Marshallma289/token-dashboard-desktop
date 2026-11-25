import contextlib
import csv
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest

from backend import DashboardDB, main


class CLIAnalyticsTests(unittest.TestCase):
    def test_custom_range_model_filter_and_discount_export(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sessions = folder / 'sessions'
            sessions.mkdir()
            path = folder / 'usage.sqlite3'
            rows = []
            for day in (10, 11, 12):
                rows.append({'timestamp': f'2026-10-{day}T08:00:00Z', 'type': 'token_usage_record',
                    'payload': {'thread_id': 'cli-test', 'response_id': f'r{day}',
                        'model_provider_id': 'demo', 'model': 'cli-model', 'cwd': '/cli',
                        'usage': {'input_tokens': 1000, 'cached_input_tokens': 900,
                                  'output_tokens': 100, 'total_tokens': 1100}}})
            (sessions / 'example.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
            db = DashboardDB(path, roots=[sessions], timezone_name='UTC')
            try:
                db.set_pricing_rules([{'id': 'cli-rule', 'provider': 'demo', 'model': 'cli-model',
                    'effective_at': '2020-01-01T00:00:00Z', 'input': 2, 'cached_input': .2,
                    'cache_write': 2.5, 'output': 10, 'multiplier': .8, 'source': 'Test fixture', 'note': ''}])
                db.scan()
            finally:
                db.close()
            base = ['--db', str(path), '--root', str(sessions), '--timezone', 'UTC',
                    '--model', 'cli-model', '--provider', 'demo', '--start-date', '2026-10-11', '--end-date', '2026-10-11']
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(['summary', *base, '--compare']), 0)
            summary = json.loads(stream.getvalue())
            self.assertEqual(summary['summary']['request_count'], 1)
            self.assertEqual(summary['comparison']['summary']['request_count'], 1)
            amounts = {}
            for mode in ('before', 'after'):
                exported = folder / f'{mode}.csv'
                self.assertEqual(main(['export', *base, '--format', 'csv', '--cost-mode', mode,
                                       '--output', str(exported)]), 0)
                result = list(csv.DictReader(io.StringIO(exported.read_text(encoding='utf-8'))))
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['cost_display_mode'], mode)
                self.assertEqual(result[0]['date'], '2026-10-11')
                amounts[mode] = float(result[0]['displayed_cost_usd'])
                self.assertEqual(result[0]['displayed_cost_usd'], result[0]['estimated_cost_usd'])
            self.assertGreater(amounts['before'], 0)
            self.assertAlmostEqual(amounts['after'], amounts['before'] * .8)


    def test_csv_distinguishes_unpriced_from_real_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sessions = folder / 'sessions'
            sessions.mkdir()
            path = folder / 'usage.sqlite3'
            records = []
            for index, model in enumerate(('mock-no-price', 'mock-zero-price')):
                records.append({'timestamp': '2026-10-10T08:00:00Z', 'type': 'token_usage_record',
                    'payload': {'thread_id': 'cli-price-test', 'response_id': f'price-{index}',
                        'model_provider_id': 'demo', 'model': model,
                        'usage': {'input_tokens': 1000, 'output_tokens': 100, 'total_tokens': 1100}}})
            (sessions / 'example.jsonl').write_text(
                '\n'.join(json.dumps(record) for record in records) + '\n', encoding='utf-8')
            db = DashboardDB(path, roots=[sessions], timezone_name='UTC')
            try:
                db.set_pricing_rules([{'id': 'zero', 'provider': 'demo', 'model': 'mock-zero-price',
                    'effective_at': '2020-01-01T00:00:00Z', 'input': 0, 'cached_input': 0,
                    'cache_write': 0, 'output': 0, 'multiplier': 1, 'source': 'Test fixture', 'note': ''}])
                db.scan()
            finally:
                db.close()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['export', '--db', str(path), '--root', str(sessions), '--timezone', 'UTC',
                    '--days', '0', '--format', 'csv', '--cost-mode', 'before']), 0)
            data = {row['model']: row for row in csv.DictReader(io.StringIO(output.getvalue()))}
            self.assertEqual(data['mock-no-price']['pricing_status'], 'unpriced')
            self.assertEqual(data['mock-zero-price']['pricing_status'], 'priced')
            for key in ('displayed_cost_usd', 'estimated_cost_usd', 'cost_before_discount_usd',
                    'cost_after_discount_usd', 'cache_savings_before_discount_usd', 'cache_savings_after_discount_usd'):
                self.assertEqual(data['mock-no-price'][key], '')
                self.assertEqual(float(data['mock-zero-price'][key]), 0)


if __name__ == '__main__':
    unittest.main()
