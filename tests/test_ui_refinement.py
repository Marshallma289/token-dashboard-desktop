"""Behavioral checks for the new sessions subview preference and layout module."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from desktop import _validated_layout


ROOT = Path(__file__).resolve().parents[1]
LAYOUT_MODULE = ROOT / "web" / "layout.js"


class UIRefinementTests(unittest.TestCase):
    def test_sessions_subview_survives_native_and_frontend_layout_round_trip(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for frontend layout normalization")

        layout = {
            "version": 1,
            "view": "usage",
            "usage": "sessions",
            "pricing": "rules",
            "pages": {
                "overview": [],
                "trends": [],
                "usage": [{"id": "usage-sessions", "span": 12, "height": 0}],
                "pricing": [],
            },
        }

        native_saved = _validated_layout(layout)
        self.assertIsNotNone(native_saved)
        self.assertEqual(native_saved["usage"], "sessions")
        self.assertIn("usage-sessions", [item["id"] for item in native_saved["pages"]["usage"]])

        script = r"""
const assert = require('node:assert/strict');
const { normalizeLayout } = require(process.argv[1]);
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const restored = normalizeLayout(JSON.parse(input));
  assert.equal(restored.usage, 'sessions');
  assert.ok(restored.pages.usage.some(item => item.id === 'usage-sessions'));
  process.stdout.write('sessions layout round trip passed\n');
});
"""
        result = subprocess.run(
            [node, "-e", script, str(LAYOUT_MODULE)],
            input=json.dumps(native_saved),
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("sessions layout round trip passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
