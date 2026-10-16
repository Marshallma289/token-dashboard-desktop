"""Browser-level regressions for automatic dashboard catch-up polling."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AutoRefreshFrontendTests(unittest.TestCase):
    def test_auto_refresh_lifecycle_and_polling_safety(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the browser automatic-refresh harness")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "frontend_auto_refresh_harness.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("Automatic refresh regressions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
