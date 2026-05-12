import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import json

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.translator import translate_segments

def test_translate_segments_basic():
    """Test translation with mocked Gemini API."""
    raw_segments = [
        {"index": 0, "start": 0.0, "end": 2.0, "ja": "こんにちは"},
        {"index": 1, "start": 2.0, "end": 4.0, "ja": "元気ですか？"}
    ]
    
    # Mock response from Gemini
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "translations": [
            {"index": 0, "en": "Hello", "zh": "你好"},
            {"index": 1, "en": "How are you?", "zh": "你好吗？"}
        ]
    })
    
    with patch("google.genai.Client") as MockClient:
        instance = MockClient.return_value
        instance.models.generate_content.return_value = mock_response
        
        # We need to mock the environment variable to avoid early exit in some configurations
        # though lib/translator.py handles missing API keys by returning empty strings.
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            results = translate_segments(raw_segments)
            
    assert len(results) == 2
    assert results[0]["en"] == "Hello"
    assert results[0]["zh"] == "你好"
    assert results[0]["time"] == "00:00:00"
    assert results[1]["en"] == "How are you?"
    assert results[1]["zh"] == "你好吗？"
    assert results[1]["time"] == "00:00:02"

def test_translate_segments_empty_api_key():
    """Test behavior when API key is missing."""
    raw_segments = [{"index": 0, "start": 0.0, "end": 2.0, "ja": "こんにちは"}]
    
    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=True):
        # We also need to patch the internal _API_KEY which might have been loaded at import time
        with patch("lib.translator._API_KEY", ""):
            results = translate_segments(raw_segments)
            
    assert len(results) == 1
    assert results[0]["en"] == ""
    assert results[0]["zh"] == ""

def test_translate_segments_batching():
    """Test if segments are correctly batched (mocking _BATCH to a small number)."""
    raw_segments = [{"index": i, "start": float(i), "end": float(i+1), "ja": f"Text {i}"} for i in range(5)]
    
    mock_response = MagicMock()
    # Simplified mock that returns what it's given
    def side_effect(model, contents, config):
        # Extract indices from prompt (this is a bit hacky but works for testing batching)
        # The prompt contains the JSON payload
        import re
        indices = [int(x) for x in re.findall(r'"index": (\d+)', contents)]
        return MagicMock(text=json.dumps({
            "translations": [{"index": i, "en": f"EN {i}", "zh": f"ZH {i}"} for i in indices]
        }))

    with patch("google.genai.Client") as MockClient:
        instance = MockClient.return_value
        instance.models.generate_content.side_effect = side_effect
        
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            # Patch _BATCH to 2 so we get 3 batches for 5 segments
            with patch("lib.translator._BATCH", 2):
                results = translate_segments(raw_segments)
                
    assert len(results) == 5
    for i in range(5):
        assert results[i]["en"] == f"EN {i}"
        assert results[i]["index"] == i
