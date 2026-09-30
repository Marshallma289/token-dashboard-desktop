import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopBridge


class PreferencesTests(unittest.TestCase):
    def test_preferences_survive_bridge_restart_and_ignore_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch('desktop.local_data_dir', return_value=Path(tmp)):
            first = DesktopBridge()
            self.assertEqual(first.load_preferences(), {})
            self.assertTrue(first.save_preferences({'theme': 'dark', 'unexpected': 'ignored'})['ok'])
            second = DesktopBridge()
            self.assertEqual(second.load_preferences(), {'theme': 'dark'})
            self.assertFalse(second.save_preferences({'theme': 'invalid'})['ok'])
            self.assertEqual(second.load_preferences(), {'theme': 'dark'})
            (Path(tmp) / 'preferences.json').write_text('broken', encoding='utf-8')
            self.assertEqual(second.load_preferences(), {})
