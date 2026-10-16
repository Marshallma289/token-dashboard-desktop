"""Session aggregation and request drill-down API coverage."""

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from backend import DashboardDB, DashboardService, create_server


def event(timestamp, kind, **payload):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def session_meta(thread_id, timestamp, workspace, parent=None):
    payload = {"id": thread_id, "model_provider": "openai", "cwd": workspace}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    return event(timestamp, "session_meta", **payload)


def request(thread_id, response_id, timestamp, model, provider, workspace, input_tokens, output_tokens):
    payload = {
        "response_id": response_id,
        "model": model,
        "model_provider_id": provider,
        "cwd": workspace,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "total_tokens": input_tokens + output_tokens},
    }
    if thread_id is not None:
        payload["thread_id"] = thread_id
    return event(timestamp, "token_usage_record", **payload)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="token-dashboard-sessions-"))
        self.root = self.temp / "sessions"
        self.root.mkdir()
        self.main_thread = "thread-a"
        write_jsonl(self.root / "sessions.jsonl", [
            session_meta("thread-a", "2026-10-16T01:00:00Z", "/workspace/a"),
            request("thread-a", "a-1", "2026-10-16T02:00:00Z", "gpt-6.1-sol",
                    "openai", "/workspace/a", 100, 20),
            request("thread-a", "a-2", "2026-10-16T03:00:00Z", "custom-model",
                    "vendor-a", "/workspace/a", 50, 5),
            session_meta("thread-b", "2026-10-16T04:00:00Z", "/workspace/b", parent="thread-a"),
            request("thread-b", "b-1", "2026-10-16T05:00:00Z", "gpt-6-luna",
                    "openai", "/workspace/b", 30, 10),
        ])
        write_jsonl(self.root / "missing-thread.jsonl", [
            request(None, "no-thread", "2026-10-16T06:00:00Z", "unknown-model",
                    "vendor-a", "/workspace/no-thread", 7, 2),
        ])
        self.db = DashboardDB(self.temp / "usage.sqlite3", roots=[self.root], timezone_name="UTC")
        self.db.scan()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_sessions_aggregate_real_threads_and_report_missing_ids(self):
        result = self.db.sessions(days=0)
        self.assertEqual(set(result), {"data", "total", "omitted_request_count"})
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["omitted_request_count"], 1)
        child, main = result["data"]
        self.assertEqual(child["thread_key"], hashlib.sha256(b"thread-b").hexdigest())
        self.assertEqual(child["agent_role"], "subagent")
        self.assertEqual(child["models"], ["gpt-6-luna"])
        self.assertEqual((child["request_count"], child["input_tokens"],
                          child["output_tokens"], child["total_tokens"]), (1, 30, 10, 40))
        self.assertEqual(child["first_seen"], "2026-10-16T05:00:00+00:00")
        self.assertEqual(child["last_seen"], "2026-10-16T05:00:00+00:00")
        self.assertEqual(main["thread_key"], hashlib.sha256(b"thread-a").hexdigest())
        self.assertEqual(main["agent_role"], "main")
        self.assertEqual(main["models"], ["custom-model", "gpt-6.1-sol"])
        self.assertEqual((main["request_count"], main["input_tokens"],
                          main["output_tokens"], main["total_tokens"]), (2, 150, 25, 175))
        self.assertEqual(main["unpriced_request_count"], 1)
        self.assertGreater(main["cost_before_discount_usd"], 0)
        self.assertGreater(main["cost_after_discount_usd"], 0)
        self.assertEqual(main["workspace"], "/workspace/a")
        self.assertEqual(main["first_seen"], "2026-10-16T02:00:00+00:00")
        self.assertEqual(main["last_seen"], "2026-10-16T03:00:00+00:00")

    def test_session_filters_match_request_filters_and_omit_only_matching_rows(self):
        provider = self.db.sessions(days=0, provider="vendor-a")
        self.assertEqual(provider["total"], 1)
        self.assertEqual(provider["omitted_request_count"], 1)
        self.assertEqual(provider["data"][0]["models"], ["custom-model"])
        workspace = self.db.sessions(days=0, workspace="/workspace/a")
        self.assertEqual(workspace["data"][0]["request_count"], 2)
        model = self.db.sessions(days=0, model="gpt-6.1-sol")
        self.assertEqual(model["total"], 1)
        self.assertEqual(model["data"][0]["models"], ["gpt-6.1-sol"])
        day = self.db.sessions(days=1, start_date="2026-10-16", end_date="2026-10-16")
        self.assertEqual(day["total"], 2)
        outside = self.db.sessions(days=0, start_date="2026-10-17", end_date="2026-10-17")
        self.assertEqual((outside["total"], outside["omitted_request_count"]), (0, 0))

    def test_deidentified_thread_key_filters_requests_and_raw_ids_are_rejected(self):
        key = hashlib.sha256(self.main_thread.encode("utf-8")).hexdigest()
        result = self.db.requests(days=0, thread_key=key)
        self.assertEqual(result["total"], 2)
        self.assertTrue(all(row["thread_key"] == key for row in result["data"]))
        with self.assertRaises(ValueError):
            self.db.requests(days=0, thread_key=self.main_thread)
        with self.assertRaises(ValueError):
            self.db.requests(days=0, thread_key="0" * 63 + "'")

    def test_sessions_request_filter_and_dashboard_css_routes(self):
        service = DashboardService(self.db, interval=60)
        server = create_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with urlopen(base + "/api/sessions?days=0", timeout=3) as response:
                sessions = json.loads(response.read().decode("utf-8"))
            self.assertEqual(sessions["total"], 2)
            self.assertEqual(sessions["omitted_request_count"], 1)
            key = sessions["data"][1]["thread_key"]
            with urlopen(base + "/api/requests?" + urlencode({"days": 0, "thread_key": key}),
                         timeout=3) as response:
                requests = json.loads(response.read().decode("utf-8"))
            self.assertEqual(requests["total"], 2)
            self.assertTrue(all(row["thread_key"] == key for row in requests["data"]))
            try:
                urlopen(base + "/api/requests?" + urlencode({"days": 0, "thread_key": "thread-a"}),
                        timeout=3)
            except HTTPError as error:
                self.assertEqual(error.code, 400)
            else:
                self.fail("raw thread ids must not be accepted as request filters")
            with urlopen(base + "/dashboard.css", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/css", response.headers.get("Content-Type", ""))
        finally:
            server.shutdown()
            server.server_close()
            service.stop()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
