"""Identify JLPT vocabulary and grammar patterns via OpenAI gpt-4o-mini function calling."""
import json
import logging
import os
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini")

# level key → (display label, ordered JLPT tiers to find)
LEVELS: dict[str, tuple[str, list[str]]] = {
    "beginner":              ("N5",    ["N5"]),
    "beginner-intermediate": ("N4–N3", ["N4", "N3"]),
    "intermediate":          ("N3",    ["N3"]),
    "intermediate-advanced": ("N2",    ["N2"]),
    "advanced":              ("N2–N1", ["N2", "N1"]),
}
DEFAULT_LEVEL = "advanced"

_SCHEMA: dict = {
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
                    "word":     {"type": "string", "description": "Exact surface form from the transcript"},
                    "reading":  {"type": "string", "description": "Hiragana reading"},
                    "en":       {"type": "string", "description": "English gloss (concise)"},
                    "zh":       {"type": "string", "description": "Chinese gloss (concise)"},
                    "type":     {"type": "string", "enum": ["vocab", "grammar"]},
                    "level":    {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5", "context-specific"]},
                    "register": {"type": "string", "description": "e.g. formal, casual, written, literary, spoken"},
                },
                "required": ["word", "reading", "en", "zh", "type", "level", "register"],
            },
        },
        "vocab": {
            "type": "array",
            "description": "Expanded flashcard entries for every vocab item in highlights.",
            "items": {
                "type": "object",
                "properties": {
                    "word":     {"type": "string"},
                    "reading":  {"type": "string"},
                    "en":       {"type": "string"},
                    "zh":       {"type": "string"},
                    "level":    {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5", "context-specific"]},
                    "example":  {"type": "string", "description": "Example sentence from the transcript"},
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
                    "pattern":      {"type": "string", "description": "Pattern notation, e.g. 〜にもかかわらず"},
                    "reading":      {"type": "string"},
                    "meaning_en":   {"type": "string"},
                    "meaning_zh":   {"type": "string"},
                    "level":        {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5", "context-specific"]},
                    "construction": {"type": "string", "description": "How to form this pattern"},
                    "example":      {"type": "string", "description": "Example from transcript"},
                },
                "required": ["pattern", "reading", "meaning_en", "meaning_zh", "level", "construction", "example"],
            },
        },
        "expressions": {
            "type": "array",
            "description": "Set phrases, idioms, and notable collocations",
            "items": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "reading":    {"type": "string"},
                    "en":         {"type": "string"},
                    "zh":         {"type": "string"},
                    "context":    {"type": "string", "description": "When/how this expression is typically used"},
                },
                "required": ["expression", "reading", "en", "zh", "context"],
            },
        },
    },
    "required": ["highlights", "vocab", "grammar", "expressions"],
}

_EMPTY = {"highlights": [], "vocab": [], "grammar": [], "expressions": []}
_CHUNK_CHARS = 1500   # target Japanese chars per analysis chunk


def _build_system(jlpt_tiers: list[str]) -> str:
    tiers_str = " and ".join(jlpt_tiers)
    n1_guidance = (
        "\n\nN1 vs N2 classification guidance: "
        "Label an item N1 if it is low-frequency in everyday spoken Japanese — "
        "formal/written vocabulary, literary or classical expressions, abstract or academic nouns, "
        "compound verbs or auxiliary forms rare in conversation, and grammar patterns found only in "
        "formal writing or sophisticated prose. "
        "Label it N2 only if it genuinely appears often in news, business conversation, or daily life. "
        "When in doubt between N1 and N2, prefer N1."
    ) if "N1" in jlpt_tiers else ""
    return (
        f"You are a Japanese language expert specialising in JLPT {tiers_str} preparation. "
        f"Analyse the transcript below and identify all {tiers_str} vocabulary items, grammar patterns, "
        "set phrases, and idioms. Be comprehensive — learners rely on you to catch everything at "
        "these levels. For 'highlights', use the exact surface form from the text so the UI can "
        f"underline it inline. Include only {tiers_str} items; skip items outside this range."
        f"{n1_guidance}\n\n"
        "Additionally, use level 'context-specific' for words and expressions that are beyond N1 "
        "but are important for understanding this specific content — for example: domain-specific "
        "terminology (political, legal, medical, technical), advanced literary or formal expressions, "
        "topical jargon, or culturally significant terms a learner at this level should know to "
        "follow the topic. These appear separately from JLPT levels in the UI."
    )


