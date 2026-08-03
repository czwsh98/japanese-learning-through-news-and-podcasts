import os
import pytest
from unittest.mock import patch, MagicMock

# Set mock environment variable for tests
os.environ["OPENAI_API_KEY"] = "mock-openai-key"
os.environ["DEEPSEEK_API_KEY"] = "mock-deepseek-key"

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
