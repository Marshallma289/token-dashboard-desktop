import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB
from pricing import OPENAI_LONG_CONTEXT_THRESHOLD, PricingCatalog, canonical_model


class PricingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.pricing = PricingCatalog()

    def test_gpt6_sol_and_luna_rates(self):
        sol = self.pricing.quote(
            "gpt-6-sol",
            timestamp="2026-09-23T05:00:00Z",
            input_tokens=100_000,
            cached_input_tokens=10_000,
            cache_write_input_tokens=10_000,
            output_tokens=20_000,
        )
        luna = self.pricing.quote(
            "gpt-6-luna",
            timestamp="2026-09-23T05:00:00Z",
            input_tokens=100_000,
            cached_input_tokens=10_000,
            cache_write_input_tokens=10_000,
            output_tokens=20_000,
        )

        self.assertTrue(sol.is_priced)
        self.assertEqual(sol.rate_band, "standard")
        self.assertAlmostEqual(sol.estimated_cost_usd, 0.387, places=10)
        self.assertTrue(luna.is_priced)
        self.assertEqual(luna.rate_band, "standard")
        self.assertAlmostEqual(luna.estimated_cost_usd, 0.01935, places=10)

        metadata = {item["model"]: item for item in self.pricing.metadata()["models"]}
        self.assertEqual(metadata["gpt-6-sol"]["short_context"]["input"], 2.0)
        self.assertEqual(metadata["gpt-6-luna"]["short_context"]["output"], 0.5)

    def test_openai_token_classes_are_priced_separately(self):
        quote = self.pricing.quote(
            "gpt-5.6-sol",
            timestamp="2026-09-18T05:00:00Z",
            input_tokens=1000,
            cached_input_tokens=200,
            cache_write_input_tokens=100,
            output_tokens=300,
        )

        self.assertTrue(quote.is_priced)
        self.assertEqual(quote.rate_band, "standard")
        self.assertAlmostEqual(quote.estimated_cost_usd, 0.00938, places=10)

    def test_openai_long_context_boundary(self):
        standard = self.pricing.quote(
            "gpt-5.6-terra",
            timestamp="2026-09-18T05:00:00Z",
            input_tokens=OPENAI_LONG_CONTEXT_THRESHOLD,
        )
        long_context = self.pricing.quote(
            "gpt-5.6-terra",
            timestamp="2026-09-18T05:00:00Z",
            input_tokens=OPENAI_LONG_CONTEXT_THRESHOLD + 1,
        )

        self.assertEqual(standard.rate_band, "standard")
        self.assertAlmostEqual(standard.estimated_cost_usd, 0.544, places=10)
        self.assertEqual(long_context.rate_band, "long_context")
        self.assertAlmostEqual(long_context.estimated_cost_usd, 1.088004, places=10)

    def test_deepseek_peak_and_off_peak_rates(self):
        peak = self.pricing.quote(
            "deepseek-v4-pro",
            timestamp=dt.datetime(2026, 9, 18, 2, 0, tzinfo=dt.timezone.utc),
            input_tokens=1_000_000,
        )
        off_peak = self.pricing.quote(
            "deepseek-v4-pro",
            timestamp=dt.datetime(2026, 9, 18, 5, 0, tzinfo=dt.timezone.utc),
            input_tokens=1_000_000,
        )
        weekend = self.pricing.quote(
            "deepseek-v4-pro",
            timestamp=dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.timezone.utc),
            input_tokens=1_000_000,
        )

        self.assertEqual(peak.rate_band, "peak")
        self.assertAlmostEqual(peak.estimated_cost_usd, 1.32, places=10)
        self.assertEqual(off_peak.rate_band, "off_peak")
        self.assertAlmostEqual(off_peak.estimated_cost_usd, 0.66, places=10)
        self.assertEqual(weekend.rate_band, "off_peak")

    def test_xiaomi_mimo_v2_6_pro_rates(self):
        self.assertEqual(
            canonical_model("xiaomi-mimo/mimo-v2.6-pro"), "mimo-v2.6-pro"
        )
        quote = self.pricing.quote(
            "mimo-v2.6-pro",
            timestamp="2026-09-22T05:00:00Z",
            input_tokens=1_000_000,
            cached_input_tokens=250_000,
            output_tokens=100_000,
        )

        self.assertTrue(quote.is_priced)
        self.assertEqual(quote.pricing_model, "mimo-v2.6-pro")
        self.assertEqual(quote.rate_band, "real_time")
        # 750K ordinary input + 250K cache-hit input + 100K output.
        self.assertAlmostEqual(quote.estimated_cost_usd, 0.41415, places=10)

        metadata = self.pricing.metadata()
        model = next(item for item in metadata["models"] if item["model"] == "mimo-v2.6-pro")
        self.assertEqual(model["real_time"]["cached_input"], 0.0036)
        self.assertEqual(model["real_time"]["input"], 0.435)
        self.assertEqual(model["real_time"]["output"], 0.87)

    def test_aliases_qwen_and_unknown_models(self):
        qwen = self.pricing.quote(
            "models/qwen3.8-max-0902",
            timestamp="2026-09-18T05:00:00Z",
            input_tokens=1000,
            cached_input_tokens=100,
            cache_write_input_tokens=100,
            output_tokens=200,
        )
        unknown = self.pricing.quote(
            "codex-auto-review",
            timestamp="2026-09-18T05:00:00Z",
            input_tokens=1000,
        )

        self.assertEqual(canonical_model("deepseek-v4-flash"), "deepseek-flash")
        self.assertEqual(canonical_model("deepseek/deepseek-flash"), "deepseek-flash")
        self.assertEqual(canonical_model("models/DeepSeek/DeepSeek-Flash"), "deepseek-flash")
        self.assertEqual(canonical_model("gpt-daybreak-blue-latest"), "gpt-5.6-sol")
        self.assertEqual(qwen.pricing_model, "qwen3.8-max")
        self.assertEqual(qwen.rate_band, "standard")
        self.assertAlmostEqual(qwen.estimated_cost_usd, 0.00307, places=10)
        self.assertFalse(unknown.is_priced)
        self.assertIn("standalone", unknown.note)

    def test_relay_prefixed_deepseek_flash_uses_bare_model_rates(self):
        peak_timestamp = dt.datetime(2026, 9, 18, 2, 0, tzinfo=dt.timezone.utc)
        off_peak_timestamp = dt.datetime(2026, 9, 18, 5, 0, tzinfo=dt.timezone.utc)

        prefixed_peak = self.pricing.quote(
            "deepseek/deepseek-flash",
            timestamp=peak_timestamp,
            input_tokens=1_000_000,
        )
        bare_peak = self.pricing.quote(
            "deepseek-flash",
            timestamp=peak_timestamp,
            input_tokens=1_000_000,
        )
        prefixed_off_peak = self.pricing.quote(
            "deepseek/deepseek-flash",
            timestamp=off_peak_timestamp,
            input_tokens=1_000_000,
        )

        self.assertTrue(prefixed_peak.is_priced)
        self.assertEqual(prefixed_peak.pricing_model, "deepseek-flash")
        self.assertEqual(prefixed_peak.rate_band, "peak")
        self.assertAlmostEqual(prefixed_peak.estimated_cost_usd, 0.3, places=10)
        self.assertAlmostEqual(
            prefixed_peak.estimated_cost_usd, bare_peak.estimated_cost_usd, places=10
        )
        self.assertEqual(prefixed_off_peak.rate_band, "off_peak")
        self.assertEqual(prefixed_off_peak.pricing_model, "deepseek-flash")
        self.assertAlmostEqual(prefixed_off_peak.estimated_cost_usd, 0.15, places=10)
        self.assertAlmostEqual(
            prefixed_peak.estimated_cost_usd,
            2 * prefixed_off_peak.estimated_cost_usd,
            places=10,
        )


