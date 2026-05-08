"""Transcribe Japanese audio via local mlx-whisper (default) or OpenAI API.

Default: OpenAI Whisper API. Set USE_LOCAL_WHISPER=1 to use local mlx-whisper instead.
Set MLX_WHISPER_MODEL to override the local model (default: mlx-community/whisper-large-v3-mlx).

Files larger than the API's 25 MB limit are automatically split into chunks,
transcribed in parallel, and merged — the caller receives a single seamless result.
"""
import collections
import os
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_USE_API = os.environ.get("USE_LOCAL_WHISPER", "").strip() in ("", "0")
_MLX_MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx")

_MIN_CHARS = 3
_LOOP_WINDOW = 4
_API_LIMIT = 23 * 1024 * 1024   # 23 MB — leave 2 MB headroom below the 25 MB cap


def transcribe_audio(audio_path: Path) -> dict:
    """Return dict with keys: language, duration, text, segments."""
    if _USE_API:
        return _transcribe_api(audio_path)
    return _transcribe_local(audio_path)


# ── Hallucination filter (used by both backends) ──────────────────────────────

def _has_repeating_phrase(text: str) -> bool:
    """True if any 4–20 char substring appears 3+ times (phrase-loop hallucination)."""
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
    """Drop hallucinated / garbage segments. Four-layer filter:
    1. Empty / too-short  2. Single-char dominance  3. Phrase loop  4. Cross-segment loop
    """
    cleaned: list[dict] = []
    recent: collections.deque[str] = collections.deque(maxlen=_LOOP_WINDOW)

    for seg in raw:
        text = seg["ja"].strip("　 \t\n\r")
        if not text or len(text) < _MIN_CHARS:
            continue
        if len(text) >= 6 and max(text.count(c) for c in set(text)) / len(text) > 0.60:
            log.warning(f"Dropping char-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue
        if _has_repeating_phrase(text):
            log.warning(f"Dropping phrase-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue
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


# ── OpenAI Whisper API ────────────────────────────────────────────────────────

def _transcribe_api(audio_path: Path) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    size = audio_path.stat().st_size
    log.info(f"Transcribing {audio_path.name} ({size // 1024:,} KB) via OpenAI API ...")

    if size > _API_LIMIT:
        return _transcribe_api_chunked(client, audio_path, size)
    return _transcribe_api_single(client, audio_path)


def _transcribe_api_single(client, audio_path: Path) -> dict:
    with open(audio_path, "rb") as fh:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=fh,
            language="ja",
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    raw = [
        {
            "index": seg.id,
            "start": round(float(seg.start), 3),
            "end":   round(float(seg.end),   3),
            "ja":    seg.text.strip(),
        }
        for seg in response.segments
    ]
    segments = _clean_segments(raw)
    log.info(f"Transcription complete: {len(segments)} segments, duration {response.duration:.1f}s")
    return {
        "language": response.language,
        "duration": float(response.duration),
        "text":     response.text,
        "segments": segments,
    }


def _audio_bitrate_bps(audio_path: Path) -> int:
    """Return audio bitrate in bits/sec via ffprobe, or 192 kbps as fallback."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=bit_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    try:
        bps = int(result.stdout.strip())
        if bps > 0:
            return bps
    except ValueError:
        pass
    log.warning("ffprobe bitrate detection failed — assuming 192 kbps")
    return 192_000


def _transcribe_api_chunked(client, audio_path: Path, size: int) -> dict:
    """Split audio into chunks sized to stay under _API_LIMIT, transcribe each, merge."""
    bitrate = _audio_bitrate_bps(audio_path)
    chunk_secs = int(_API_LIMIT / (bitrate / 8))
    n_chunks = -(-size // _API_LIMIT)   # ceiling estimate for logging
    log.info(
        f"File is {size // 1_048_576} MB at {bitrate // 1000} kbps — "
        f"splitting into ~{n_chunks} chunks of {chunk_secs}s each"
    )

    with tempfile.TemporaryDirectory(prefix="whisper_chunks_") as tmp:
        chunk_pattern = str(Path(tmp) / "chunk_%03d.mp3")

        # Split with ffmpeg — copy stream (no re-encode, fast, lossless)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-f", "segment",
            "-segment_time", str(chunk_secs),
            "-c", "copy",
            "-y", chunk_pattern,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg split failed: {result.stderr.strip()}")

        chunks = sorted(Path(tmp).glob("chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("ffmpeg produced no chunks")

        log.info(f"Split into {len(chunks)} chunk(s), transcribing ...")

        all_raw: list[dict] = []
        offset = 0.0
        full_text_parts: list[str] = []
        language = "ja"
        total_duration = 0.0

        for i, chunk_path in enumerate(chunks):
            log.info(f"  Chunk {i+1}/{len(chunks)} (offset {offset:.0f}s) ...")
            with open(chunk_path, "rb") as fh:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=fh,
                    language="ja",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )

            language = response.language
            full_text_parts.append(response.text)
            chunk_duration = float(response.duration)

            for seg in response.segments:
                all_raw.append({
                    "index": len(all_raw),
                    "start": round(float(seg.start) + offset, 3),
                    "end":   round(float(seg.end)   + offset, 3),
                    "ja":    seg.text.strip(),
                })

            offset += chunk_duration
            total_duration = offset

        segments = _clean_segments(all_raw)
        log.info(
            f"Transcription complete: {len(segments)} segments across "
            f"{len(chunks)} chunks, total duration {total_duration:.1f}s"
        )
        return {
            "language": language,
            "duration": total_duration,
            "text":     " ".join(full_text_parts),
            "segments": segments,
        }


# ── Local mlx-whisper ─────────────────────────────────────────────────────────

def _transcribe_local(audio_path: Path) -> dict:
    import mlx_whisper

    log.info(f"Transcribing {audio_path.name} locally with {_MLX_MODEL} ...")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=_MLX_MODEL,
        language="ja",
        word_timestamps=False,
        no_speech_threshold=0.5,
        hallucination_silence_threshold=2.0,
        condition_on_previous_text=False,
        compression_ratio_threshold=2.2,
    )

    raw = [
        {
            "index": i,
            "start": round(float(seg["start"]), 3),
            "end":   round(float(seg["end"]),   3),
            "ja":    seg["text"].strip(),
        }
        for i, seg in enumerate(result.get("segments", []))
    ]

    segments = _clean_segments(raw)
    duration = segments[-1]["end"] if segments else 0.0
    log.info(f"Transcription complete: {len(segments)} segments, duration ~{duration:.1f}s")
    return {
        "language": result.get("language", "ja"),
        "duration": duration,
        "text":     result.get("text", ""),
        "segments": segments,
    }
