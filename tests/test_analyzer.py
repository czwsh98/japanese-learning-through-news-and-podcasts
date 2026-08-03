import os
import json
import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Set mock environment variable for tests
os.environ["OPENAI_API_KEY"] = "mock-openai-key"
os.environ["DEEPSEEK_API_KEY"] = "mock-deepseek-key"

from lib import analyzer
from lib.analyzer import explain_sentence, explanation_cache_key

@patch('lib.analyzer.OpenAI')
def test_explain_sentence(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Grammar explanation here."
    mock_client.chat.completions.create.return_value = mock_response
    
    result = explain_sentence("これはテストです。")
    
    assert result == "Grammar explanation here."
    mock_client.chat.completions.create.assert_called_once()
    assert mock_client.chat.completions.create.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    mock_openai_class.assert_called_once_with(
        api_key="mock-deepseek-key", base_url="https://api.deepseek.com"
    )
    
@patch('lib.analyzer.OpenAI')
def test_explain_sentence_error(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.side_effect = Exception("API Error")
    
    result = explain_sentence("これはテストです。")
    
    assert "Error: API Error" in result


def test_explanation_cache_key_is_stable_and_content_sensitive():
    assert explanation_cache_key(" これはテストです。 ") == explanation_cache_key("これはテストです。")
    assert explanation_cache_key("これはテストです。") != explanation_cache_key("別の文です。")


def test_vocab_curation_disables_thinking_for_required_tool_call():
    tool_call = SimpleNamespace(function=SimpleNamespace(
        name="pick_vocab", arguments=json.dumps({"picked": []}),
    ))
    response = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))],
    )
    client = MagicMock()
    client.chat.completions.create.return_value = response
    candidates = [{
        "word": "語彙", "reading": "ごい", "level": "N2", "count": 1,
        "en": "vocabulary", "example": "語彙を学ぶ", "surfaces": ["語彙"],
    }]

    assert analyzer._curate_vocab(client, candidates, ["N2"]) == []
    assert client.chat.completions.create.call_args.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


def test_analysis_failure_raises_instead_of_returning_empty_sidebar():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("provider failed")

    with patch.object(analyzer.time, "sleep"):
        with pytest.raises(analyzer.AnalysisIncompleteError):
            analyzer._analyze_chunk(client, "system", ["N2"], "文です。", 0, 1)


def test_merge_drops_incomplete_sidebar_items():
    merged = analyzer._merge_analyses([{
        "highlights": [
            {"word": "完全", "en": "complete", "zh": "完整"},
            {"word": "不完全", "en": "", "zh": "不完整"},
        ],
        "grammar": [
            {"pattern": "〜ながら", "meaning_en": "while", "meaning_zh": "一边"},
            {"pattern": "〜はもちろん", "meaning_en": "", "meaning_zh": ""},
        ],
        "expressions": [
            {"expression": "念のため", "en": "just in case", "zh": "以防万一"},
            {"expression": "空", "en": "", "zh": ""},
        ],
    }])

    assert [item["word"] for item in merged["highlights"]] == ["完全"]
    assert [item["pattern"] for item in merged["grammar"]] == ["〜ながら"]
    assert [item["expression"] for item in merged["expressions"]] == ["念のため"]

    sanitized = analyzer.sanitize_analysis_result({
        **merged,
        "vocab": [
            {"word": "語彙", "en": "vocabulary", "zh": "词汇"},
            {"word": "空", "en": "", "zh": "空"},
        ],
    })
    assert [item["word"] for item in sanitized["vocab"]] == ["語彙"]
