"""Acceptance coverage for the analytics and pricing features added in v2."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPResponse
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend import DashboardDB, DashboardService, create_server
from pricing import PricingCatalog


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def session_meta(
    thread_id: str,
    provider: str = "vendor-a",
    workspace: str = "/workspace/alpha",
    timestamp: str = "2026-10-12T08:00:00Z",
    *,
    parent_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": thread_id, "model_provider": provider, "cwd": workspace}
    if parent_thread_id:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent_thread_id}}}
    return {"timestamp": timestamp, "type": "session_meta", "payload": payload}


def usage_record(
    *,
    thread_id: str,
    response_id: str,
    timestamp: str,
    model: str = "custom-model",
    provider: str = "vendor-a",
    workspace: str = "/workspace/alpha",
    input_tokens: int = 100,
    output_tokens: int = 0,
    cached_input_tokens: Optional[int] = None,
    cache_write_input_tokens: Optional[int] = None,
    ttft_ms: Optional[float] = None,
    turn_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_input_tokens is not None:
        usage["cached_input_tokens"] = cached_input_tokens
    if cache_write_input_tokens is not None:
        usage["cache_write_input_tokens"] = cache_write_input_tokens
    payload: dict[str, Any] = {
        "thread_id": thread_id,
        "response_id": response_id,
        "model": model,
        "model_provider_id": provider,
        "cwd": workspace,
        "usage": usage,
    }
    if turn_id:
        payload["turn_id"] = turn_id
    if session_id:
        payload["session_id"] = session_id
    if ttft_ms is not None:
        payload["time_to_first_token_ms"] = ttft_ms
    return {"timestamp": timestamp, "type": "token_usage_record", "payload": payload}


def pricing_rule(
    *,
    rule_id: str,
    provider: str,
    model: str,
    effective_at: str,
    input_rate: float,
    cached_rate: float = 0.0,
    cache_write_rate: float = 0.0,
    output_rate: float = 0.0,
    multiplier: float = 1.0,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "provider": provider,
        "model": model,
        "effective_at": effective_at,
        "input": input_rate,
        "cached_input": cached_rate,
        "cache_write": cache_write_rate,
        "output": output_rate,
        "multiplier": multiplier,
        "source": "acceptance fixture",
        "note": "synthetic test price",
    }


class FeatureAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="token-dashboard-acceptance-")
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.db_path = self.root / "history.sqlite3"
        self.db = DashboardDB(
            self.db_path,
            roots=[self.sessions],
            timezone_name="UTC",
            clock=lambda: NOW,
        )
        self.service: Optional[DashboardService] = None
        self.server = None
        self.server_thread: Optional[threading.Thread] = None
        self.base: Optional[str] = None

    def tearDown(self) -> None:
        self.stop_server()
        if getattr(self, "db", None) is not None:
            self.db.close()
        self.temp.cleanup()

    def start_server(self, interval: float = 3600.0) -> None:
        if self.server is not None:
            return
        self.service = DashboardService(self.db, interval=interval)
        self.service.start()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.service is not None:
            self.service.stop()
            self.service = None
        if self.server_thread is not None:
            self.server_thread.join(timeout=3)
            self.server_thread = None
        self.base = None

    def json_request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        raw: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> tuple[int, Any]:
        if not self.base:
            raise RuntimeError("dashboard server is not running")
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if method.upper() != "GET":
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("X-Dashboard-Request", "1")
            if self.base:
                request_headers.setdefault("Origin", self.base)
        request = Request(self.base + path, data=body, method=method.upper(), headers=request_headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, self._read_json(response)
        except HTTPError as error:
            return error.code, self._read_json(error)

    @staticmethod
    def _read_json(response: HTTPResponse) -> Any:
        content = response.read()
        if not content:
            return None
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return content.decode("utf-8", errors="replace")

    def get_json(self, path: str) -> Any:
        status, body = self.json_request("GET", path)
        self.assertEqual(status, 200, body)
        return body

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        status, body = self.json_request("POST", path, payload)
        self.assertEqual(status, 200, body)
        return body

    def save_pricing_rules(self, rules: list[dict[str, Any]]) -> Any:
        return self.post_json("/api/pricing", {"rules": rules})

    def scan_now(self) -> Any:
        return self.post_json("/api/scan/now", {})

    def test_pricing_catalog_prefers_provider_and_latest_effective_rule(self) -> None:
        catalog = PricingCatalog([
            pricing_rule(rule_id="global", provider="*", model="custom-model",
                         effective_at="2026-10-01T00:00:00Z", input_rate=10, multiplier=0.5),
            pricing_rule(rule_id="vendor-old", provider="vendor-a", model="custom-model",
                         effective_at="2026-10-01T00:00:00Z", input_rate=4, multiplier=0.8),
            pricing_rule(rule_id="vendor-new", provider="vendor-a", model="custom-model",
                         effective_at="2026-10-12T00:00:00Z", input_rate=5, multiplier=0.8),
        ])

        current = catalog.quote("custom-model", provider="vendor-a",
                                timestamp="2026-10-12T08:00:00Z", input_tokens=1_000_000)
        before_specific = catalog.quote("custom-model", provider="vendor-a",
                                        timestamp="2026-10-10T08:00:00Z", input_tokens=1_000_000)
        fallback = catalog.quote("custom-model", provider="vendor-b",
                                 timestamp="2026-10-12T08:00:00Z", input_tokens=1_000_000)

        self.assertEqual(current.rate_snapshot["id"], "vendor-new")
        self.assertEqual(current.cost_before_discount_usd, 5.0)
        self.assertAlmostEqual(current.cost_after_discount_usd, 4.0)
        self.assertEqual(before_specific.rate_snapshot["id"], "vendor-old")
        self.assertEqual(before_specific.cost_before_discount_usd, 4.0)
        self.assertEqual(fallback.rate_snapshot["id"], "global")
        self.assertEqual(fallback.cost_before_discount_usd, 10.0)

    def test_history_price_freezes_until_explicit_preview_reprice_and_survives_source_removal(self) -> None:
        self.start_server()
        initial_rule = pricing_rule(
            rule_id="base", provider="vendor-a", model="custom-model",
            effective_at="2026-10-01T00:00:00Z", input_rate=1, multiplier=0.8,
        )
        self.save_pricing_rules([initial_rule])
        self.log = self.sessions / "priced.jsonl"
        write_jsonl(self.log, [
            session_meta("thread-priced"),
            usage_record(thread_id="thread-priced", response_id="response-priced",
                         timestamp="2026-10-12T08:00:00Z", input_tokens=1_000_000),
        ])
        self.scan_now()

        initial = self.get_json("/api/dashboard?days=0")["summary"]
        self.assertEqual(initial["request_count"], 1)
        self.assertEqual(initial["cost_before_discount_usd"], 1.0)
        self.assertAlmostEqual(initial["cost_after_discount_usd"], 0.8)
        self.assertAlmostEqual(initial["estimated_cost_usd"], 0.8)

        replacement_rule = pricing_rule(
            rule_id="replacement", provider="vendor-a", model="custom-model",
            effective_at="2026-10-01T00:00:00Z", input_rate=3, multiplier=0.5,
        )
        self.save_pricing_rules([replacement_rule])
        frozen = self.get_json("/api/dashboard?days=0")["summary"]
        self.assertEqual(frozen["cost_before_discount_usd"], 1.0)
        self.assertAlmostEqual(frozen["cost_after_discount_usd"], 0.8)

        stale_preview = self.post_json("/api/reprice/preview", {"days": 0})
        changed_again = pricing_rule(
            rule_id="third-price", provider="vendor-a", model="custom-model",
            effective_at="2026-10-01T00:00:00Z", input_rate=4, multiplier=0.5,
        )
        self.save_pricing_rules([changed_again])
        stale_status, _ = self.json_request("POST", "/api/reprice", {"preview_token": stale_preview["preview_token"]})
        self.assertIn(stale_status, (400, 409, 422), "a price change must invalidate a preview")

        preview = self.post_json("/api/reprice/preview", {"days": 0})
        self.assertEqual(preview["request_count"], 1)
        self.assertEqual(preview["cost_before_discount_usd"], 4.0)
        self.assertEqual(preview["cost_after_discount_usd"], 2.0)
        applied = self.post_json("/api/reprice", {"preview_token": preview["preview_token"]})
        self.assertEqual(applied["updated_count"], 1)
        self.assertTrue(Path(applied["backup_path"]).is_file())

        self.log.unlink()
        self.db.scan()
        after_delete = self.db.dashboard(days=0)["summary"]
        self.assertEqual(after_delete["request_count"], 1)
        self.assertEqual(after_delete["total_tokens"], 1_000_000)
        self.assertEqual(after_delete["cost_before_discount_usd"], 4.0)
        self.assertEqual(after_delete["cost_after_discount_usd"], 2.0)

        self.stop_server()
        self.db.close()
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC", clock=lambda: NOW)
        self.db.scan()
        after_restart = self.db.dashboard(days=0)["summary"]
        self.assertEqual(after_restart["request_count"], 1)
        self.assertEqual(after_restart["cost_after_discount_usd"], 2.0)

    def test_cache_availability_ttft_percentiles_and_cost_display_controls(self) -> None:
        fixture_rows: list[dict[str, Any]] = [session_meta("thread-metrics")]
        samples = [
            ("cache-915", 91_500, 100.0),
            ("cache-900", 90_000, 200.0),
            ("cache-missing", None, 300.0),
            ("cache-zero", 0, None),
        ]
        for index, (model, cached, ttft) in enumerate(samples):
            turn_id = "turn-%d" % index
            fixture_rows.append({
                "timestamp": "2026-10-12T08:%02d:00Z" % index,
                "type": "turn_context",
                "payload": {"turn_id": turn_id, "model": model},
            })
            if ttft is not None:
                fixture_rows.extend([
                    {"timestamp": "2026-10-12T08:%02d:01Z" % index, "type": "event_msg",
                     "payload": {"type": "task_started", "turn_id": turn_id}},
                    {"timestamp": "2026-10-12T08:%02d:03Z" % index, "type": "event_msg",
                     "payload": {"type": "task_complete", "turn_id": turn_id,
                                 "duration_ms": 2000, "time_to_first_token_ms": ttft}},
                ])
            fixture_rows.append(usage_record(
                thread_id="thread-metrics", response_id="response-%d" % index,
                timestamp="2026-10-12T08:%02d:02Z" % index, model=model,
                input_tokens=100_000, cached_input_tokens=cached,
                turn_id=turn_id,
            ))
        write_jsonl(self.sessions / "metrics.jsonl", fixture_rows)
        self.start_server()
        self.scan_now()

        summary = self.get_json("/api/dashboard?days=0")["summary"]
        self.assertEqual(summary["request_count"], 4)
        self.assertEqual(summary["cache_read_available_count"], 3)
        self.assertEqual(summary["ttft_sample_count"], 3)
        self.assertEqual(summary["ttft_median_ms"], 200.0)
        self.assertEqual(summary["ttft_p90_ms"], 280.0)

        for model, expected_cache, expected_ratio in (
            ("cache-915", 91_500, 0.915),
            ("cache-900", 90_000, 0.9),
            ("cache-zero", 0, 0.0),
        ):
            model_summary = self.get_json("/api/dashboard?days=0&model=" + model)["summary"]
            self.assertEqual(model_summary["input"], 100_000)
            cache_tokens = model_summary.get(
                "cache", model_summary.get("cached", model_summary.get("cached_input_tokens"))
            )
            self.assertEqual(cache_tokens, expected_cache)
            self.assertEqual(model_summary["cache_read_available_count"], 1)
            self.assertAlmostEqual(cache_tokens / model_summary["input"], expected_ratio)

        missing_cache = self.get_json("/api/dashboard?days=0&model=cache-missing")["summary"]
        self.assertEqual(missing_cache["input"], 100_000)
        self.assertEqual(missing_cache["cache_read_available_count"], 0)
        self.assertEqual(missing_cache["ttft_sample_count"], 1)
        self.assertEqual(missing_cache["ttft_median_ms"], 300.0)
        no_ttft = self.get_json("/api/dashboard?days=0&model=cache-zero")["summary"]
        self.assertEqual(no_ttft["ttft_sample_count"], 0)
        self.assertIsNone(no_ttft["ttft_median_ms"])
        self.assertIsNone(no_ttft["ttft_p90_ms"])

        web_dir = Path(__file__).resolve().parents[1] / "web"
        app = (web_dir / "app.js").read_text(encoding="utf-8")
        cache_metrics = (web_dir / "cache-metrics.js").read_text(encoding="utf-8")
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="costModeControls"', html)
        self.assertIn('data-cost-mode="before">折扣前', html)
        self.assertIn('data-cost-mode="after">折扣后', html)
        self.assertIn("codex-token-cost-mode", app)
        self.assertRegex(
            cache_metrics,
            re.compile(
                r"function formatPercent\(value, available = true\)\s*\{\s*"
                r"[\s\S]*?toFixed\(1\)[\s\S]*?: '—';",
            ),
        )
        self.assertIn("return cacheMetrics.formatPercent(value, available);", app)
        self.assertLess(html.index('src="cache-metrics.js"'), html.index('src="app.js"'))

    def test_custom_date_range_comparison_is_inclusive_and_keeps_filters(self) -> None:
        rows = [session_meta("thread-dates")]
        cases = [
            ("start", "2026-10-10T00:00:00Z", "vendor-a", "date-model", "/workspace/alpha", 10),
            ("end", "2026-10-12T23:59:59Z", "vendor-a", "date-model", "/workspace/alpha", 20),
            ("prior", "2026-10-07T12:00:00Z", "vendor-a", "date-model", "/workspace/alpha", 30),
            ("other-provider", "2026-10-11T12:00:00Z", "vendor-b", "date-model", "/workspace/alpha", 40),
            ("other-model", "2026-10-11T13:00:00Z", "vendor-a", "other-model", "/workspace/alpha", 50),
            ("other-workspace", "2026-10-11T14:00:00Z", "vendor-a", "date-model", "/workspace/beta", 60),
        ]
        for index, (response_id, timestamp, provider, model, workspace, token_count) in enumerate(cases):
            rows.append(usage_record(
                thread_id="thread-dates", response_id=response_id, timestamp=timestamp,
                model=model, provider=provider, workspace=workspace,
                input_tokens=token_count, output_tokens=0,
            ))
        write_jsonl(self.sessions / "dates.jsonl", rows)
        self.start_server()
        self.scan_now()

        query = urlencode({
            "start_date": "2026-10-10", "end_date": "2026-10-12", "compare": "1",
            "provider": "vendor-a", "model": "date-model", "workspace": "/workspace/alpha",
        })
        result = self.get_json("/api/dashboard?" + query)
        self.assertEqual(result["summary"]["request_count"], 2)
        self.assertEqual(result["summary"]["input"], 30)
        self.assertEqual(result["comparison"]["start_date"], "2026-10-07")
        self.assertEqual(result["comparison"]["end_date"], "2026-10-09")
        self.assertEqual(result["comparison"]["summary"]["request_count"], 1)
        self.assertEqual(result["comparison"]["summary"]["input"], 30)

    def test_request_pagination_sort_detail_and_price_breakdown(self) -> None:
        rules = [pricing_rule(
            rule_id="request-pricing", provider="vendor-a", model="custom-model",
            effective_at="2026-10-01T00:00:00Z", input_rate=1, output_rate=2, multiplier=0.5,
        )]
        self.start_server()
        self.save_pricing_rules(rules)
        rows = [session_meta("thread-requests")]
        for index, token_count in enumerate((10_000_000, 30_000_000, 20_000_000)):
            rows.append(usage_record(
                thread_id="thread-requests", response_id="request-%d" % index,
                timestamp="2026-10-12T08:0%d:00Z" % index,
                input_tokens=token_count, model="custom-model",
            ))
        write_jsonl(self.sessions / "requests.jsonl", rows)
        self.scan_now()

        query = urlencode({"days": 0, "page": 1, "page_size": 2, "sort": "cost_desc"})
        first_page = self.get_json("/api/requests?" + query)
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["page"], 1)
        self.assertEqual(first_page["page_size"], 2)
        self.assertEqual(len(first_page["data"]), 2)
        self.assertGreaterEqual(first_page["data"][0]["cost_after_discount_usd"],
                                first_page["data"][1]["cost_after_discount_usd"])
        self.assertEqual(first_page["data"][0]["input_tokens"], 30_000_000)
        detail = self.get_json("/api/request?" + urlencode({"id": first_page["data"][0]["request_id"]}))
        self.assertEqual(detail["request"]["request_id"], first_page["data"][0]["request_id"])
        self.assertIn("input", detail["request"]["cost_breakdown_before_discount"])
        self.assertNotIn("message", detail["request"])
        self.assertNotIn("content", detail["request"])

        second_page = self.get_json("/api/requests?" + urlencode({
            "days": 0, "page": 2, "page_size": 2, "sort": "cost_desc",
        }))
        self.assertEqual(len(second_page["data"]), 1)
        self.assertLessEqual(second_page["data"][0]["cost_after_discount_usd"],
                             first_page["data"][1]["cost_after_discount_usd"])

    def test_legacy_cumulative_usage_and_parent_child_replay_are_not_double_counted(self) -> None:
        parent_file = self.sessions / "parent.jsonl"
        child_file = self.sessions / "child.jsonl"
        parent_rows = [
            session_meta("parent-thread", timestamp="2026-10-12T08:00:00Z"),
            {"timestamp": "2026-10-12T08:00:01Z", "type": "turn_context",
             "payload": {"turn_id": "root-turn", "model": "parent-model"}},
            usage_record(thread_id="parent-thread", response_id="parent-response",
                         timestamp="2026-10-12T08:00:02Z", model="parent-model",
                         input_tokens=1_000, output_tokens=100, turn_id="root-turn"),
            {"timestamp": "2026-10-12T08:00:03Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 1_000, "output_tokens": 100, "total_tokens": 1_100},
                    "total_token_usage": {"input_tokens": 1_000, "output_tokens": 100, "total_tokens": 1_100},
                },
            }},
        ]
        child_usage = usage_record(
            thread_id="child-thread", response_id="child-response",
            timestamp="2026-10-12T08:00:04Z", model="child-model",
            input_tokens=500, output_tokens=50, turn_id="child-turn", session_id="parent-thread",
        )
        child_rows = [
            session_meta("child-thread", timestamp="2026-10-12T08:00:00Z", parent_thread_id="parent-thread"),
            {"timestamp": "2026-10-12T08:00:01Z", "type": "turn_context",
             "payload": {"turn_id": "child-turn", "model": "child-model"}},
            child_usage,
        ]
        write_jsonl(parent_file, parent_rows)
        # Child logs can replay the parent's request history. Both the replayed
        # parent request and the repeated child response must keep one identity.
        write_jsonl(child_file, child_rows + [parent_rows[2], child_usage])
        self.start_server()
        self.scan_now()

        summary = self.get_json("/api/dashboard?days=0")["summary"]
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["total_tokens"], 1_650)
        tasks = self.get_json("/api/tasks?days=0")["data"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["summary"]["request_count"], 2)
        self.assertEqual(tasks[0]["summary"]["total_tokens"], 1_650)
        roles = {agent["agent_role"] for agent in tasks[0]["agents"]}
        self.assertEqual(roles, {"main", "subagent"})

    def test_scan_options_survive_restart_and_manual_sync_still_runs(self) -> None:
        self.start_server(interval=0.1)
        saved = self.post_json("/api/scan/options", {"auto_scan": False, "interval_seconds": 10})
        self.assertFalse(saved["auto_scan"])
        self.stop_server()
        self.db.close()

        write_jsonl(self.sessions / "after-restart.jsonl", [
            session_meta("thread-scan"),
            usage_record(thread_id="thread-scan", response_id="scan-response",
                         timestamp="2026-10-12T08:00:00Z", input_tokens=25),
        ])
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC", clock=lambda: NOW)
        self.start_server(interval=0.1)
        time.sleep(0.3)
        diagnostics = self.get_json("/api/diagnostics")
        self.assertFalse(diagnostics["scan_options"]["auto_scan"])
        self.assertEqual(diagnostics["scan_options"]["interval_seconds"], 10)
        self.assertEqual(self.get_json("/api/dashboard?days=0")["summary"]["request_count"], 0)

        self.scan_now()
        self.assertEqual(self.get_json("/api/dashboard?days=0")["summary"]["request_count"], 1)
        diagnostics = self.get_json("/api/diagnostics")
        self.assertTrue(diagnostics["roots"][0]["available"])
        self.assertGreaterEqual(diagnostics["formats"]["modern"], 1)
        self.assertIn("storage", diagnostics)
        self.assertIsNotNone(diagnostics["scan"]["scan"]["last_success_at"])
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("autoScanEnabled", html)
        self.assertIn('id="scanNow"', html)

    def test_migration_backs_up_old_sqlite_schema_before_adding_analytics(self) -> None:
        path = self.root / "old-history.sqlite3"
        connection = sqlite3.connect(str(path))
        connection.executescript("""
            CREATE TABLE history_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE source_files (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL, scanned_at TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL DEFAULT '', parser_version INTEGER NOT NULL DEFAULT 0,
                usage_format TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE history_contexts (
                id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                workspace TEXT NOT NULL, UNIQUE(provider, model, workspace)
            );
            CREATE TABLE history_snapshots (
                id INTEGER PRIMARY KEY, fingerprint BLOB NOT NULL UNIQUE,
                context_id INTEGER NOT NULL REFERENCES history_contexts(id), timestamp TEXT NOT NULL,
                day TEXT NOT NULL, hour INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                cache_write_input_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL, speed_group_key TEXT,
                speed_output_tokens INTEGER NOT NULL DEFAULT 0,
                speed_duration_ms REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE history_requests (
                logical_key BLOB PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id),
                retained_snapshot_id INTEGER REFERENCES history_snapshots(id)
            ) WITHOUT ROWID;
            CREATE TABLE history_sources (
                file_id INTEGER NOT NULL REFERENCES source_files(id), logical_key BLOB NOT NULL,
                snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id), source_line INTEGER NOT NULL,
                PRIMARY KEY(file_id, logical_key)
            ) WITHOUT ROWID;
            CREATE INDEX idx_history_logical ON history_sources(logical_key);
            CREATE INDEX idx_history_day ON history_snapshots(day);
            CREATE VIEW usage_records AS
                SELECT r.logical_key AS record_key, '' AS source_path, 0 AS source_line,
                    NULL AS response_id, NULL AS thread_id, s.timestamp, s.day, s.hour,
                    c.provider, c.provider AS provider_label, c.model, c.workspace,
                    s.input_tokens, s.output_tokens, s.cached_input_tokens,
                    s.cache_write_input_tokens, s.reasoning_output_tokens, s.total_tokens
                FROM history_requests r JOIN history_snapshots s ON s.id=r.snapshot_id
                JOIN history_contexts c ON c.id=s.context_id;
        """)
        connection.execute("INSERT INTO history_meta VALUES ('timezone', 'UTC')")
        connection.execute("INSERT INTO history_contexts VALUES (1, 'vendor-old', 'legacy-model', '/workspace/old')")
        connection.execute("""INSERT INTO history_snapshots
            (id, fingerprint, context_id, timestamp, day, hour, input_tokens, output_tokens,
             cached_input_tokens, cache_write_input_tokens, reasoning_output_tokens, total_tokens,
             speed_group_key, speed_output_tokens, speed_duration_ms)
            VALUES (1, ?, 1, '2026-10-10T08:00:00Z', '2026-10-10', 8, 140, 10, 20, 0, 0, 150, NULL, 0, 0)""",
            (b"legacy-fingerprint",))
        connection.execute("INSERT INTO history_requests VALUES (?, 1, 1)", (b"legacy-logical-key",))
        connection.commit()
        connection.close()

        migrated = DashboardDB(path, roots=[], timezone_name="UTC", clock=lambda: NOW)
        try:
            self.assertEqual(migrated.dashboard(days=0)["summary"]["request_count"], 1)
            self.assertEqual(migrated.dashboard(days=0)["summary"]["total_tokens"], 150)
            current_columns = {row[1] for row in migrated._connection.execute("PRAGMA table_info(history_snapshots)")}
            self.assertIn("cost_after_discount_usd", current_columns)
        finally:
            migrated.close()

        backup = Path(str(path) + ".pre-analytics.bak")
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(str(backup))) as old_copy:
            backup_columns = {row[1] for row in old_copy.execute("PRAGMA table_info(history_snapshots)")}
            self.assertNotIn("cost_after_discount_usd", backup_columns)
            self.assertEqual(old_copy.execute("SELECT total_tokens FROM history_snapshots").fetchone()[0], 150)

    def test_cross_site_and_malformed_json_mutations_are_rejected(self) -> None:
        self.start_server()
        before = self.get_json("/api/pricing")["rules"]
        rules_payload = {"rules": [pricing_rule(
            rule_id="blocked", provider="vendor-a", model="custom-model",
            effective_at="2026-10-01T00:00:00Z", input_rate=1,
        )]}
        status, _ = self.json_request("POST", "/api/pricing", rules_payload, headers={
            "Content-Type": "application/json",
            "X-Dashboard-Request": "1",
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
        })
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)

        status, _ = self.json_request("POST", "/api/pricing", raw=b"{", headers={
            "Content-Type": "application/json",
            "X-Dashboard-Request": "1",
        })
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertEqual(self.get_json("/api/pricing")["rules"], before)

    def test_unknown_model_keeps_explicit_unpriced_status(self) -> None:
        write_jsonl(self.sessions / "unpriced.jsonl", [
            session_meta("thread-unpriced"),
            usage_record(thread_id="thread-unpriced", response_id="unknown-response",
                         timestamp="2026-10-12T08:00:00Z", model="unknown-private-model", input_tokens=100),
        ])
        self.start_server()
        self.scan_now()

        summary = self.get_json("/api/dashboard?days=0")["summary"]
        request = self.get_json("/api/requests?days=0")["data"][0]
        self.assertEqual(request["pricing_status"], "unpriced")
        self.assertEqual(summary["estimated_cost_usd"], 0)
        self.assertIn(request["cost_after_discount_usd"], (None, 0))


if __name__ == "__main__":
    unittest.main()
