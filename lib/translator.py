"""Translate Japanese segments to EN + ZH via DeepSeek."""
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

log = logging.getLogger(__name__)

_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
_BATCH = max(1, int(os.environ.get("TRANSLATION_BATCH_SIZE", "50")))
_MAX_WORKERS = max(1, int(os.environ.get("TRANSLATION_WORKERS", "4")))
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds

_TR_LOCK = threading.Lock()

_SYSTEM = (
    "You are a professional Japanese translator. "
    "Translate each segment naturally, preserving nuance, tone, and register. "
    "For 'en': idiomatic English. For 'zh': simplified Mandarin Chinese (普通话/简体). "
    "Return a JSON object with a 'translations' array. Each item must have: "
    "'index' (integer, same as input), 'en' (English translation), 'zh' (Chinese translation). "
    "Return EVERY segment preserving the original index."
)


def translate_segments(raw_segments: list[dict]) -> list[dict]:
    """Add 'time', 'en', 'zh' keys to each segment. Returns merged list."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        log.error("DEEPSEEK_API_KEY is not set — returning empty translations")
        return [{**s, "time": _fmt(s["start"]), "en": "", "zh": ""} for s in raw_segments]

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    tr_map: dict[int, dict] = {}
    batches = [raw_segments[i:i + _BATCH] for i in range(0, len(raw_segments), _BATCH)]

    if len(batches) <= 1:
        for batch in batches:
            _translate_batch(client, batch, tr_map)
        log.info(f"Translated {len(raw_segments)}/{len(raw_segments)} segments")
    else:
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
        tr = tr_map.get(idx, {"en": "", "zh": ""})
        merged.append({**orig, "time": _fmt(orig["start"]), "en": tr["en"], "zh": tr["zh"]})

    return merged


def _extract_items(data):
    """Pull the list of translation dicts from whatever shape the model returns:
    a bare list, {"translations": [...]}, or {"<anykey>": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("translations")
        if isinstance(items, list):
            return items
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _translate_batch(client: OpenAI, batch: list[dict], tr_map: dict) -> None:
    """Translate one batch with exponential backoff; populate tr_map."""
    payload = [{"index": s["index"], "ja": s["ja"]} for s in batch]
    prompt = (
        f"Translate ALL {len(batch)} Japanese segments into English (en) and "
        f"Simplified Chinese (zh). Return exactly {len(batch)} items preserving the original index.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                max_tokens=8192,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            data = json.loads(response.choices[0].message.content)
            items = _extract_items(data)
            got = 0
            for item in items:
                if isinstance(item, dict) and "index" in item:
                    with _TR_LOCK:
                        tr_map[item["index"]] = {
                            "en": item.get("en", ""),
                            "zh": item.get("zh", ""),
                        }
                    got += 1
            if got == 0:
                raise ValueError("no translations parsed from model response")
            return

        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(f"DeepSeek batch attempt {attempt + 1} failed: {exc} — retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                log.error(f"DeepSeek batch attempt {attempt + 1} failed: {exc} — padding with blanks")
                for s in batch:
                    with _TR_LOCK:
                        tr_map.setdefault(s["index"], {"en": "", "zh": ""})


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
