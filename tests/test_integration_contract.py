import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from backend import DashboardDB, DashboardService, create_server


HERE = Path(__file__).resolve().parent


class IntegrationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codex-dashboard-integration-"))
        self.root = self.temp_dir / "sessions"
        self.root.mkdir()
        shutil.copy2(HERE / "fixtures" / "mixed_rollout.jsonl", self.root / "mixed.jsonl")
        self.db = DashboardDB(self.temp_dir / "cache.sqlite3", roots=[self.root], timezone_name="Asia/Shanghai")
        self.service = DashboardService(self.db, interval=0.25)
        self.service.start()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.stop()
        self.thread.join(timeout=3)
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_static_dashboard_and_assets_are_served_locally(self):
        for route, content_type in (
            ("/", "text/html"),
            ("/styles.css", "text/css"),
            ("/app.js", "text/javascript"),
        ):
            with urlopen(self.base + route, timeout=3) as response:
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers.get("Content-Type", ""))
                self.assertTrue(body)

    def test_dashboard_contract_contains_all_five_views(self):
        with urlopen(self.base + "/api/dashboard?days=30", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for key in (
            "workspace_distribution",
            "daily_activity",
            "hourly_activity",
            "request_distribution",
            "daily_model_usage",
            "daily_model_summary",
            "pricing",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["pricing"]["currency"], "USD")
        self.assertIn("estimated_cost_usd", payload["summary"])
        self.assertIn("priced_tokens", payload["summary"])
        self.assertIn("unpriced_tokens", payload["summary"])
        self.assertIn("pricing_coverage", payload["summary"])

    def test_sse_starts_with_a_dashboard_snapshot(self):
        with urlopen(self.base + "/api/events", timeout=3) as response:
            self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
            first_line = response.readline().decode("utf-8").strip()
        self.assertEqual(first_line, "event: dashboard")


if __name__ == "__main__":
    unittest.main()