class PricingDashboardTests(unittest.TestCase):
    def test_xiaomi_prefixed_model_merges_into_bare_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            rows = [
                {
                    "timestamp": "2026-09-22T05:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": "mimo-merge",
                        "cwd": "/workspace/mimo",
                        "model_provider": "xiaomi-mimo",
                    },
                },
                {
                    "timestamp": "2026-09-22T05:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "prefixed", "model": "xiaomi-mimo/mimo-v2.6-pro"},
                },
                {
                    "timestamp": "2026-09-22T05:00:02Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-mimo",
                        "response_id": "mimo-prefixed",
                        "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    },
                },
                {
                    "timestamp": "2026-09-22T05:00:03Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "bare", "model": "mimo-v2.6-pro"},
                },
                {
                    "timestamp": "2026-09-22T05:00:04Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-mimo",
                        "response_id": "mimo-bare",
                        "usage": {"input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200},
                    },
                },
            ]
            (root / "mimo.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            db = DashboardDB(Path(folder) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                dashboard = db.dashboard(days=0)
            finally:
                db.close()

        self.assertEqual(dashboard["filters"]["models"], ["mimo-v2.6-pro"])
        usage = dashboard["daily_model_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["model"], "mimo-v2.6-pro")
        self.assertEqual(usage[0]["total_tokens"], 3300)
        self.assertEqual(
            usage[0]["source_models"],
            ["mimo-v2.6-pro", "xiaomi-mimo/mimo-v2.6-pro"],
        )
        self.assertEqual(usage[0]["pricing_status"], "priced")
        # 3000 input tokens at $0.435/M plus 300 output tokens at $0.87/M.
        self.assertAlmostEqual(usage[0]["estimated_cost_usd"], 0.001566, places=10)

    def test_relay_prefixed_model_is_counted_as_the_canonical_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            rows = [
                {
                    "timestamp": "2026-09-18T05:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": "merge",
                        "cwd": "/workspace/merge",
                        "model_provider": "opencodex",
                    },
                },
                {
                    "timestamp": "2026-09-18T05:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "prefixed-turn", "model": "deepseek/deepseek-flash"},
                },
                {
                    "timestamp": "2026-09-18T05:00:02Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-merge",
                        "response_id": "prefixed-response",
                        "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    },
                },
                {
                    "timestamp": "2026-09-18T05:00:03Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "bare-turn", "model": "deepseek-flash"},
                },
                {
                    "timestamp": "2026-09-18T05:00:04Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-merge",
                        "response_id": "bare-response",
                        "usage": {"input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200},
                    },
                },
            ]
            (root / "merge.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            db = DashboardDB(Path(folder) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                dashboard = db.dashboard(days=0)
                by_prefixed = db.dashboard(days=0, model="deepseek/deepseek-flash")
                by_bare = db.dashboard(days=0, model="deepseek-flash")
            finally:
                db.close()

        self.assertEqual(dashboard["filters"]["models"], ["deepseek-flash"])
        usage = dashboard["daily_model_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["model"], "deepseek-flash")
        self.assertEqual(usage[0]["total_tokens"], 3300)
        self.assertEqual(usage[0]["request_count"], 2)
        self.assertEqual(usage[0]["pricing_status"], "priced")
        self.assertEqual(
            usage[0]["source_models"], ["deepseek-flash", "deepseek/deepseek-flash"]
        )
        # 3000 input tokens at $0.15/M plus 300 output tokens at $0.6/M.
        self.assertAlmostEqual(usage[0]["estimated_cost_usd"], 0.00063, places=10)
        for filtered in (by_prefixed, by_bare):
            self.assertEqual(len(filtered["daily_model_usage"]), 1)
            self.assertEqual(filtered["daily_model_usage"][0]["total_tokens"], 3300)

    def test_dashboard_aggregates_cost_coverage_and_unpriced_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sessions"
            root.mkdir()
            rows = [
                {
                    "timestamp": "2026-09-18T05:00:00Z",
                    "type": "session_meta",
                    "payload": {"session_id": "pricing", "cwd": "/workspace/pricing", "model_provider": "openai"},
                },
                {
                    "timestamp": "2026-09-18T05:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "priced-turn", "model": "gpt-5.6-luna"},
                },
                {
                    "timestamp": "2026-09-18T05:00:02Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-pricing",
                        "response_id": "priced-response",
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 200,
                            "cache_write_input_tokens": 100,
                            "output_tokens": 300,
                            "total_tokens": 1300,
                        },
                    },
                },
                {
                    "timestamp": "2026-09-18T05:00:03Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "unpriced-turn", "model": "codex-auto-review"},
                },
                {
                    "timestamp": "2026-09-18T05:00:04Z",
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "thread-pricing",
                        "response_id": "unpriced-response",
                        "usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
                    },
                },
            ]
            (root / "pricing.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            db = DashboardDB(Path(folder) / "cache.sqlite3", roots=[root], timezone_name="UTC")
            try:
                db.scan()
                dashboard = db.dashboard(days=0)
            finally:
                db.close()

        summary = dashboard["summary"]
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.000529, places=10)
        self.assertEqual(summary["priced_tokens"], 1300)
        self.assertEqual(summary["unpriced_tokens"], 50)
        self.assertAlmostEqual(summary["pricing_coverage"], 1300 / 1350, places=10)

        unpriced = dashboard["pricing"]["unpriced_model_usage"]
        self.assertEqual(unpriced, [{"provider": "openai", "model": "codex-auto-review", "total_tokens": 50}])
        daily = {row["model"]: row for row in dashboard["daily_model_usage"]}
        self.assertEqual(daily["gpt-5.6-luna"]["pricing_status"], "priced")
        self.assertEqual(daily["codex-auto-review"]["pricing_status"], "unpriced")


if __name__ == "__main__":
    unittest.main()
