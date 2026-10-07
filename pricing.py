"""Reference pricing and configured free models for Codex rollout logs.

Rates are US dollars per million tokens.  The catalog intentionally returns
an unpriced result for unknown models instead of guessing from a model family.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


PRICING_REFERENCE_DATE = "2026-09-23"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
QWEN_PRICING_URL = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
XIAOMI_PRICING_URL = "https://xiaomimimo.com"
TOKENS_PER_MILLION = 1_000_000
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000


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

    @property
    def is_priced(self) -> bool:
        return self.status == "priced"


OPENAI_RATES: Dict[str, Tuple[TokenRates, Optional[TokenRates]]] = {
    # 1M context models use 272K input tokens as the tier boundary.
    "gpt-6-astra": (
        TokenRates(10.0, 1.0, 12.5, 50.0),
        TokenRates(20.0, 2.0, 25.0, 75.0),
    ),
    "gpt-6.1-sol": (
        TokenRates(2.0, 0.1, 2.5, 10.0),
        TokenRates(4.0, 0.2, 5.0, 15.0),
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
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
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


def _cost(
    rates: TokenRates,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_input_tokens: int,
    output_tokens: int,
) -> float:
    input_tokens = max(0, int(input_tokens or 0))
    cached = min(input_tokens, max(0, int(cached_input_tokens or 0)))
    cache_write = min(
        max(0, input_tokens - cached),
        max(0, int(cache_write_input_tokens or 0)),
    )
    ordinary_input = max(0, input_tokens - cached - cache_write)
    output_tokens = max(0, int(output_tokens or 0))
    return (
        ordinary_input * rates.input
        + cached * rates.cached_input
        + cache_write * rates.cache_write
        + output_tokens * rates.output
    ) / TOKENS_PER_MILLION


class PricingCatalog:
    """Resolve observed model names to an official-rate estimate."""

    def quote(
        self,
        model: object,
        *,
        timestamp: object,
        input_tokens: int,
        cached_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Quote:
        pricing_model = canonical_model(model)

        if pricing_model in FREE_RATES:
            return Quote(
                "priced", pricing_model=pricing_model, rate_band="free",
                note=FREE_PRICING_NOTE,
            )

        openai_prices = OPENAI_RATES.get(pricing_model)
        if openai_prices is not None:
            short_rates, long_rates = openai_prices
            rates = short_rates
            band = "standard"
            if long_rates is not None and int(input_tokens or 0) > OPENAI_LONG_CONTEXT_THRESHOLD:
                rates = long_rates
                band = "long_context"
            return Quote(
                "priced",
                _cost(
                    rates,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    output_tokens,
                ),
                pricing_model,
                band,
                OPENAI_PRICING_URL,
            )

        deepseek_rates = DEEPSEEK_RATES.get(pricing_model)
        if deepseek_rates is not None:
            peak = is_deepseek_peak(timestamp)
            rates = deepseek_rates.scaled(2.0) if peak else deepseek_rates
            return Quote(
                "priced",
                _cost(
                    rates,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    output_tokens,
                ),
                pricing_model,
                "peak" if peak else "off_peak",
                DEEPSEEK_PRICING_URL,
            )

        qwen_rates = QWEN_RATES.get(pricing_model)
        if qwen_rates is not None:
            return Quote(
                "priced",
                _cost(
                    qwen_rates,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    output_tokens,
                ),
                pricing_model,
                "standard",
                QWEN_PRICING_URL,
            )

        xiaomi_rates = XIAOMI_RATES.get(pricing_model)
        if xiaomi_rates is not None:
            return Quote(
                "priced",
                _cost(
                    xiaomi_rates,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    output_tokens,
                ),
                pricing_model,
                "real_time",
                XIAOMI_PRICING_URL,
            )

        return Quote(
            "unpriced",
            pricing_model=pricing_model or str(model or "unknown"),
            note=UNPRICED_MODELS.get(pricing_model, "No official price is configured for this model."),
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
            "notes": [
                "OpenAI input, cached input, and cache write tokens are treated as mutually exclusive input classes.",
                "OpenAI models with long-context rates switch when input tokens exceed 272000.",
                "DeepSeek peak hours are 01:00-04:00 and 06:00-10:00 UTC on weekdays; other times use off-peak rates.",
                "DeepSeek Chinese public-holiday exceptions are not inferred from rollout logs; weekday peak windows are applied literally.",
                "Qwen pricing uses Alibaba Cloud Model Studio international rates because rollout logs do not record a deployment region.",
                "Xiaomi MiMo cache-write input tokens are estimated at ordinary input rates because no separate cache-write rate is published in the pricing table.",
                "Models are matched by model name without restricting the provider id; supplier-specific discounts or contract pricing are not applied.",
                "Estimates use current published reference rates applied to historical token usage and are not billing statements.",
            ],
        }


DEFAULT_PRICING = PricingCatalog()
