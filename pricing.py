"""Reference pricing and configured free models for Codex rollout logs.

Rates are US dollars per million tokens.  The catalog intentionally returns
an unpriced result for unknown models instead of guessing from a model family.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
from typing import Dict, Mapping, Optional, Tuple


PRICING_REFERENCE_DATE = "2026-09-23"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
QWEN_PRICING_URL = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
XIAOMI_PRICING_URL = "https://xiaomimimo.com"
TOKENS_PER_MILLION = 1_000_000
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000
SOL_FLAT_RATE_NOTE = ("GPT-6.1 Sol uses standard rates at all context lengths in this catalog; "
                      "official API long-context surcharges are not included.")


@dataclass(frozen=True)
class TokenRates:
    input: float
    cached_input: float
    cache_write: float
    output: float

    def scaled(self, factor: float) -> "TokenRates":
        return TokenRates(
            self.input * factor,
            self.cached_input * factor,
            self.cache_write * factor,
            self.output * factor,
        )


@dataclass(frozen=True)
class Quote:
    status: str
    estimated_cost_usd: float = 0.0
    pricing_model: Optional[str] = None
    rate_band: Optional[str] = None
    source_url: Optional[str] = None
    note: Optional[str] = None
    # Fields are appended to preserve the original six-position constructor.
    cost_before_discount_usd: Optional[float] = None
    cost_after_discount_usd: Optional[float] = None
    cost_multiplier: Optional[float] = None
    rate_snapshot: Dict[str, object] = field(default_factory=dict)
    cost_breakdown_before_discount: Dict[str, Optional[float]] = field(default_factory=dict)
    cache_savings_before_discount_usd: Optional[float] = None
    cache_savings_after_discount_usd: Optional[float] = None

    @property
    def is_priced(self) -> bool:
        return self.status == "priced"


OPENAI_RATES: Dict[str, Tuple[TokenRates, Optional[TokenRates]]] = {
    # 1M context models use 272K input tokens as the tier boundary.
    "gpt-6-astra": (
        TokenRates(10.0, 1.0, 12.5, 50.0),
        TokenRates(20.0, 2.0, 25.0, 75.0),
    ),
    "gpt-6.1-terra": (
        TokenRates(1.0, 0.05, 1.25, 5.0),
        TokenRates(2.0, 0.1, 2.5, 7.5),
    ),
    # User-selected flat estimate; official API pricing has a long-context surcharge.
    "gpt-6.1-sol": (
        TokenRates(2.0, 0.1, 2.5, 10.0),
        None,
    ),
    "gpt-6-sol": (
        TokenRates(2.0, 0.2, 2.5, 10.0),
        TokenRates(4.0, 0.4, 5.0, 15.0),
    ),
    "gpt-6-luna": (
        TokenRates(0.1, 0.01, 0.125, 0.5),
        TokenRates(0.2, 0.02, 0.25, 0.75),
    ),
    "gpt-5.6-sol": (
        TokenRates(4.0, 0.4, 5.0, 20.0),
        TokenRates(8.0, 0.8, 10.0, 30.0),
    ),
    "gpt-5.6-terra": (
        TokenRates(2.0, 0.2, 2.5, 12.0),
        TokenRates(4.0, 0.4, 5.0, 18.0),
    ),
    "gpt-5.6-luna": (
        TokenRates(0.2, 0.02, 0.25, 1.2),
        TokenRates(0.4, 0.04, 0.5, 1.8),
    ),
    # The public page does not list a separate cache-write rate.  Treat cache
    # writes as ordinary input if a client or relay reports them.
    "gpt-5.3-codex": (
        TokenRates(1.75, 0.175, 1.75, 14.0),
        None,
    ),
}


# DeepSeek publishes off-peak rates and says peak rates are exactly double.
DEEPSEEK_RATES: Dict[str, TokenRates] = {
    "deepseek-flash": TokenRates(0.15, 0.003, 0.15, 0.6),
    "deepseek-v4-pro": TokenRates(0.66, 0.022, 0.66, 1.98),
}


# Alibaba Cloud Model Studio international rates.  Context cache hits are 10%
# of input and explicit cache creation is 125% of input.
QWEN_RATES: Dict[str, TokenRates] = {
    "qwen3.8-max": TokenRates(2.0, 0.2, 2.5, 6.0),
}


# Xiaomi MiMo real-time inference rates. The public table lists cache-hit and
# ordinary input separately but no cache-write rate, so writes bill as input.
XIAOMI_RATES: Dict[str, TokenRates] = {
    "mimo-v2.6-pro": TokenRates(0.435, 0.0036, 0.435, 0.87),
}


MODEL_ALIASES = {
    # Relays and provider routers can prefix the provider id onto the model
    # name they report (for example "deepseek/deepseek-flash"); such names
    # bill at the same reference rates as the bare model.
    "deepseek/deepseek-flash": "deepseek-flash",
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek/deepseek-v4.1-flash": "deepseek-flash",
    "xiaomi-mimo/mimo-v2.6-pro": "mimo-v2.6-pro",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "qwen3.8-max-0902": "qwen3.8-max",
    "gpt-daybreak-blue-latest": "gpt-5.6-sol",
}


# Explicit local estimation policy; this is not a published API price.
FREE_RATES = {"codex-auto-review": TokenRates(0.0, 0.0, 0.0, 0.0)}
FREE_PRICING_NOTE = "按免费计价，输入、输出及缓存费用均为 0。"
UNPRICED_MODELS: Dict[str, str] = {}


def _normalize_model(model: object) -> str:
    value = str(model or "").strip().casefold()
    if value.startswith("models/"):
        value = value[len("models/") :]
    return value


def canonical_model(model: object) -> str:
    normalized = _normalize_model(model)
    return MODEL_ALIASES.get(normalized, normalized)


def _timestamp_utc(value: object) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = _dt.datetime.fromisoformat(
                text.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def is_deepseek_peak(value: object) -> bool:
    """Return whether a timestamp falls in DeepSeek's published peak window."""

    timestamp = _timestamp_utc(value)
    if timestamp is None or timestamp.weekday() >= 5:
        return False
    minute = timestamp.hour * 60 + timestamp.minute
    return 60 <= minute < 240 or 360 <= minute < 600


