import builtins
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop

APP_VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()


class SelfTestTests(unittest.TestCase):
    def _deny_webview_import(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "webview":
                raise AssertionError("self-test must not import the GUI")
            return original_import(name, *args, **kwargs)

        return patch("builtins.__import__", side_effect=guarded_import)

    def _passed_report(self):
        return {
            "status": "passed",
            "app_version": APP_VERSION,
            "frozen": False,
            "gui_tested": False,
            "speed_sample_count": 2,
            "output_tokens_per_second": 33.3333,
            "checks": ["synthetic_data"],
        }

    def test_run_self_test_uses_synthetic_data_without_preferences_or_gui(self):
        with patch.object(desktop, "local_data_dir", side_effect=AssertionError("personal data path accessed")), \
                patch.object(desktop, "_read_preferences", side_effect=AssertionError("preferences accessed")), \
                self._deny_webview_import():
            report = desktop.run_self_test()

        self.assertEqual(report["status"], "passed")
        self.assertIsInstance(report["frozen"], bool)
        self.assertFalse(report["gui_tested"])
        self.assertEqual(report["app_version"], APP_VERSION)
        self.assertEqual(report["speed_sample_count"], 2)
        self.assertAlmostEqual(report["output_tokens_per_second"], 33.3333, places=4)
        self.assertTrue(report["checks"])
        self.assertTrue(all(isinstance(check, str) and check for check in report["checks"]))

    def test_main_self_test_writes_success_report_without_importing_gui(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path = Path(folder) / "self-test.json"
            argv = ["desktop.py", "--self-test", "--self-test-report", str(report_path)]
            with patch.object(sys, "argv", argv), patch.object(desktop, "run_self_test", return_value=self._passed_report()), \
                    self._deny_webview_import():
                result = desktop.main()

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), self._passed_report())

    def test_main_self_test_writes_failed_report_and_returns_one(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path = Path(folder) / "self-test.json"
            argv = ["desktop.py", "--self-test", "--self-test-report", str(report_path)]
            with patch.object(sys, "argv", argv), patch.object(desktop, "run_self_test", side_effect=RuntimeError("synthetic failure")), \
                    self._deny_webview_import():
                result = desktop.main()

            self.assertEqual(result, 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["gui_tested"])
            self.assertIn("synthetic failure", report["error"])


if __name__ == "__main__":
    unittest.main()
