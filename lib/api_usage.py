"""Structured, content-free accounting for text-model API calls."""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# USD per one million tokens. Keep these estimates explicit and versioned in
# code so historical log entries remain interpretable when provider prices
# change. Unknown models still log tokens, with estimated_cost_usd=None.
_PRICES = {
    ("deepseek", "deepseek-v4-flash"): {"input": 0.14, "cached": 0.0028, "output": 0.28},
    ("deepseek", "deepseek-v4-pro"): {"input": 0.435, "cached": 0.003625, "output": 0.87},
    ("openai", "gpt-4o-mini"): {"input": 0.15, "cached": 0.075, "output": 0.60},
}


def _number(value: Any) -> int:
    """Return SDK usage values as safe non-negative integers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def record_chat_usage(
    response: Any,
    *,
    provider: str,
    model: str,
    stage: str,
    attempt: int = 1,
) -> dict:
    """Log token counts, retries, cache hits, and estimated cost.

    Prompts and model output are deliberately excluded from the record.
    """
    usage = getattr(response, "usage", None)
    input_tokens = _number(getattr(usage, "prompt_tokens", 0))
    output_tokens = _number(getattr(usage, "completion_tokens", 0))

    cached_tokens = _number(getattr(usage, "prompt_cache_hit_tokens", 0))
    miss_tokens = _number(getattr(usage, "prompt_cache_miss_tokens", 0))
    if not cached_tokens:
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = _number(getattr(details, "cached_tokens", 0))
    cached_tokens = min(cached_tokens, input_tokens) if input_tokens else cached_tokens
    if not miss_tokens:
        miss_tokens = max(0, input_tokens - cached_tokens)

    price = _PRICES.get((provider, model))
    estimated_cost = None
    if price:
        estimated_cost = (
            miss_tokens * price["input"]
            + cached_tokens * price["cached"]
            + output_tokens * price["output"]
        ) / 1_000_000

    record = {
        "provider": provider,
        "model": model,
        "stage": stage,
        "status": "ok",
        "attempt": max(1, int(attempt)),
        "retry_count": max(0, int(attempt) - 1),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "uncached_input_tokens": miss_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated_cost, 8) if estimated_cost is not None else None,
    }
    log.info("API usage %s", json.dumps(record, separators=(",", ":"), sort_keys=True))
    return record


def record_chat_failure(*, provider: str, model: str, stage: str, attempt: int, exc: Exception) -> None:
    """Log a failed call without including exception text or request content."""
    record = {
        "provider": provider,
        "model": model,
        "stage": stage,
        "status": "error",
        "attempt": max(1, int(attempt)),
        "retry_count": max(0, int(attempt) - 1),
        "error_type": type(exc).__name__,
    }
    log.warning("API usage %s", json.dumps(record, separators=(",", ":"), sort_keys=True))
