"""Translate Japanese segments to EN + ZH via Google Gemini Flash."""
import json
import logging
import os

from google import genai

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_BATCH   = 50   # segments per Gemini call (large context window — 50 is comfortable)

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

    for i in range(0, len(raw_segments), _BATCH):
        batch = raw_segments[i : i + _BATCH]
        _translate_batch(client, batch, i, tr_map)
        log.info(f"Translated {min(i + _BATCH, len(raw_segments))}/{len(raw_segments)} segments")

    merged = []
    for orig in raw_segments:
        idx = orig["index"]
        tr  = tr_map.get(idx, {"en": "", "zh": ""})
        merged.append({**orig, "time": _fmt(orig["start"]), "en": tr["en"], "zh": tr["zh"]})

    return merged


def _translate_batch(client: genai.Client, batch: list[dict], offset: int, tr_map: dict) -> None:
    """Translate one batch; populate tr_map with {index: {en, zh}} entries."""
    payload = [{"index": s["index"], "ja": s["ja"]} for s in batch]
    prompt  = (
        f"Translate ALL {len(batch)} Japanese segments below into English (en) "
        f"and Simplified Chinese (zh). Return exactly {len(batch)} items preserving the index.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    for attempt in range(2):
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
                    tr_map[item["index"]] = {"en": item.get("en", ""), "zh": item.get("zh", "")}
            return

        except Exception as exc:
            if attempt == 0:
                log.warning(f"Gemini translation batch attempt 1 failed: {exc} — retrying")
            else:
                log.error(f"Gemini translation batch attempt 2 failed: {exc} — padding with blanks")
                for s in batch:
                    tr_map.setdefault(s["index"], {"en": "", "zh": ""})


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
