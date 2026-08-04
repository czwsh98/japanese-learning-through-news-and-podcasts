import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import transcriber


class CaptionFastPathTests(unittest.TestCase):
    def test_current_youtube_transcript_api_shape(self):
        snippets = [
            types.SimpleNamespace(text="これはテストです。", start=0.5, duration=2.0),
            types.SimpleNamespace(text="どんどんどんどん進みます。", start=2.5, duration=3.0),
        ]

        class FakeApi:
            def fetch(self, video_id, languages):
                self.args = (video_id, languages)
                return snippets

        module = types.ModuleType("youtube_transcript_api")
        module.YouTubeTranscriptApi = FakeApi
        with patch.dict(sys.modules, {"youtube_transcript_api": module}):
            result = transcriber.fetch_youtube_transcript("video123")

        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0]["start"], 0.5)
        self.assertEqual(result["segments"][1]["end"], 5.5)
        self.assertEqual(result["segments"][1]["ja"], "どんどんどんどん進みます。")


class ParallelChunkTests(unittest.TestCase):
    def test_parallel_responses_are_merged_in_source_order(self):
        class Transcriptions:
            def create(self, *, file, **_kwargs):
                index = int(Path(file.name).stem.split("_")[-1])
                text = f"チャンク{index}の文章です。"
                segment = types.SimpleNamespace(
                    start=0.0, end=1.0, text=text,
                )
                word = types.SimpleNamespace(start=0.0, end=1.0, word=text)
                return types.SimpleNamespace(
                    language="ja", text=text, duration=10.0,
                    segments=[segment], words=[word],
                )

        client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=Transcriptions())
        )

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"audio")
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "source.mp3"
            audio.write_bytes(b"source")
            with patch.object(transcriber, "_audio_bitrate_bps", return_value=64_000), \
                 patch.object(transcriber, "get_audio_duration_seconds", return_value=3_010.0), \
                 patch.object(transcriber.subprocess, "run", side_effect=fake_run):
                result = transcriber._transcribe_api_chunked(
                    client, audio, transcriber._API_LIMIT + 1,
                )

        self.assertEqual([s["ja"] for s in result["segments"]], [
            "チャンク0の文章です。", "チャンク1の文章です。",
        ])
        self.assertEqual(result["segments"][1]["start"], 3002.0)
        self.assertEqual(result["duration"], 3_010.0)

    def test_overlap_prefers_boundary_segment_with_following_context(self):
        class Transcriptions:
            def create(self, *, file, **_kwargs):
                index = int(Path(file.name).stem.split("_")[-1])
                if index == 0:
                    segment = types.SimpleNamespace(start=3001.0, end=3003.5, text="前半だけ")
                else:
                    segment = types.SimpleNamespace(start=0.0, end=2.0, text="境界をまたぐ完全な文です。")
                return types.SimpleNamespace(
                    language="ja", text=segment.text, duration=10.0,
                    segments=[segment], words=[],
                )

        client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=Transcriptions())
        )

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"audio")
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "source.mp3"
            audio.write_bytes(b"source")
            with patch.object(transcriber, "_audio_bitrate_bps", return_value=64_000), \
                 patch.object(transcriber, "get_audio_duration_seconds", return_value=3_010.0), \
                 patch.object(transcriber.subprocess, "run", side_effect=fake_run):
                result = transcriber._transcribe_api_chunked(
                    client, audio, transcriber._API_LIMIT + 1,
                )

        self.assertEqual([segment["ja"] for segment in result["segments"]], [
            "境界をまたぐ完全な文です。",
        ])


if __name__ == "__main__":
    unittest.main()
