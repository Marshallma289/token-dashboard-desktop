import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopBridge, DesktopRuntime
import desktop
from backend import DashboardDB


class LayoutPreferencesTests(unittest.TestCase):
    def test_dependency_probe_writes_report_before_creating_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'probe.json'
            report = {'status': 'passed', 'frozen': True, 'renderer': 'edgechromium', 'gui_tested': False}
            with patch.object(sys, 'argv', ['desktop.py', '--probe-desktop-dependencies', '--probe-report', str(path)]), \
                    patch.object(desktop, 'probe_desktop_dependencies', return_value=report), \
                    patch.object(desktop, 'DesktopRuntime', side_effect=AssertionError('probe touched runtime')):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), report)

    def test_layout_survives_new_bridge_and_preserves_theme_and_data_path(self):
        with tempfile.TemporaryDirectory() as temporary, patch('desktop.local_data_dir', return_value=Path(temporary)):
            path = Path(temporary) / 'preferences.json'
            path.write_text(json.dumps({'database_path': 'F:/history/usage.sqlite3'}), encoding='utf-8')
            first = DesktopBridge()
            self.assertIsNone(first.load_layout()['layout'])
            self.assertTrue(first.save_preferences({'theme': 'dark'})['ok'])
            layout = {'version': 1, 'view': 'usage', 'usage': 'all', 'pricing': 'rules',
                      'pages': {'usage': [{'id': 'usage-requests', 'span': 9, 'height': 600}]}}
            self.assertTrue(first.save_layout(layout)['ok'])
            second = DesktopBridge()
            self.assertEqual(second.load_layout()['layout']['pages']['usage'], layout['pages']['usage'])
            self.assertEqual(second.load_layout()['layout']['view'], 'usage')
            self.assertEqual(second.load_preferences(), {'theme': 'dark'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['database_path'], 'F:/history/usage.sqlite3')
            self.assertTrue((Path(temporary) / 'preferences.json.bak').is_file())

    def test_invalid_layout_does_not_overwrite_saved_layout(self):
        with tempfile.TemporaryDirectory() as temporary, patch('desktop.local_data_dir', return_value=Path(temporary)):
            bridge = DesktopBridge()
            valid = {'version': 1, 'pages': {'overview': [{'id': 'overview-kpis', 'span': 12, 'height': 0}]}}
            self.assertTrue(bridge.save_layout(valid)['ok'])
            saved = bridge.load_layout()
            for invalid in (None, {}, {'version': 2, 'pages': {}},
                            {'version': 1, 'pages': {'overview': [{'id': '../preferences', 'span': 12, 'height': 0}]}},
                            {'version': 1, 'pages': {'overview': [{'id': 'overview-kpis', 'span': True, 'height': 0}]}},
                            {'version': 1, 'pages': {'overview': [{'id': 'overview-kpis', 'span': 12, 'height': 99999}]}}):
                self.assertFalse(bridge.save_layout(invalid)['ok'])
                self.assertEqual(bridge.load_layout(), saved)

    def test_runtime_uses_saved_scan_interval_instead_of_overriding_it(self):
        with tempfile.TemporaryDirectory() as temporary, patch('desktop.local_data_dir', return_value=Path(temporary)):
            root = Path(temporary)
            database = DashboardDB(root / 'usage.sqlite3', roots=[root])
            database.set_scan_options(True, 60)
            runtime = DesktopRuntime(database)
            try:
                self.assertEqual(runtime.service.interval, 60)
                self.assertTrue(runtime.service.auto_scan)
            finally:
                runtime.stop()
                database.close()
