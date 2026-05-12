"""Translate Japanese segments to EN + ZH via Google Gemini Flash."""
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from google import genai

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
_BATCH   = 50   # segments per Gemini call (large context window — 50 is comfortable)
_MAX_WORKERS = 4  # concurrent Gemini requests
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds

_TR_LOCK = threading.Lock()

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "en":    {"type": "string"},
                    "zh":    {"type": "string"},
                },
                "required": ["index", "en", "zh"],
            },
        }
    },
    "required": ["translations"],
}

_SYSTEM = (
    "You are a professional Japanese translator. "
    "Translate each segment naturally, preserving nuance, tone, and register. "
    "For 'en': idiomatic English. For 'zh': simplified Mandarin Chinese (普通话/简体). "
    "Return every segment in the same order with its original index."
)


def translate_segments(raw_segments: list[dict]) -> list[dict]:
    """Add 'time', 'en', 'zh' keys to each segment. Returns merged list."""
    if not _API_KEY:
        log.error("GEMINI_API_KEY is not set — returning empty translations")
        return [{**s, "time": _fmt(s["start"]), "en": "", "zh": ""} for s in raw_segments]

    client = genai.Client(api_key=_API_KEY)

    # Index → {en, zh} lookup built from batched Gemini responses
    tr_map: dict[int, dict] = {}

    # Build batch list
    batches = []
    for i in range(0, len(raw_segments), _BATCH):
        batches.append(raw_segments[i : i + _BATCH])

    if len(batches) <= 1:
        # Single batch — no need for threading overhead
        for batch in batches:
            _translate_batch(client, batch, tr_map)
        log.info(f"Translated {len(raw_segments)}/{len(raw_segments)} segments")
    else:
        # Multiple batches — run concurrently
        log.info(f"Translating {len(raw_segments)} segments in {len(batches)} batches ({_MAX_WORKERS} workers)")
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_translate_batch, client, batch, tr_map): idx
                for idx, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                batch_idx = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log.error(f"Translation batch {batch_idx + 1} failed: {exc}")
                done_count = min((batch_idx + 1) * _BATCH, len(raw_segments))
                log.info(f"Translated batch {batch_idx + 1}/{len(batches)} (up to segment {done_count})")

    merged = []
    for orig in raw_segments:
        idx = orig["index"]
        tr  = tr_map.get(idx, {"en": "", "zh": ""})
        merged.append({**orig, "time": _fmt(orig["start"]), "en": tr["en"], "zh": tr["zh"]})

    return merged


def _translate_batch(client: genai.Client, batch: list[dict], tr_map: dict) -> None:
    """Translate one batch with exponential backoff; populate tr_map."""
    payload = [{"index": s["index"], "ja": s["ja"]} for s in batch]
    prompt  = (
        f"Translate ALL {len(batch)} Japanese segments below into English (en) "
        f"and Simplified Chinese (zh). Return exactly {len(batch)} items preserving the index.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config={
                    "system_instruction": _SYSTEM,
                    "response_mime_type": "application/json",
                    "response_schema":    _RESPONSE_SCHEMA,
                },
            )
            data = json.loads(response.text)
            for item in data.get("translations", []):
                if isinstance(item, dict) and "index" in item:
                    with _TR_LOCK:
                        tr_map[item["index"]] = {"en": item.get("en", ""), "zh": item.get("zh", "")}
            return

        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(f"Gemini batch attempt {attempt + 1} failed: {exc} — retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                log.error(f"Gemini batch attempt {attempt + 1} failed: {exc} — padding with blanks")
                for s in batch:
                    with _TR_LOCK:
                        tr_map.setdefault(s["index"], {"en": "", "zh": ""})


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
