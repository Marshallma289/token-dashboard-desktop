import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendRegressionTests(unittest.TestCase):
    def test_hidden_status_panels_cannot_be_forced_visible(self) -> None:
        styles = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".error-banner[hidden], .empty-state[hidden]", styles)
        self.assertIn("display: none !important", styles)

    def test_cache_metric_is_returned_to_kpi_renderer(self) -> None:
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("models, cache, cacheRate", script)

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
