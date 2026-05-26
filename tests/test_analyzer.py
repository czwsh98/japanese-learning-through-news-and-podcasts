import os
import pytest
from unittest.mock import patch, MagicMock

# Set mock environment variable for tests
os.environ["OPENAI_API_KEY"] = "mock-openai-key"

from lib.analyzer import explain_sentence

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
    
@patch('lib.analyzer.OpenAI')
def test_explain_sentence_error(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.side_effect = Exception("API Error")
    
    result = explain_sentence("これはテストです。")
    
    assert "Error: API Error" in result
