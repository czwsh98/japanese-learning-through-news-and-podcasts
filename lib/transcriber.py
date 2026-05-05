"""Transcribe Japanese audio via local mlx-whisper (default) or OpenAI API.

Set USE_OPENAI_WHISPER=1 in the environment to fall back to the OpenAI API.
Set MLX_WHISPER_MODEL to override the local model (default: mlx-community/whisper-large-v3-mlx).
"""
import collections
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_USE_API = os.environ.get("USE_OPENAI_WHISPER", "").strip() not in ("", "0")
_MLX_MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx")

_MIN_CHARS = 3      # minimum meaningful characters
_LOOP_WINDOW = 4    # recent kept segments to check for cross-segment loops


def transcribe_audio(audio_path: Path) -> dict:
    """Return dict with keys: language, duration, text, segments."""
    if _USE_API:
        return _transcribe_api(audio_path)
    return _transcribe_local(audio_path)


def _has_repeating_phrase(text: str) -> bool:
    """Return True if any substring of length 4-20 chars appears 3+ times.

    Catches intra-segment phrase loops like:
      トイレトリー店のトイレトリー店のトイレトリー店のトイレトリー店の
    """
    n = len(text)
    for length in range(4, min(21, n // 3 + 1)):
        counts: dict[str, int] = {}
        for i in range(n - length + 1):
            gram = text[i:i + length]
            counts[gram] = counts.get(gram, 0) + 1
            if counts[gram] >= 3:
                return True
    return False


def _clean_segments(raw: list[dict]) -> list[dict]:
    """Drop hallucinated / garbage segments from Whisper output.

    Four-layer filter:
    1. Empty / too-short text (< _MIN_CHARS chars)
    2. Single-character dominance — one char > 60% of text (トトトト…)
    3. Multi-character phrase loop within one segment (トイレトリー店の…)
    4. Cross-segment loop — same text seen in the last _LOOP_WINDOW kept
       segments (catches alternating loops like 渡辺知事は / 渡辺知事の声援…)
    """
    cleaned: list[dict] = []
    recent: collections.deque[str] = collections.deque(maxlen=_LOOP_WINDOW)

    for seg in raw:
        text = seg["ja"].strip("　 \t\n\r")

        if not text:
            continue

        if len(text) < _MIN_CHARS:
            log.debug(f"Dropping short segment [{seg['start']:.1f}s]: {text!r}")
            continue

        # Single-char dominance (e.g. トトトトト…)
        if len(text) >= 6 and max(text.count(c) for c in set(text)) / len(text) > 0.60:
            log.warning(f"Dropping char-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue

        # Multi-char phrase loop within one segment
        if _has_repeating_phrase(text):
            log.warning(f"Dropping phrase-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue

        # Cross-segment loop: same text appeared among recent kept segments
        if text in recent:
            log.warning(f"Dropping cross-segment loop [{seg['start']:.1f}s]: {text!r}")
            continue

        recent.append(text)
        cleaned.append({**seg, "ja": text})

    for i, seg in enumerate(cleaned):
        seg["index"] = i

    dropped = len(raw) - len(cleaned)
    if dropped:
        log.info(f"Cleaned {dropped} hallucinated segment(s) ({len(cleaned)} kept)")
    return cleaned


def _transcribe_local(audio_path: Path) -> dict:
    import mlx_whisper

    log.info(f"Transcribing {audio_path.name} locally with {_MLX_MODEL} ...")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=_MLX_MODEL,
        language="ja",
        word_timestamps=False,
        # Suppress non-speech segments at the model level
        no_speech_threshold=0.5,
        # Flag and skip silent stretches that trigger hallucination loops
        hallucination_silence_threshold=2.0,
        # Disabling context conditioning reduces hallucination chains
        condition_on_previous_text=False,
        # Stay close to the default (2.4) so speaker/acoustic changes aren't
        # mistaken for hallucinations; _clean_segments catches the rest
        compression_ratio_threshold=2.2,
    )

    raw_segments = [
        {
            "index": i,
            "start": round(float(seg["start"]), 3),
            "end": round(float(seg["end"]), 3),
            "ja": seg["text"].strip(),
        }
        for i, seg in enumerate(result.get("segments", []))
    ]

    segments = _clean_segments(raw_segments)
    duration = segments[-1]["end"] if segments else 0.0
    log.info(f"Transcription complete: {len(segments)} segments, duration ~{duration:.1f}s")
    return {
        "language": result.get("language", "ja"),
        "duration": duration,
        "text": result.get("text", ""),
        "segments": segments,
    }


def _transcribe_api(audio_path: Path) -> dict:
    from openai import OpenAI

    _WARN_SIZE = 24 * 1024 * 1024
    size = audio_path.stat().st_size
    if size > _WARN_SIZE:
        log.warning(
            f"Audio is {size // 1_048_576} MB -- Whisper API limit is 25 MB. "
            "Consider splitting the file if transcription fails."
        )

    log.info(f"Transcribing {audio_path.name} ({size // 1024:,} KB) via OpenAI API ...")
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    with open(audio_path, "rb") as fh:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=fh,
            language="ja",
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    raw_segments = [
        {
            "index": seg.id,
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "ja": seg.text.strip(),
        }
        for seg in response.segments
    ]

    segments = _clean_segments(raw_segments)
    log.info(
        f"Transcription complete: {len(segments)} segments, "
        f"duration {response.duration:.1f}s, language={response.language}"
    )
    return {
        "language": response.language,
        "duration": float(response.duration),
        "text": response.text,
        "segments": segments,
    }
