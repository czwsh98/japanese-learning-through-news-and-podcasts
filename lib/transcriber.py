"""Transcribe Japanese audio via local mlx-whisper (default) or OpenAI API.

Default: OpenAI Whisper API. Set USE_LOCAL_WHISPER=1 to use local mlx-whisper instead.
Set MLX_WHISPER_MODEL to override the local model (default: mlx-community/whisper-large-v3-mlx).

Files larger than the API's 25 MB limit are automatically split into chunks,
transcribed in parallel, and merged — the caller receives a single seamless result.
"""
import os
import logging
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger(__name__)

_USE_API = os.environ.get("USE_LOCAL_WHISPER", "").strip() in ("", "0")
_MLX_MODEL = os.environ.get("MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx")

_MIN_CHARS = 1
_API_LIMIT = 23 * 1024 * 1024   # 23 MB — leave 2 MB headroom below the 25 MB cap
_API_AUDIO_BITRATE = os.environ.get("WHISPER_AUDIO_BITRATE", "64k")
_MAX_CHUNK_WORKERS = max(1, int(os.environ.get("WHISPER_CHUNK_WORKERS", "3")))
_CHUNK_OVERLAP_SECONDS = 2.0

# Maximum audio duration for non-unlimited users.  Whisper is billed per
# minute (~$0.006/min), so 30 min caps per-job spend at ~$0.18 of Whisper.
# Unlimited users (admin / whitelist) skip this check.
# Set MAX_AUDIO_MINUTES env var to override (e.g. "60" for longer content).
_MAX_AUDIO_MINUTES: float = float(os.environ.get("MAX_AUDIO_MINUTES", "30"))


def transcribe_audio(audio_path: Path) -> dict:
    """Return dict with keys: language, duration, text, segments."""
    if _USE_API:
        return _transcribe_api(audio_path)
    return _transcribe_local(audio_path)


# ── Hallucination filter (used by both backends) ──────────────────────────────

