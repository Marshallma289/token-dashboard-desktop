import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RequestFrontendTests(unittest.TestCase):
    def test_request_estimates_and_refresh_races(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the browser request regression harness")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "frontend_request_refresh_harness.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("regressions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
