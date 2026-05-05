"""Translate Japanese segments to EN + ZH via Claude tool use (Call 1)."""
import json
import logging
import os
from typing import Any

import anthropic

log = logging.getLogger(__name__)

_BATCH = 40  # segments per API call
_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

_TOOL: dict = {
    "name": "write_translations",
    "description": "Output EN and ZH translations for a list of Japanese audio segments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "Copy the index from the input segment exactly",
                        },
                        "en": {
                            "type": "string",
                            "description": "Natural English translation",
                        },
                        "zh": {
                            "type": "string",
                            "description": "Simplified Chinese translation (简体中文)",
                        },
                    },
                    "required": ["index", "en", "zh"],
                },
            }
        },
        "required": ["segments"],
    },
}

_SYSTEM = (
    "You are a professional Japanese-to-English and Japanese-to-Chinese translator. "
    "Translate each segment naturally, preserving nuance, tone, and register. "
    "For 'en': idiomatic English. For 'zh': simplified Mandarin Chinese (普通话/简体). "
    "Copy the 'time' and 'ja' fields exactly as given — do not modify them."
)


def translate_segments(raw_segments: list[dict]) -> list[dict]:
    """Add 'time', 'en', 'zh' keys to each segment. Returns merged list."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    merged: list[dict] = []

    for i in range(0, len(raw_segments), _BATCH):
        batch = raw_segments[i : i + _BATCH]
        translations = _translate_batch(client, batch)
        # Positional zip — translations[j] corresponds to batch[j] by index,
        # so a time-string mismatch can never silently drop a segment.
        for orig, tr in zip(batch, translations):
            merged.append({**orig, "time": _fmt(orig["start"]), **tr})
        log.info(
            f"Translated {min(i + _BATCH, len(raw_segments))}/{len(raw_segments)} segments"
        )

    return merged


def _safe_input(block_input: Any) -> dict:
    """Coerce block.input to dict — SDK can return a raw string on truncation."""
    if isinstance(block_input, dict):
        return block_input
    if isinstance(block_input, str):
        try:
            parsed = json.loads(block_input)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    log.warning(f"Unexpected block.input type {type(block_input)}")
    return {}


_BLANK = {"en": "", "zh": ""}


def _translate_batch(client: anthropic.Anthropic, batch: list[dict]) -> list[dict]:
    """Return a list of {en, zh} dicts, one per input segment, in the same order.
    Never shorter than batch — missing entries are padded with blanks."""
    n = len(batch)
    payload = [{"index": j, "time": _fmt(s["start"]), "ja": s["ja"]}
               for j, s in enumerate(batch)]

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=_MODEL,
                max_tokens=4096,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "write_translations"},
                messages=[{
                    "role": "user",
                    "content": (
                        f"Translate ALL {n} segments below. "
                        f"Return exactly {n} items in the same order.\n"
                        + json.dumps(payload, ensure_ascii=False, indent=2)
                    ),
                }],
            )
            if response.stop_reason == "max_tokens":
                log.warning("Translation hit max_tokens — partial batch, padding with blanks")

            for block in response.content:
                if block.type == "tool_use" and block.name == "write_translations":
                    raw = _safe_input(block.input).get("segments", [])
                    # Extract only en/zh from each returned item (positional)
                    out = []
                    for item in raw:
                        if isinstance(item, dict):
                            out.append({"en": item.get("en", ""), "zh": item.get("zh", "")})
                    # Pad to match batch length if Claude returned fewer
                    while len(out) < n:
                        out.append(_BLANK)
                    if len(out) > n:
                        log.warning(f"Claude returned {len(out)} segments for batch of {n} — truncating")
                        out = out[:n]
                    return out
        except Exception as exc:
            if attempt == 0:
                log.warning(f"Translation batch attempt 1 failed: {exc} — retrying")
            else:
                log.error(f"Translation batch attempt 2 failed: {exc} — padding with blanks")

    return [_BLANK] * n


def _fmt(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
