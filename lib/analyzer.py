"""Identify JLPT vocabulary and grammar patterns via Claude tool use (Call 2)."""
import json
import logging
import os
from typing import Any

import anthropic

log = logging.getLogger(__name__)

_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# level key → (display label, ordered JLPT tiers to find)
LEVELS: dict[str, tuple[str, list[str]]] = {
    "beginner":             ("N5",    ["N5"]),
    "beginner-intermediate":("N4–N3", ["N4", "N3"]),
    "intermediate":         ("N3",    ["N3"]),
    "intermediate-advanced":("N2",    ["N2"]),
    "advanced":             ("N2–N1", ["N2", "N1"]),
}
DEFAULT_LEVEL = "advanced"

_TOOL: dict = {
    "name": "write_analysis",
    "description": (
        "Output JLPT vocabulary and grammar analysis for a Japanese transcript, "
        "including highlight markers, flashcards, and expressions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "highlights": {
                "type": "array",
                "description": (
                    "Every target-level vocabulary item and grammar pattern found in the transcript. "
                    "'word' must be the exact surface form as it appears in the text."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {
                            "type": "string",
                            "description": "Exact surface form from the transcript",
                        },
                        "reading": {"type": "string", "description": "Hiragana reading"},
                        "en": {"type": "string", "description": "English gloss (concise)"},
                        "zh": {"type": "string", "description": "Chinese gloss (concise)"},
                        "type": {
                            "type": "string",
                            "enum": ["vocab", "grammar"],
                        },
                        "level": {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5"]},
                        "register": {
                            "type": "string",
                            "description": "e.g. formal, casual, written, literary, spoken",
                        },
                    },
                    "required": ["word", "reading", "en", "zh", "type", "level", "register"],
                },
            },
            "vocab": {
                "type": "array",
                "description": "Expanded flashcard entries for every vocab item in highlights. Must include all vocab-type highlights — do not skip any.",
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "reading": {"type": "string"},
                        "en": {"type": "string"},
                        "zh": {"type": "string"},
                        "level": {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5"]},
                        "example": {
                            "type": "string",
                            "description": "Example sentence from the transcript",
                        },
                        "register": {"type": "string"},
                    },
                    "required": ["word", "reading", "en", "zh", "level", "example", "register"],
                },
            },
            "grammar": {
                "type": "array",
                "description": "Grammar pattern flashcard entries",
                "items": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Pattern notation, e.g. 〜にもかかわらず",
                        },
                        "reading": {"type": "string"},
                        "meaning_en": {"type": "string"},
                        "meaning_zh": {"type": "string"},
                        "level": {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5"]},
                        "construction": {
                            "type": "string",
                            "description": "How to form this pattern, e.g. V辞書形 + にもかかわらず",
                        },
                        "example": {"type": "string", "description": "Example from transcript"},
                    },
                    "required": [
                        "pattern", "reading", "meaning_en", "meaning_zh",
                        "level", "construction", "example",
                    ],
                },
            },
            "expressions": {
                "type": "array",
                "description": "Set phrases, idioms, and notable collocations",
                "items": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string"},
                        "reading": {"type": "string"},
                        "en": {"type": "string"},
                        "zh": {"type": "string"},
                        "context": {
                            "type": "string",
                            "description": "When/how this expression is typically used",
                        },
                    },
                    "required": ["expression", "reading", "en", "zh", "context"],
                },
            },
        },
        "required": ["highlights", "vocab", "grammar", "expressions"],
    },
}

_EMPTY = {"highlights": [], "vocab": [], "grammar": [], "expressions": []}


def _build_system(jlpt_tiers: list[str]) -> str:
    tiers_str = " and ".join(jlpt_tiers)
    return (
        f"You are a Japanese language expert specialising in JLPT {tiers_str} preparation. "
        f"Analyse the transcript below and identify all {tiers_str} vocabulary items, grammar patterns, "
        "set phrases, and idioms. Be comprehensive — learners rely on you to catch everything at "
        "this level. For 'highlights', use the exact surface form from the text so the UI can "
        f"underline it inline. Include only {tiers_str} items; skip items outside this range."
    )


def _extract_input(block_input: Any) -> dict:
    if isinstance(block_input, dict):
        return block_input
    if isinstance(block_input, str):
        try:
            parsed = json.loads(block_input)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    log.warning(f"Unexpected block.input type {type(block_input)} — treating as empty")
    return {}


def _coerce_list_of_dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def analyze_transcript(segments: list[dict], level: str = DEFAULT_LEVEL) -> dict:
    """Return analysis dict: highlights, vocab, grammar, expressions."""
    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    system = _build_system(jlpt_tiers)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    transcript = "\n".join(f"[{s['time']}] {s['ja']}" for s in segments)

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=_MODEL,
                max_tokens=8192,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "write_analysis"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Analyse this Japanese transcript for {' and '.join(jlpt_tiers)} content:\n\n"
                                    + transcript
                                ),
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            )
            if response.stop_reason == "max_tokens":
                log.warning(
                    "Analysis hit max_tokens — output was truncated. "
                    "The transcript may be too long; consider shorter audio clips."
                )
            for block in response.content:
                if block.type == "tool_use" and block.name == "write_analysis":
                    raw = _extract_input(block.input)
                    data = {
                        "highlights":  _coerce_list_of_dicts(raw.get("highlights", [])),
                        "vocab":       _coerce_list_of_dicts(raw.get("vocab", [])),
                        "grammar":     _coerce_list_of_dicts(raw.get("grammar", [])),
                        "expressions": _coerce_list_of_dicts(raw.get("expressions", [])),
                    }
                    log.info(
                        f"Analysis ({' / '.join(jlpt_tiers)}): {len(data['highlights'])} highlights, "
                        f"{len(data['vocab'])} vocab, {len(data['grammar'])} grammar, "
                        f"{len(data['expressions'])} expressions"
                    )
                    return data
        except Exception as exc:
            if attempt == 0:
                log.warning(f"Analysis attempt 1 failed: {exc} — retrying")
            else:
                log.error(f"Analysis attempt 2 failed: {exc} — returning empty analysis")

    return _EMPTY