def _usage_counts(
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_input_tokens: int,
    output_tokens: int,
) -> Dict[str, int]:
    total_input = max(0, int(input_tokens or 0))
    cached = min(total_input, max(0, int(cached_input_tokens or 0)))
    cache_write = min(
        max(0, total_input - cached),
        max(0, int(cache_write_input_tokens or 0)),
    )
    return {
        "input": max(0, total_input - cached - cache_write),
        "cached_input": cached,
        "cache_write": cache_write,
        "output": max(0, int(output_tokens or 0)),
    }


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a finite number") from None
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return result


def _effective_datetime(value: object) -> Tuple[_dt.datetime, str]:
    """Parse an explicit UTC ISO timestamp and return its canonical form."""

    if not isinstance(value, str) or "T" not in value.upper():
        raise ValueError("effective_at must be a UTC ISO timestamp")
    text = value.strip()
    if not (text.endswith("Z") or text.endswith("z") or text.endswith("+00:00") or text.endswith("+0000")):
        raise ValueError("effective_at must include a UTC timezone")
    parsed = _timestamp_utc(text)
    if parsed is None:
        raise ValueError("effective_at must be a valid UTC ISO timestamp")
    canonical = parsed.isoformat(timespec="auto").replace("+00:00", "Z")
    return parsed, canonical


