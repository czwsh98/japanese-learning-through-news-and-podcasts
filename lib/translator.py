"""Translate Japanese segments to EN + ZH via Google Cloud Translation API."""
import html
import logging
import os

import requests

log = logging.getLogger(__name__)

_API_KEY  = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
_BATCH    = 100   # Google's max per request


def translate_segments(raw_segments: list[dict]) -> list[dict]:
    """Add 'time', 'en', 'zh' keys to each segment. Returns merged list."""
    texts   = [s["ja"] for s in raw_segments]
    en_list = _translate(texts, "en")
    zh_list = _translate(texts, "zh-CN")

    merged = []
    for orig, en, zh in zip(raw_segments, en_list, zh_list):
        merged.append({**orig, "time": _fmt(orig["start"]), "en": en, "zh": zh})

    log.info(f"Translated {len(merged)} segments (EN + ZH)")
    return merged


def _translate(texts: list[str], target: str) -> list[str]:
    """Batch-translate *texts* to *target* language code. Returns same-length list."""
    if not _API_KEY:
        log.error("GOOGLE_TRANSLATE_API_KEY is not set — returning empty translations")
        return [""] * len(texts)

    results: list[str] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i : i + _BATCH]
        try:
            resp = requests.post(
                _ENDPOINT,
                params={"key": _API_KEY},
                json={"q": batch, "source": "ja", "target": target, "format": "text"},
                timeout=30,
            )
            resp.raise_for_status()
            translations = resp.json()["data"]["translations"]
            # Google returns HTML entities even in text mode — unescape them
            results.extend(html.unescape(t["translatedText"]) for t in translations)
        except Exception as exc:
            log.error(f"Google Translate failed [{i}:{i+len(batch)}] → {target}: {exc}")
            results.extend("" for _ in batch)
    return results


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
