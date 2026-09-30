import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from backend import DashboardDB
from desktop import DesktopRuntime, dashboard_csv


class DesktopTests(unittest.TestCase):
    def test_desktop_runtime_serves_polished_dashboard_and_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copy2(Path(__file__).parent / "fixtures" / "mixed_rollout.jsonl", root / "mixed.jsonl")
            db = DashboardDB(root / "usage.sqlite3", roots=[root])
            db.scan()
            runtime = DesktopRuntime(db, interval=60)
            try:
                url = runtime.start()
                with urlopen(url, timeout=5) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("Codex Token", html)
                self.assertIn("exportButton", html)

                api = url.split("/?", 1)[0] + "/api/dashboard?days=0&model=gpt-5-mini"
                with urlopen(api, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.assertEqual(data["filters"]["model"], "gpt-5-mini")
                self.assertEqual(len(data["daily_model_usage"]), 1)
            finally:
                runtime.stop()
                db.close()

    def test_csv_export_is_excel_friendly_and_escapes_values(self):
        content = dashboard_csv([
            {
                "date": "2026-09-19",
                "model": 'gpt,"special"',
                "provider": "openai",
                "provider_label": "OpenAI 官方",
                "total_tokens": 42,
                "request_count": 1,
            }
        ])
        self.assertTrue(content.startswith("\ufeffdate,model,provider"))
        self.assertIn("estimated_cost_usd", content.splitlines()[0])
        self.assertIn("pricing_status", content.splitlines()[0])
        self.assertIn('"gpt,""special"""', content)
        self.assertIn("OpenAI 官方", content)


if __name__ == "__main__":
    unittest.main()
