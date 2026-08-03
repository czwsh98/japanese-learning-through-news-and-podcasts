import logging
from types import SimpleNamespace

from lib.api_usage import record_chat_failure, record_chat_usage


def test_record_chat_usage_tracks_deepseek_cache_and_cost(caplog):
    response = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=125,
        completion_tokens=40,
        prompt_cache_hit_tokens=25,
        prompt_cache_miss_tokens=100,
    ))

    with caplog.at_level(logging.INFO):
        result = record_chat_usage(
            response,
            provider="deepseek",
            model="deepseek-v4-flash",
            stage="translation",
            attempt=2,
        )

    assert result["cached_input_tokens"] == 25
    assert result["uncached_input_tokens"] == 100
    assert result["output_tokens"] == 40
    assert result["retry_count"] == 1
    assert result["estimated_cost_usd"] == 0.00002527
    assert "API usage" in caplog.text


def test_record_chat_failure_excludes_exception_message(caplog):
    with caplog.at_level(logging.WARNING):
        record_chat_failure(
            provider="deepseek",
            model="deepseek-v4-flash",
            stage="grammar_analysis",
            attempt=1,
            exc=RuntimeError("potentially sensitive provider response"),
        )

    assert "RuntimeError" in caplog.text
    assert "potentially sensitive" not in caplog.text
