"""Regression tests for keeping chart resize separate from dashboard refresh."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendResizeTests(unittest.TestCase):
    def test_resize_does_not_redraw_or_refetch_unrelated_views(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the frontend resize harness")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "frontend_resize_harness.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("Frontend resize regressions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