def validate_pricing_rules(rules: object) -> list[Dict[str, object]]:
    """Validate and normalize editable provider/model rate rules.

    Rules are applied in UTC and contain prices in dollars per million tokens.
    A multiplier of 0.8 means the final estimated cost is 80% of the listed
    reference-rate estimate.
    """

    if rules is None:
        return []
    if isinstance(rules, Mapping):
        entries = [rules]
    elif isinstance(rules, (list, tuple)):
        entries = list(rules)
    else:
        raise ValueError("pricing rules must be a list of rule objects")

    required = {
        "id", "provider", "model", "effective_at", "input", "cached_input",
        "cache_write", "output", "multiplier",
    }
    normalized: list[Dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_keys: set[Tuple[str, str, str]] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise ValueError(f"pricing rule {index} must be an object")
        missing = sorted(required.difference(raw.keys()))
        if missing:
            raise ValueError(f"pricing rule {index} is missing: {', '.join(missing)}")

        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError(f"pricing rule {index} id must be a non-empty string")
        rule_id = rule_id.strip()
        if rule_id in seen_ids:
            raise ValueError(f"duplicate pricing rule id: {rule_id}")

        provider = raw.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(f"pricing rule {index} provider must be '*' or a non-empty id")
        provider = provider.strip()

        model_value = raw.get("model")
        if not isinstance(model_value, str):
            raise ValueError(f"pricing rule {index} model must be a non-empty string")
        model = canonical_model(model_value)
        if not model:
            raise ValueError(f"pricing rule {index} model must be non-empty")

        _, effective_at = _effective_datetime(raw.get("effective_at"))
        rates = {}
        for name in ("input", "cached_input", "cache_write", "output"):
            amount = _decimal(raw.get(name), f"pricing rule {index} {name}")
            if amount < 0:
                raise ValueError(f"pricing rule {index} {name} must be non-negative")
            as_float = float(amount)
            if not math.isfinite(as_float):
                raise ValueError(f"pricing rule {index} {name} must be finite")
            rates[name] = as_float

        multiplier = _decimal(raw.get("multiplier"), f"pricing rule {index} multiplier")
        if multiplier < 0 or multiplier > 100:
            raise ValueError(f"pricing rule {index} multiplier must be between 0 and 100")

        key = (provider, model, effective_at)
        if key in seen_keys:
            raise ValueError(
                f"duplicate pricing rule for provider={provider}, model={model}, effective_at={effective_at}"
            )
        seen_ids.add(rule_id)
        seen_keys.add(key)

        source = raw.get("source")
        note = raw.get("note")
        if source is not None and not isinstance(source, str):
            raise ValueError(f"pricing rule {index} source must be a string or null")
        if note is not None and not isinstance(note, str):
            raise ValueError(f"pricing rule {index} note must be a string or null")
        normalized.append({
            "id": rule_id,
            "provider": provider,
            "model": model,
            "effective_at": effective_at,
            **rates,
            "multiplier": float(multiplier),
            "source": source.strip() if isinstance(source, str) else None,
            "note": note.strip() if isinstance(note, str) else None,
        })
    return normalized


def _unpriced_quote(model: str, note: str) -> Quote:
    empty_breakdown = {key: None for key in ("input", "cached_input", "cache_write", "output")}
    return Quote(
        "unpriced",
        pricing_model=model,
        note=note,
        cost_before_discount_usd=None,
        cost_after_discount_usd=None,
        cost_multiplier=None,
        rate_snapshot={
            "status": "unpriced", "model": model,
            "input": None, "cached_input": None, "cache_write": None, "output": None,
            "id": None, "source": None, "effective_at": None,
            "multiplier": None, "note": note,
        },
        cost_breakdown_before_discount=empty_breakdown,
        cache_savings_before_discount_usd=None,
        cache_savings_after_discount_usd=None,
    )


def _priced_quote(
    *,
    model: str,
    rates: TokenRates,
    multiplier: object,
    timestamp: object,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_input_tokens: int,
    output_tokens: int,
    rate_band: Optional[str],
    source_url: Optional[str],
    rule_id: Optional[str] = None,
    effective_at: Optional[str] = None,
    note: Optional[str] = None,
    snapshot_status: str = "priced",
) -> Quote:
    counts = _usage_counts(
        input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens
    )
    rate_values = {
        "input": Decimal(str(rates.input)),
        "cached_input": Decimal(str(rates.cached_input)),
        "cache_write": Decimal(str(rates.cache_write)),
        "output": Decimal(str(rates.output)),
    }
    multiplier_decimal = _decimal(multiplier, "cost multiplier")
    breakdown_decimal = {
        key: Decimal(count) * rate_values[key] / Decimal(TOKENS_PER_MILLION)
        for key, count in counts.items()
    }
    before = sum(breakdown_decimal.values(), Decimal(0))
    after = before * multiplier_decimal
    cache_savings = (
        Decimal(counts["cached_input"])
        * max(Decimal(0), rate_values["input"] - rate_values["cached_input"])
        / Decimal(TOKENS_PER_MILLION)
    )
    before_float = float(before)
    after_float = float(after)
    rates_snapshot = {
        "status": snapshot_status,
        "model": model,
        "input": float(rates.input),
        "cached_input": float(rates.cached_input),
        "cache_write": float(rates.cache_write),
        "output": float(rates.output),
        "id": rule_id,
        "source": source_url,
        "effective_at": effective_at,
        "multiplier": float(multiplier_decimal),
        "rate_band": rate_band,
        "note": note,
    }
    return Quote(
        "priced",
        after_float,
        model,
        rate_band,
        source_url,
        note,
        before_float,
        after_float,
        float(multiplier_decimal),
        rates_snapshot,
        {key: float(value) for key, value in breakdown_decimal.items()},
        float(cache_savings),
        float(cache_savings * multiplier_decimal),
    )


class PricingCatalog:
    """Resolve model usage to configured or reference-rate estimates."""

    def __init__(self, rules: object = None):
        self.rules = validate_pricing_rules(rules)
        self._rule_dates = {
            rule["id"]: _timestamp_utc(rule["effective_at"])
            for rule in self.rules
        }

    def _select_rule(
        self, model: str, provider: object, timestamp: object
    ) -> Optional[Dict[str, object]]:
        request_time = _timestamp_utc(timestamp)
        if not model or request_time is None:
            return None
        provider_id = str(provider).strip() if provider is not None else ""
        for scope in ([provider_id, "*"] if provider_id and provider_id != "*" else ["*"]):
            eligible = [
                rule for rule in self.rules
                if rule["provider"] == scope
                and rule["model"] == model
                and self._rule_dates[rule["id"]] <= request_time
            ]
            if eligible:
                return max(eligible, key=lambda rule: self._rule_dates[rule["id"]])
        return None

    def quote(
        self,
        model: object,
        *,
        timestamp: object,
        input_tokens: int,
        cached_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
        output_tokens: int = 0,
        provider: object = None,
        aggregate: bool = False,
    ) -> Quote:
        pricing_model = canonical_model(model)

        rule = self._select_rule(pricing_model, provider, timestamp)
        # Keep the existing free default unless the user explicitly supplies
        # an applicable rule through the software's price settings.
        if pricing_model in FREE_RATES and rule is None:
            return _priced_quote(
                model=pricing_model,
                rates=FREE_RATES[pricing_model],
                multiplier=1,
                timestamp=timestamp,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens,
                output_tokens=output_tokens,
                rate_band="free",
                source_url=None,
                note=FREE_PRICING_NOTE,
                snapshot_status="free",
            )

        if rule is not None:
            return _priced_quote(
                model=pricing_model,
                rates=TokenRates(
                    rule["input"], rule["cached_input"],
                    rule["cache_write"], rule["output"],
                ),
                multiplier=rule["multiplier"],
                timestamp=timestamp,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens,
                output_tokens=output_tokens,
                rate_band="custom",
                source_url=rule["source"],
                rule_id=rule["id"],
                effective_at=rule["effective_at"],
                note=rule["note"],
            )

        openai_prices = OPENAI_RATES.get(pricing_model)
        if openai_prices is not None:
            short_rates, long_rates = openai_prices
            rates = short_rates
            band = "standard"
            if not aggregate and long_rates is not None and int(input_tokens or 0) > OPENAI_LONG_CONTEXT_THRESHOLD:
                rates = long_rates
                band = "long_context"
            return _priced_quote(
                model=pricing_model, rates=rates, multiplier=1, timestamp=timestamp,
                input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens, output_tokens=output_tokens,
                rate_band=band, source_url=OPENAI_PRICING_URL,
                effective_at=f"{PRICING_REFERENCE_DATE}T00:00:00Z",
                note=SOL_FLAT_RATE_NOTE if pricing_model == "gpt-6.1-sol" else None,
            )

        deepseek_rates = DEEPSEEK_RATES.get(pricing_model)
        if deepseek_rates is not None:
            peak = is_deepseek_peak(timestamp)
            rates = deepseek_rates.scaled(2.0) if peak else deepseek_rates
            return _priced_quote(
                model=pricing_model, rates=rates, multiplier=1, timestamp=timestamp,
                input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens, output_tokens=output_tokens,
                rate_band="peak" if peak else "off_peak", source_url=DEEPSEEK_PRICING_URL,
                effective_at=f"{PRICING_REFERENCE_DATE}T00:00:00Z",
            )

        qwen_rates = QWEN_RATES.get(pricing_model)
        if qwen_rates is not None:
            return _priced_quote(
                model=pricing_model, rates=qwen_rates, multiplier=1, timestamp=timestamp,
                input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens, output_tokens=output_tokens,
                rate_band="standard", source_url=QWEN_PRICING_URL,
                effective_at=f"{PRICING_REFERENCE_DATE}T00:00:00Z",
            )

        xiaomi_rates = XIAOMI_RATES.get(pricing_model)
        if xiaomi_rates is not None:
            return _priced_quote(
                model=pricing_model, rates=xiaomi_rates, multiplier=1, timestamp=timestamp,
                input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens, output_tokens=output_tokens,
                rate_band="real_time", source_url=XIAOMI_PRICING_URL,
                effective_at=f"{PRICING_REFERENCE_DATE}T00:00:00Z",
            )

        return _unpriced_quote(
            pricing_model or str(model or "unknown"),
            UNPRICED_MODELS.get(pricing_model, "No official price is configured for this model."),
        )

    def quote_from_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        model: object = None,
        timestamp: object = None,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
        output_tokens: int = 0,
        provider: object = None,
    ) -> Quote:
        """Recalculate usage at a frozen quote's rates and discount multiplier."""

        if not isinstance(snapshot, Mapping):
            raise ValueError("rate snapshot must be an object")
        pricing_model = canonical_model(model or snapshot.get("model") or "unknown")
        status = snapshot.get("status")
        note = snapshot.get("note") if isinstance(snapshot.get("note"), str) else None
        if status == "unpriced":
            return _unpriced_quote(pricing_model, note or "The frozen quote was unpriced.")
        if status == "free":
            rates = TokenRates(0.0, 0.0, 0.0, 0.0)
            multiplier = 1
        elif status == "priced":
            values = []
            for key in ("input", "cached_input", "cache_write", "output"):
                amount = _decimal(snapshot.get(key), f"rate snapshot {key}")
                if amount < 0:
                    raise ValueError(f"rate snapshot {key} must be non-negative")
                values.append(float(amount))
            rates = TokenRates(*values)
            multiplier = snapshot.get("multiplier", 1)
            multiplier_decimal = _decimal(multiplier, "rate snapshot multiplier")
            if multiplier_decimal < 0 or multiplier_decimal > 100:
                raise ValueError("rate snapshot multiplier must be between 0 and 100")
        else:
            raise ValueError("rate snapshot status must be priced, free, or unpriced")

        source = snapshot.get("source")
        if source is not None and not isinstance(source, str):
            raise ValueError("rate snapshot source must be a string or null")
        effective_at = snapshot.get("effective_at")
        if effective_at is not None:
            _, effective_at = _effective_datetime(effective_at)
        band = snapshot.get("rate_band")
        if band is not None and not isinstance(band, str):
            raise ValueError("rate snapshot rate_band must be a string or null")
        rule_id = snapshot.get("id")
        if rule_id is not None and not isinstance(rule_id, str):
            raise ValueError("rate snapshot id must be a string or null")
        return _priced_quote(
            model=pricing_model,
            rates=rates,
            multiplier=multiplier,
            timestamp=timestamp,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            output_tokens=output_tokens,
            rate_band=band,
            source_url=source,
            rule_id=rule_id,
            effective_at=effective_at,
            note=note,
            snapshot_status=status,
        )

    def metadata(self) -> Dict[str, object]:
        models = [
            {
                "model": model,
                "status": "priced",
                "short_context": {
                    "input": short.input,
                    "cached_input": short.cached_input,
                    "cache_write": short.cache_write,
                    "output": short.output,
                },
                "long_context": (
                    {
                        "input": long.input,
                        "cached_input": long.cached_input,
                        "cache_write": long.cache_write,
                        "output": long.output,
                    }
                    if long
                    else None
                ),
                "source": OPENAI_PRICING_URL,
                "note": SOL_FLAT_RATE_NOTE if model == "gpt-6.1-sol" else None,
            }
            for model, (short, long) in OPENAI_RATES.items()
        ]
        models.extend(
            {
                "model": model,
                "status": "priced",
                "standard": {
                    "input": rates.input,
                    "cached_input": rates.cached_input,
                    "cache_write": rates.cache_write,
                    "output": rates.output,
                },
                "rate_band": "free",
                "source": None,
                "note": FREE_PRICING_NOTE,
            }
            for model, rates in FREE_RATES.items()
        )
        models.extend(
            {
                "model": model,
                "status": "priced",
                "off_peak": {
                    "input": rates.input,
                    "cached_input": rates.cached_input,
                    "output": rates.output,
                },
                "peak": {
                    "input": rates.input * 2,
                    "cached_input": rates.cached_input * 2,
                    "output": rates.output * 2,
                },
                "source": DEEPSEEK_PRICING_URL,
            }
            for model, rates in DEEPSEEK_RATES.items()
        )
        models.extend(
            {
                "model": model,
                "status": "priced",
                "standard": {
                    "input": rates.input,
                    "cached_input": rates.cached_input,
                    "cache_write": rates.cache_write,
                    "output": rates.output,
                },
                "source": QWEN_PRICING_URL,
            }
            for model, rates in QWEN_RATES.items()
        )
        models.extend(
            {
                "model": model,
                "status": "priced",
                "real_time": {
                    "input": rates.input,
                    "cached_input": rates.cached_input,
                    "cache_write": rates.cache_write,
                    "output": rates.output,
                },
                "source": XIAOMI_PRICING_URL,
            }
            for model, rates in XIAOMI_RATES.items()
        )
        models.extend(
            {
                "model": model,
                "status": "unpriced",
                "reason": reason,
            }
            for model, reason in UNPRICED_MODELS.items()
        )
        return {
            "currency": "USD",
            "unit": "per_million_tokens",
            "reference_date": PRICING_REFERENCE_DATE,
            "is_estimate": True,
            "sources": [
                OPENAI_PRICING_URL,
                DEEPSEEK_PRICING_URL,
                QWEN_PRICING_URL,
                XIAOMI_PRICING_URL,
            ],
            "models": models,
            "rules": [dict(rule) for rule in self.rules],
            "notes": [
                "OpenAI input, cached input, and cache write tokens are treated as mutually exclusive input classes.",
                "OpenAI models with long-context rates switch when input tokens exceed 272000.",
                "DeepSeek peak hours are 01:00-04:00 and 06:00-10:00 UTC on weekdays; other times use off-peak rates.",
                "DeepSeek Chinese public-holiday exceptions are not inferred from rollout logs; weekday peak windows are applied literally.",
                "Qwen pricing uses Alibaba Cloud Model Studio international rates because rollout logs do not record a deployment region.",
                "Xiaomi MiMo cache-write input tokens are estimated at ordinary input rates because no separate cache-write rate is published in the pricing table.",
                "Built-in reference prices match by model name; configured provider/model rules override them.",
                "Estimates use current published reference rates applied to historical token usage and are not billing statements.",
            ],
        }


DEFAULT_PRICING = PricingCatalog()
