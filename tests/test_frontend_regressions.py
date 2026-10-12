import unittest
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


class FrontendRegressionTests(unittest.TestCase):
    def test_hidden_status_panels_cannot_be_forced_visible(self) -> None:
        styles = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".error-banner[hidden], .empty-state[hidden]", styles)
        self.assertIn("display: none !important", styles)

    def test_cache_rate_uses_available_input_and_one_decimal_display(self) -> None:
        module = ROOT / "web" / "cache-metrics.js"
        script = r"""
const assert = require('node:assert/strict');
const metrics = require(process.argv[1]);
const rate = metrics.calculateRate({ cachedTokens: 91500, cacheReadInputTokens: 100000, denominatorProvided: true });
assert.equal(rate, 0.915);
assert.equal(metrics.formatPercent(rate, true), '91.5%');
assert.equal(metrics.formatPercent(0.9, true), '90.0%');
assert.equal(metrics.calculateRate({ cachedTokens: 91500, inputTokens: 200000, availableCount: 2, requestCount: 3 }), null);
assert.equal(metrics.formatPercent(null, false), '—');
"""
        result = subprocess.run(
            ["node", "-e", script, str(module)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hourly_heatmap_lists_the_newest_dates_first(self) -> None:
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        heatmap = script.split("function renderHeatmap(data) {", 1)[1].split(
            "function renderActivity", 1
        )[0]
        self.assertIn("b[0].localeCompare(a[0])", heatmap)
        self.assertIn(".slice(0, 45)", heatmap)
        self.assertNotIn(".slice(-45)", heatmap)


if __name__ == "__main__":
    unittest.main()
