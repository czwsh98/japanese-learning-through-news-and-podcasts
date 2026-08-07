import json
import sys
import types
import unittest
from unittest.mock import patch

# Keep this standard-library test runnable in lightweight development
# environments where the production OpenAI SDK is not installed.
if "openai" not in sys.modules:
    try:
        __import__("openai")
    except ImportError:
        openai_stub = types.ModuleType("openai")
        openai_stub.OpenAI = object
        sys.modules["openai"] = openai_stub

from lib import transcriber, translator


class TranscriptCleanupTests(unittest.TestCase):
    def test_common_phrase_repeated_nonconsecutively_is_not_a_loop(self):
        text = "これはそういう説明です。次もそういう話です。最後もそういう結論です。"
        self.assertFalse(transcriber._has_repeating_phrase(text))

    def test_consecutive_long_phrase_loop_is_detected(self):
        phrase = "同じ長いフレーズです。"
        self.assertTrue(transcriber._has_repeating_phrase("前置きです。" + phrase * 5))

    def test_short_and_repeated_utterances_are_preserved(self):
        raw = [
            {"index": 0, "start": 0.0, "end": 1.0, "ja": "はい"},
            {"index": 1, "start": 1.0, "end": 2.0, "ja": "大切な文章です。"},
            {"index": 2, "start": 2.0, "end": 3.0, "ja": "大切な文章です。"},
        ]
        cleaned = transcriber._clean_segments(raw)
        self.assertEqual([segment["ja"] for segment in cleaned], [
            "はい", "大切な文章です。", "大切な文章です。",
        ])
        self.assertEqual([segment["index"] for segment in cleaned], [0, 1, 2])

    def test_segment_stretch_is_capped_to_short_caption_lag(self):
        raw = [
            {"index": 0, "start": 0.0, "end": 2.0, "ja": "短い遅延"},
            {"index": 1, "start": 8.0, "end": 9.0, "ja": "次の字幕"},
            {"index": 2, "start": 25.0, "end": 26.0, "ja": "長い無音後"},
        ]
        cleaned = transcriber._clean_segments(raw)
        self.assertEqual(cleaned[0]["end"], 8.0)
        self.assertEqual(cleaned[1]["end"], 9.0)


class TranslationCompletenessTests(unittest.TestCase):
    def test_partial_response_retries_only_missing_indices(self):
        responses = [
            {"translations": [{"index": "0", "en": "zero", "zh": "零"}]},
            {"translations": [
                {"index": 1, "en": "one", "zh": "一"},
                {"index": 2, "en": "two", "zh": "二"},
            ]},
        ]
        requested_indices = []
        request_options = []

        class Completions:
            def create(self, **kwargs):
                request_options.append(kwargs)
                payload = json.loads(kwargs["messages"][1]["content"].split("\n\n", 1)[1])
                requested_indices.append([item["index"] for item in payload])
                body = responses.pop(0)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=json.dumps(body)),
                )])

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=Completions())
        )
        batch = [
            {"index": 0, "ja": "零"},
            {"index": 1, "ja": "一"},
            {"index": 2, "ja": "二"},
        ]
        translated = {}

        with patch.object(translator.time, "sleep"):
            translator._translate_batch(client, batch, translated)

        self.assertEqual(requested_indices, [[0, 1, 2], [1, 2]])
        self.assertEqual(set(translated), {0, 1, 2})
        self.assertTrue(all(translated[index]["en"] for index in translated))
        self.assertTrue(all(translated[index]["zh"] for index in translated))
        self.assertTrue(all(
            call["extra_body"] == {"thinking": {"type": "disabled"}}
            for call in request_options
        ))

    def test_failed_batches_cannot_publish_blank_translations(self):
        class Completions:
            def create(self, **kwargs):
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content='{"translations": []}'),
                )])

        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=Completions())
        )
        segments = [{"index": 0, "start": 0.0, "end": 1.0, "ja": "文です。"}]

        with patch.object(translator, "OpenAI", return_value=fake_client), \
             patch.object(translator.time, "sleep"):
            with self.assertRaises(translator.TranslationIncompleteError):
                translator.translate_segments(segments)


if __name__ == "__main__":
    unittest.main()
