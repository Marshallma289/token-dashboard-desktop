import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from backend import DashboardDB, DashboardService, create_server


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codex-dashboard-test-"))
        self.root = self.temp_dir / "sessions"
        self.root.mkdir()
        shutil.copy2(FIXTURES / "mixed_rollout.jsonl", self.root / "mixed.jsonl")
        shutil.copy2(FIXTURES / "legacy_rollout.jsonl", self.root / "legacy.jsonl")
        self.providers = self.temp_dir / "providers.json"
        self.providers.write_text(json.dumps({"providers": {"moonshot": "Moonshot AI"}}), encoding="utf-8")
        self.db_path = self.temp_dir / "cache.sqlite3"
        self.db = DashboardDB(self.db_path, roots=[self.root], providers_path=self.providers)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_provider_switch_model_switch_dedup_and_legacy(self):
        result = self.db.scan()
        self.assertEqual(result.files_scanned, 2)
        # mixed has two modern records (the old token_count is ignored and
        # the duplicate response id is ignored), legacy contributes one.
        self.assertEqual(self.db.counts()["usage_records"], 3)
        data = self.db.dashboard(days=30)
        self.assertEqual(data["summary"]["request_count"], 3)
        self.assertEqual(data["summary"]["total_tokens"], 495)
        providers = {item["provider"]: item["provider_label"] for item in data["providers"]}
        self.assertEqual(providers["openai"], "OpenAI 官方")
        self.assertEqual(providers["moonshot"], "Moonshot AI")
        models = {(item["provider"], item["model"]) for item in data["daily_model_distribution"]}
        self.assertIn(("openai", "gpt-5"), models)
        self.assertIn(("moonshot", "gpt-5-mini"), models)
        self.assertIn(("custom", "legacy-model"), models)
        workspaces = {item["workspace"] for item in data["workspace_distribution"]}
        self.assertEqual(workspaces, {"/workspace/alpha", "/workspace/beta", "/workspace/legacy"})

    def test_filter_options_survive_selection_and_empty_combinations(self):
        self.db.scan()
        all_data = self.db.dashboard(days=0)
        for kwargs in ({'provider': 'moonshot'}, {'workspace': '/workspace/alpha'},
                       {'model': 'gpt-5'}, {'provider': 'moonshot', 'model': 'legacy-model'}):
            selected = self.db.dashboard(days=0, **kwargs)
            self.assertEqual(selected['providers'], all_data['providers'])
            self.assertEqual(selected['workspaces'], all_data['workspaces'])
            self.assertEqual(selected['filters']['models'], all_data['filters']['models'])
        empty = self.db.dashboard(days=0, provider='moonshot', model='legacy-model')
        self.assertEqual(empty['summary']['request_count'], 0)
        selected = self.db.dashboard(days=0, model='gpt-5-mini')
        self.assertEqual(selected['filters']['model'], 'gpt-5-mini')
        self.assertTrue(selected['daily_model_usage'])
        self.assertTrue(all(row['model'] == 'gpt-5-mini' for row in selected['daily_model_usage']))
        self.assertEqual(selected['summary']['total_tokens'], sum(row['total_tokens'] for row in selected['daily_model_usage']))
        self.assertEqual(selected['summary']['request_count'], sum(row['count'] for row in selected['request_token_histogram']))

    def test_five_aggregates_and_percentiles(self):
        self.db.scan()
        data = self.db.dashboard(days=30)
        self.assertTrue(data["workspace_distribution"])
        self.assertTrue(data["daily_active"])
        distribution = data["request_token_distribution"]
        self.assertEqual(distribution["request_count"], 3)
        self.assertEqual(distribution["p50"], 140)
        self.assertEqual(distribution["p90"], 252)
        self.assertEqual(distribution["p99"], 277)
        self.assertEqual(sum(bucket["count"] for bucket in distribution["histogram"]), 3)
        self.assertTrue(data["daily_model_distribution"])
        self.assertTrue(data["daily_model_usage"])
        moonshot = next(item for item in data["daily_model_usage"] if item["provider"] == "moonshot")
        self.assertEqual(moonshot["input_tokens"], 200)
        self.assertEqual(moonshot["output_tokens"], 80)
        self.assertEqual(moonshot["reasoning_output_tokens"], 30)
        self.assertEqual(moonshot["total_tokens"], 280)

    def test_incremental_file_change_rebuilds_only_changed_file(self):
        first = self.db.scan()
        self.assertEqual(first.files_scanned, 2)
        self.assertEqual(self.db.counts()["usage_records"], 3)
        target = self.root / "legacy.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                '\n{"timestamp":"2026-09-19T10:00:00+08:00","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}}'
            )
        second = self.db.scan()
        self.assertEqual(second.files_scanned, 1)
        self.assertEqual(second.files_unchanged, 1)
        self.assertEqual(self.db.counts()["usage_records"], 4)
        self.assertEqual(self.db.dashboard(days=30)["summary"]["total_tokens"], 510)

    def test_health_endpoint(self):
        self.db.scan()
        service = DashboardService(self.db, interval=0.25)
        service.start()
        server = create_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen("http://127.0.0.1:%d/api/health" % server.server_port, timeout=3) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["usage_records"], 3)
        finally:
            server.shutdown()
            server.server_close()
            service.stop()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