def _coerce_list_of_dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _chunk_segments(segments: list[dict]) -> list[list[dict]]:
    """Split segments into groups where each group's Japanese text stays under _CHUNK_CHARS."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for seg in segments:
        seg_chars = len(seg["ja"])
        if current and current_chars + seg_chars > _CHUNK_CHARS:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(seg)
        current_chars += seg_chars
    if current:
        chunks.append(current)
    return chunks


def _merge_analyses(results: list[dict]) -> dict:
    """Merge chunk results, deduplicating by surface form / pattern."""
    seen: dict[str, set] = {
        "highlights": set(), "vocab": set(), "grammar": set(), "expressions": set()
    }
    merged: dict[str, list] = {k: [] for k in seen}
    keys = {"highlights": "word", "vocab": "word", "grammar": "pattern", "expressions": "expression"}

    for result in results:
        for section, key in keys.items():
            for item in result.get(section, []):
                val = item.get(key, "")
                if val and val not in seen[section]:
                    seen[section].add(val)
                    merged[section].append(item)
    return merged


def _analyze_chunk(
    client: OpenAI, system: str, jlpt_tiers: list[str],
    transcript: str, chunk_idx: int, total: int,
) -> dict:
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                max_tokens=8192,
                tools=[{
                    "type": "function",
                    "function": {
                        "name":        "write_analysis",
                        "description": (
                            "Output JLPT vocabulary and grammar analysis for a Japanese transcript, "
                            "including highlight markers, flashcards, and expressions."
                        ),
                        "parameters":  _SCHEMA,
                    },
                }],
                tool_choice={"type": "function", "function": {"name": "write_analysis"}},
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": (
                            f"Analyse this Japanese transcript for {' and '.join(jlpt_tiers)} content "
                            f"(chunk {chunk_idx + 1}/{total}):\n\n" + transcript
                        ),
                    },
                ],
            )

            choice = response.choices[0]
            if choice.finish_reason == "length":
                log.warning(f"Chunk {chunk_idx + 1}/{total} hit max_tokens — output truncated")

            for tc in (choice.message.tool_calls or []):
                if tc.function.name == "write_analysis":
                    try:
                        raw = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as exc:
                        log.warning(f"Chunk {chunk_idx + 1} JSON decode error: {exc} — skipping chunk")
                        return _EMPTY
                    return {
                        "highlights":  _coerce_list_of_dicts(raw.get("highlights", [])),
                        "vocab":       _coerce_list_of_dicts(raw.get("vocab", [])),
                        "grammar":     _coerce_list_of_dicts(raw.get("grammar", [])),
                        "expressions": _coerce_list_of_dicts(raw.get("expressions", [])),
                    }

        except Exception as exc:
            if attempt == 0:
                log.warning(f"Chunk {chunk_idx + 1} attempt 1 failed: {exc} — retrying")
            else:
                log.error(f"Chunk {chunk_idx + 1} attempt 2 failed: {exc} — skipping chunk")

    return _EMPTY


def analyze_transcript(segments: list[dict], level: str = DEFAULT_LEVEL) -> dict:
    """Return analysis dict: highlights, vocab, grammar, expressions."""
    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    system = _build_system(jlpt_tiers)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    chunks = _chunk_segments(segments)
    log.info(f"Analyzing {len(segments)} segments in {len(chunks)} chunk(s) via {_MODEL}")

    results = []
    for i, chunk in enumerate(chunks):
        transcript = "\n".join(f"[{s['time']}] {s['ja']}" for s in chunk)
        result = _analyze_chunk(client, system, jlpt_tiers, transcript, i, len(chunks))
        results.append(result)

    merged = _merge_analyses(results)
    log.info(
        f"Analysis ({' / '.join(jlpt_tiers)}): "
        f"{len(merged['highlights'])} highlights, "
        f"{len(merged['vocab'])} vocab, "
        f"{len(merged['grammar'])} grammar, "
        f"{len(merged['expressions'])} expressions"
    )
    return merged
