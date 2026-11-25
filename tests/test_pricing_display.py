"""Behavioral pricing display and desktop preference regressions."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopBridge


ROOT = Path(__file__).resolve().parents[1]


class PricingDisplayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for frontend behavior")
    def test_frontend_displays_incomplete_cost_and_preserves_mode(self):
        for scenario in ("coverage", "native"):
            with self.subTest(scenario=scenario):
                result = subprocess.run(
                    ["node", str(ROOT / "tests/frontend_pricing_display_harness.js"), scenario],
                    cwd=ROOT, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_cost_preference_survives_bridge_recreation(self):
        with tempfile.TemporaryDirectory() as tmp, patch("desktop.local_data_dir", return_value=Path(tmp)):
            first = DesktopBridge()
            self.assertTrue(first.save_preferences({"theme": "dark"})["ok"])
            self.assertTrue(first.save_preferences({"cost_mode": "before", "other": "ignored"})["ok"])
            second = DesktopBridge()
            expected = {"theme": "dark", "cost_mode": "before"}
            self.assertEqual(second.load_preferences(), expected)
            self.assertFalse(second.save_preferences({"theme": "light", "cost_mode": "USD"})["ok"])
            self.assertEqual(second.load_preferences(), expected)
