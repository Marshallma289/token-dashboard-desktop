from pathlib import Path
import shutil
import subprocess
import unittest


class HermesFrontendTests(unittest.TestCase):
    def test_cross_day_usage_is_not_presented_as_complete_today(self):
        result = subprocess.run([shutil.which('node') or 'node', 'tests/frontend_hermes_harness.js', '--time'],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_priced_hermes_card_and_reference_price_notice(self):
        result = subprocess.run([shutil.which('node') or 'node', 'tests/frontend_hermes_harness.js', '--pricing'],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_aggregate_rows_show_calls_cost_provenance_and_unavailable_metrics(self):
        result = subprocess.run([shutil.which('node') or 'node', 'tests/frontend_hermes_harness.js', '--semantics'],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_filter_propagates_to_dashboard_and_details(self):
        result = subprocess.run([shutil.which('node') or 'node', 'tests/frontend_hermes_harness.js'],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
