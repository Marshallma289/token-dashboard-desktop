import unittest

from pricing import PricingCatalog, Quote, validate_pricing_rules


def rule(rule_id, provider="*", model="gpt-5.6-sol", effective_at="2026-01-01T00:00:00Z",
         input_rate=10, cached_input=1, cache_write=2, output=20, multiplier=1,
         source="test", note=None):
    return {
        "id": rule_id,
        "provider": provider,
        "model": model,
        "effective_at": effective_at,
        "input": input_rate,
        "cached_input": cached_input,
        "cache_write": cache_write,
        "output": output,
        "multiplier": multiplier,
        "source": source,
        "note": note,
    }


class PricingRuleTests(unittest.TestCase):
    def test_exact_provider_wins_and_latest_effective_rule_applies_at_boundary(self):
        pricing = PricingCatalog([
            rule("wild-old", input_rate=30),
            rule("wild-new", effective_at="2026-01-02T00:00:00Z", input_rate=20),
            rule("relay-old", provider="relay", input_rate=5),
            rule("relay-future", provider="relay", effective_at="2026-01-03T00:00:00Z", input_rate=2),
        ])

        at_boundary = pricing.quote(
            "models/GPT-5.6-sol", provider="relay",
            timestamp="2026-01-02T00:00:00Z", input_tokens=1_000_000,
        )
        wildcard = pricing.quote(
            "gpt-5.6-sol", provider="other",
            timestamp="2026-01-02T00:00:00Z", input_tokens=1_000_000,
        )
        before_relay_rule = pricing.quote(
            "gpt-5.6-sol", provider="relay",
            timestamp="2025-12-31T23:59:59Z", input_tokens=1_000_000,
        )

        self.assertEqual(at_boundary.rate_snapshot["id"], "relay-old")
        self.assertEqual(at_boundary.cost_before_discount_usd, 5.0)
        self.assertEqual(wildcard.rate_snapshot["id"], "wild-new")
        self.assertEqual(wildcard.cost_before_discount_usd, 20.0)
        self.assertEqual(before_relay_rule.source_url, "https://developers.openai.com/api/docs/pricing")
        self.assertEqual(before_relay_rule.cost_before_discount_usd, 8.0)

    def test_discount_breakdown_and_cache_savings_use_mutually_exclusive_input(self):
        pricing = PricingCatalog([rule("discount", multiplier=0.5)])
        quote = pricing.quote(
            "gpt-5.6-sol", timestamp="2026-01-01T00:00:00Z", provider="relay",
            input_tokens=1_000_000, cached_input_tokens=100_000,
            cache_write_input_tokens=200_000, output_tokens=100_000,
        )

        self.assertEqual(quote.cost_breakdown_before_discount, {
            "input": 7.0,
            "cached_input": 0.1,
            "cache_write": 0.4,
            "output": 2.0,
        })
        self.assertEqual(quote.cost_before_discount_usd, 9.5)
        self.assertEqual(quote.cost_after_discount_usd, 4.75)
        self.assertEqual(quote.estimated_cost_usd, quote.cost_after_discount_usd)
        self.assertEqual(quote.cost_multiplier, 0.5)
        self.assertAlmostEqual(quote.cache_savings_before_discount_usd, 0.9)
        self.assertAlmostEqual(quote.cache_savings_after_discount_usd, 0.45)
        self.assertEqual(sum(quote.cost_breakdown_before_discount.values()), 9.5)

    def test_default_quote_fields_and_unknown_and_free_snapshots(self):
        pricing = PricingCatalog()
        default = pricing.quote(
            "gpt-6-sol", timestamp="2026-09-23T00:00:00Z", input_tokens=1_000_000,
        )
        unknown = pricing.quote(
            "not-configured", timestamp="2026-09-23T00:00:00Z", input_tokens=12,
        )
        free = pricing.quote(
            "codex-auto-review", timestamp="2026-09-23T00:00:00Z",
            input_tokens=12, cached_input_tokens=2, cache_write_input_tokens=3,
        )

        self.assertEqual(default.cost_before_discount_usd, default.estimated_cost_usd)
        self.assertEqual(default.rate_snapshot["input"], 4.0)
        self.assertEqual(unknown.estimated_cost_usd, 0.0)
        self.assertIsNone(unknown.cost_before_discount_usd)
        self.assertIsNone(unknown.cost_breakdown_before_discount["input"])
        self.assertEqual(unknown.rate_snapshot["status"], "unpriced")
        self.assertEqual(free.rate_snapshot["status"], "free")
        self.assertEqual(free.cost_before_discount_usd, 0.0)
        self.assertEqual(free.estimated_cost_usd, 0.0)

    def test_explicit_editable_rule_can_override_the_free_default(self):
        pricing = PricingCatalog([rule("bad-free", model="codex-auto-review", input_rate=99)])
        quote = pricing.quote(
            "codex-auto-review", timestamp="2026-01-02T00:00:00Z", input_tokens=100_000,
        )
        self.assertEqual(quote.rate_band, "custom")
        self.assertEqual(quote.estimated_cost_usd, 9.9)
        self.assertEqual(quote.rate_snapshot["status"], "priced")

    def test_snapshot_reprices_with_frozen_rates_and_preserves_free_vs_unpriced(self):
        pricing = PricingCatalog([rule("frozen", input_rate=10, output=20, multiplier=0.25)])
        original = pricing.quote(
            "gpt-5.6-sol", timestamp="2026-01-02T00:00:00Z", provider="relay",
            input_tokens=1_000_000,
        )
        changed_catalog = PricingCatalog([rule("new", input_rate=1000, output=2000, multiplier=1)])
        frozen = changed_catalog.quote_from_snapshot(
            original.rate_snapshot, input_tokens=2_000_000, output_tokens=1_000_000,
        )
        unknown = changed_catalog.quote_from_snapshot(
            {"status": "unpriced", "model": "model-x"}, input_tokens=4,
        )
        free = changed_catalog.quote_from_snapshot(
            {"status": "free", "model": "codex-auto-review"}, input_tokens=4,
        )

        self.assertEqual(frozen.cost_before_discount_usd, 40.0)
        self.assertEqual(frozen.cost_after_discount_usd, 10.0)
        self.assertEqual(frozen.rate_snapshot["id"], "frozen")
        self.assertEqual(unknown.rate_snapshot["status"], "unpriced")
        self.assertFalse(unknown.is_priced)
        self.assertEqual(free.rate_snapshot["status"], "free")
        self.assertTrue(free.is_priced)
        self.assertEqual(free.estimated_cost_usd, 0.0)

    def test_validation_rejects_invalid_numbers_dates_models_and_duplicates(self):
        valid = rule("valid")
        self.assertEqual(validate_pricing_rules([valid])[0]["model"], "gpt-5.6-sol")

        invalid_cases = [
            ({**valid, "input": -1}, "non-negative"),
            ({**valid, "output": float("inf")}, "finite"),
            ({**valid, "multiplier": 100.1}, "between 0 and 100"),
            ({**valid, "effective_at": "2026-01-01T00:00:00+08:00"}, "UTC"),
            ({**valid, "model": "  "}, "model must be non-empty"),
        ]
        for candidate, message in invalid_cases:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, message):
                    validate_pricing_rules([candidate])
        with self.assertRaisesRegex(ValueError, "duplicate pricing rule"):
            validate_pricing_rules([valid, {**valid, "id": "duplicate-key"}])

    def test_quote_constructor_keeps_original_positional_fields(self):
        quote = Quote("priced", 1.25, "model", "standard", "source", "note")
        self.assertEqual(quote.estimated_cost_usd, 1.25)
        self.assertEqual(quote.note, "note")
        self.assertEqual(quote.rate_snapshot, {})


if __name__ == "__main__":
    unittest.main()