def _has_repeating_phrase(text: str) -> bool:
    """True only for a dominant *consecutive* phrase loop.

    Repeated common Japanese phrases in different sentences are normal. The old
    n-gram counter treated any three occurrences as a hallucination and deleted
    the entire segment, producing long holes in podcast transcripts.
    """
    n = len(text)
    for length in range(4, min(21, n // 3 + 1)):
        for start in range(n - (length * 3) + 1):
            phrase = text[start:start + length]
            repeats = 1
            position = start + length
            while text[position:position + length] == phrase:
                repeats += 1
                position += length
            repeated_chars = repeats * length
            if repeats >= 3 and repeated_chars >= max(12, int(n * 0.6)):
                return True
    return False


def _clean_segments(raw: list[dict]) -> list[dict]:
    """Drop only high-confidence hallucinated / garbage segments.

    Exact repeated segments and short acknowledgements are valid dialogue and
    must be preserved. The phrase-loop check is intentionally conservative.
    """
    cleaned: list[dict] = []

    for seg in raw:
        text = seg["ja"].strip("　 \t\n\r")
        if not text or len(text) < _MIN_CHARS:
            continue
        if len(text) >= 6 and max(text.count(c) for c in set(text)) / len(text) > 0.80:
            log.warning(f"Dropping char-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue
        if _has_repeating_phrase(text):
            log.warning(f"Dropping phrase-loop [{seg['start']:.1f}s]: {text[:40]!r}")
            continue
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
        # Playback audio is often encoded at 128–320 kbps, which wastes upload
        # time and can turn a normal podcast into multiple sequential API calls.
        # Keep the original for playback and make a temporary speech-optimised
        # mono copy solely for transcription.
        with tempfile.TemporaryDirectory(prefix="whisper_audio_") as tmp:
            compact_path = Path(tmp) / "audio.mp3"
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
                "-b:a", _API_AUDIO_BITRATE, "-y", str(compact_path),
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                compact_size = compact_path.stat().st_size
                log.info(
                    "Prepared compact transcription audio: %.1f MB → %.1f MB",
                    size / 1_048_576,
                    compact_size / 1_048_576,
                )
                if compact_size <= _API_LIMIT:
                    return _transcribe_api_single(client, compact_path)
                return _transcribe_api_chunked(client, compact_path, compact_size)
            except (subprocess.CalledProcessError, OSError) as exc:
                log.warning("Compact transcription audio failed (%s); using original", exc)
                return _transcribe_api_chunked(client, audio_path, size)
    return _transcribe_api_single(client, audio_path)


def _transcribe_api_single(client, audio_path: Path) -> dict:
    with open(audio_path, "rb") as fh:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=fh,
            language="ja",
            response_format="verbose_json",
            timestamp_granularities=["segment", "word"],
        )
    words = [
        {"start": round(float(word.start), 3), "end": round(float(word.end), 3),
         "word": word.word}
        for word in (getattr(response, "words", []) or [])
    ]
    raw = [
        {
            "index": seg.id,
            "start": round(float(seg.start), 3),
            "end":   round(float(seg.end),   3),
            "ja":    seg.text.strip(),
            "words": [word for word in words
                      if word["end"] > float(seg.start) and word["start"] < float(seg.end)],
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


def get_audio_duration_seconds(audio_path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or -1.0 on failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    try:
        secs = float(result.stdout.strip())
        if secs > 0:
            return secs
    except (ValueError, TypeError):
        pass
    # Fallback: try format-level duration (covers formats where stream duration is N/A)
    result2 = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    try:
        secs = float(result2.stdout.strip())
        if secs > 0:
            return secs
    except (ValueError, TypeError):
        pass
    log.warning("ffprobe duration detection failed for %s", audio_path)
    return -1.0


def check_audio_duration(audio_path: Path, unlimited: bool = False) -> None:
    """Raise RuntimeError if the audio exceeds _MAX_AUDIO_MINUTES for limited users.

    Call this after the audio file is available (download or upload).
    Unlimited users (admin / whitelist) always pass through.
    """
    if unlimited:
        return
    duration_s = get_audio_duration_seconds(audio_path)
    if duration_s < 0:
        log.warning("Could not determine audio duration — skipping duration cap")
        return
    limit_s = _MAX_AUDIO_MINUTES * 60
    if duration_s > limit_s:
        minutes = int(duration_s // 60)
        seconds = int(duration_s % 60)
        limit_m = int(_MAX_AUDIO_MINUTES)
        raise RuntimeError(
            f"Audio is {minutes}:{seconds:02d} — only files under {limit_m} minutes "
            "are supported. Contact the admin if you need longer content."
        )


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
    """Transcribe overlapping chunks and reconcile their shared boundaries."""
    bitrate = _audio_bitrate_bps(audio_path)
    # Leave room for the overlap and container overhead below the API byte cap.
    chunk_secs = max(30, int(_API_LIMIT / (bitrate / 8)) - 10)
    step_secs = chunk_secs - _CHUNK_OVERLAP_SECONDS
    detected_duration = get_audio_duration_seconds(audio_path)
    total_source_duration = (
        detected_duration if detected_duration > 0 else size / (bitrate / 8)
    )
    n_chunks = -(-size // _API_LIMIT)   # ceiling estimate for logging
    log.info(
        f"File is {size // 1_048_576} MB at {bitrate // 1000} kbps — "
        f"splitting into ~{n_chunks} chunks of {chunk_secs}s each"
    )

    with tempfile.TemporaryDirectory(prefix="whisper_chunks_") as tmp:
        chunks: list[Path] = []
        chunk_starts: list[float] = []
        start = 0.0
        while start < total_source_duration:
            output = Path(tmp) / f"chunk_{len(chunks):03d}.mp3"
            duration = min(float(chunk_secs), total_source_duration - start)
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-i", str(audio_path),
                "-t", str(duration), "-c", "copy", "-y", str(output),
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"ffmpeg split failed: {e.stderr.strip()}")
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(f"ffmpeg did not produce chunk {len(chunks) + 1}")
            chunks.append(output)
            chunk_starts.append(start)
            start += step_secs

        if not chunks:
            raise RuntimeError("ffmpeg produced no chunks")

        log.info(f"Split into {len(chunks)} chunk(s), transcribing ...")

        all_raw: list[dict] = []
        language = "ja"

        def _request_chunk(i: int, chunk_path: Path):
            log.info("  Chunk %d/%d ...", i + 1, len(chunks))
            with open(chunk_path, "rb") as fh:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=fh,
                    language="ja",
                    response_format="verbose_json",
                    timestamp_granularities=["segment", "word"],
                )
            return i, response

        workers = min(_MAX_CHUNK_WORKERS, len(chunks))
        responses: dict[int, object] = {}
        if workers == 1:
            for i, chunk_path in enumerate(chunks):
                chunk_idx, response = _request_chunk(i, chunk_path)
                responses[chunk_idx] = response
        else:
            log.info("Transcribing %d chunks concurrently (%d workers)", len(chunks), workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_request_chunk, i, chunk_path)
                    for i, chunk_path in enumerate(chunks)
                ]
                for future in as_completed(futures):
                    chunk_idx, response = future.result()
                    responses[chunk_idx] = response

        # Merge in source order even though requests finish out of order.
        for i in range(len(chunks)):
            response = responses[i]
            language = response.language
            offset = chunk_starts[i]
            chunk_words = [
                {"start": round(float(word.start) + offset, 3),
                 "end": round(float(word.end) + offset, 3), "word": word.word}
                for word in (getattr(response, "words", []) or [])
            ]

            boundary = offset + (_CHUNK_OVERLAP_SECONDS / 2) if i else None
            incoming = []
            for seg in response.segments:
                item = {
                    "index": len(all_raw),
                    "start": round(float(seg.start) + offset, 3),
                    "end":   round(float(seg.end)   + offset, 3),
                    "ja":    seg.text.strip(),
                    "words": [word for word in chunk_words
                              if word["end"] > float(seg.start) + offset
                              and word["start"] < float(seg.end) + offset],
                }
                midpoint = (item["start"] + item["end"]) / 2
                spans_boundary = bool(boundary is not None and
                                      item["start"] < boundary <= item["end"])
                if boundary is None or midpoint >= boundary or spans_boundary:
                    incoming.append(item)
            if boundary is not None and incoming:
                all_raw = [previous for previous in all_raw if not any(
                    previous["start"] < item["end"] and item["start"] < previous["end"]
                    for item in incoming
                )]
            all_raw.extend(incoming)

        segments = _clean_segments(all_raw)
        log.info(
            f"Transcription complete: {len(segments)} segments across "
            f"{len(chunks)} chunks, total duration {total_source_duration:.1f}s"
        )
        return {
            "language": language,
            "duration": total_source_duration,
            "text":     " ".join(segment["ja"] for segment in segments),
            "segments": segments,
        }


# ── YouTube transcript API ────────────────────────────────────────────────────

def fetch_youtube_transcript(video_id: str) -> dict | None:
    """Fetch YouTube captions via transcript API. Returns whisper-format dict or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        log.info(f"Fetching YouTube captions for {video_id} ...")
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            snippets = api.fetch(video_id, languages=["ja"])
        else:  # youtube-transcript-api < 1.0 compatibility
            snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=["ja"])

        def _field(snippet, name: str):
            if isinstance(snippet, dict):
                return snippet[name]
            return getattr(snippet, name)

        raw = [
            {
                "index": i,
                "start": round(float(_field(s, "start")), 3),
                "end":   round(float(_field(s, "start")) + float(_field(s, "duration")), 3),
                "ja":    str(_field(s, "text")).strip().replace("\n", " "),
            }
            for i, s in enumerate(snippets)
        ]
        # Platform captions can legitimately contain repeated phrases such as
        # 「どんどん」 or 「はい、はい」. The aggressive Whisper hallucination
        # filters would drop those, so captions only need basic empty/short-line
        # cleanup and stable reindexing.
        segments = []
        for seg in raw:
            text = seg["ja"].strip("　 \t\n\r")
            if len(text) < _MIN_CHARS:
                continue
            segments.append({**seg, "index": len(segments), "ja": text})
        if not segments:
            log.warning("YouTube captions empty after cleaning — falling back to Whisper")
            return None
        duration = segments[-1]["end"]
        log.info(f"YouTube captions: {len(segments)} segments, {duration:.1f}s")
        return {
            "language": "ja",
            "duration": duration,
            "text": " ".join(s["ja"] for s in segments),
            "segments": segments,
        }
    except Exception as exc:
        log.warning(f"YouTube captions unavailable ({exc}) — falling back to Whisper")
        return None


# ── Local mlx-whisper ─────────────────────────────────────────────────────────

def _transcribe_local(audio_path: Path) -> dict:
    import mlx_whisper

    log.info(f"Transcribing {audio_path.name} locally with {_MLX_MODEL} ...")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=_MLX_MODEL,
        language="ja",
        word_timestamps=True,
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
            "words": [
                {"start": round(float(word["start"]), 3),
                 "end": round(float(word["end"]), 3), "word": word.get("word", "")}
                for word in seg.get("words", [])
                if word.get("start") is not None and word.get("end") is not None
            ],
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
