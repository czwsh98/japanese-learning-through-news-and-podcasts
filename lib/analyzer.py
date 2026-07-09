"""Identify JLPT vocabulary and grammar patterns via OpenAI gpt-4o-mini function calling."""
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini")
_MAX_WORKERS = 4  # concurrent OpenAI requests
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds

# level key → (display label, ordered JLPT tiers to find)
# NOTE: the `tiers` here MUST match LEVEL_TIERS in web/static/js/player.js — the UI
# filters vocab/grammar to those tiers, so any tier the analyzer omits is invisible,
# and any word graded outside the band is hidden. Keep the two in sync.
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

def _seg_time(seg: dict) -> str:
    if "time" in seg:
        return seg["time"]
    t = int(seg["start"])
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"

_CHUNK_CHARS  = 1500   # target Japanese chars per analysis chunk
# Hard ceiling on analysis API calls per job.  A 60-min transcript at typical
# speaking pace (~300 chars/min) ≈ 12 chunks; 40 gives generous headroom while
# bounding worst-case spend to ~40 × $0.003 ≈ $0.12 of gpt-4o-mini per job.
_MAX_CHUNKS   = 40


def _build_system(jlpt_tiers: list[str]) -> str:
    tiers_str = " and ".join(jlpt_tiers)
    return (
        f"You are a Japanese language expert and JLPT examiner. Analyse the transcript below and "
        f"extract noteworthy vocabulary, grammar patterns, set phrases, and idioms in the {tiers_str} "
        "difficulty band.\n\n"

        "MOST IMPORTANT — grade each item's TRUE JLPT level:\n"
        "Assign every item the JLPT level at which it is actually taught, following the standard "
        "JLPT vocabulary and grammar lists. The scale runs N5 (easiest, most common) → N1 (hardest, "
        "rarest). Grade each word on its own merit — do NOT stamp everything with the target level. "
        "Most words in ordinary conversation are N5–N3; only genuinely difficult, formal, literary, "
        "abstract, or low-frequency items reach N2 or N1. When unsure between two adjacent levels, "
        "choose the LOWER (more common) one.\n\n"

        "Calibration — these common words have been seen mislabelled too high; they are all N3 or "
        "easier and must never be labelled N1 or N2: 強い, 国, 問題, 正しい, 高い, 使う, 気をつけて, "
        "意味, 驚く, 得意, 経験, 成功, 期待, 冗談, 結局, 能力, 尊敬, 不思議, 相手, 注目, 選択肢. "
        "Common English-derived katakana loanwords (メッセージ, レベル, メディア, リスペクト, コンディション, "
        "エビデンス) are not JLPT target vocabulary — treat them as 'context-specific' or skip them.\n\n"

        f"What to extract: items whose true level falls in the {tiers_str} band. Skip words that are "
        "clearly below the band (elementary everyday vocabulary) — it is better to return fewer, "
        "correctly-graded items than to pad the list. If you do include a borderline item, label it "
        "with its TRUE level, never the target level: the app filters cards by level, so an accurate "
        "label matters more than inclusion. For 'highlights', copy the exact surface form from the "
        "text so the UI can underline it inline.\n\n"

        "Additionally, use level 'context-specific' for words and expressions beyond N1 that are "
        "important for understanding this specific content — for example: domain-specific "
        "terminology (political, legal, medical, technical), advanced literary or formal expressions, "
        "topical jargon, or culturally significant terms a learner should know to follow the topic. "
        "These appear separately from JLPT levels in the UI."
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
    for attempt in range(_MAX_RETRIES):
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
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(f"Chunk {chunk_idx + 1} attempt {attempt + 1} failed: {exc} — retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                log.error(f"Chunk {chunk_idx + 1} attempt {attempt + 1} failed: {exc} — skipping chunk")

    return _EMPTY


def analyze_transcript(segments: list[dict], level: str = DEFAULT_LEVEL) -> dict:
    """Return analysis dict: highlights, vocab, grammar, expressions."""
    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    system = _build_system(jlpt_tiers)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    chunks = _chunk_segments(segments)
    if len(chunks) > _MAX_CHUNKS:
        log.warning(
            "Transcript produced %d chunks (limit %d) — truncating to cap API spend.",
            len(chunks), _MAX_CHUNKS,
        )
        chunks = chunks[:_MAX_CHUNKS]
    log.info(f"Analyzing {len(segments)} segments in {len(chunks)} chunk(s) via {_MODEL}")

    if len(chunks) <= 1:
        # Single chunk — no threading overhead
        results = []
        for i, chunk in enumerate(chunks):
            transcript = "\n".join(f"[{_seg_time(s)}] {s['ja']}" for s in chunk)
            results.append(_analyze_chunk(client, system, jlpt_tiers, transcript, i, len(chunks)))
    else:
        # Multiple chunks — run concurrently
        log.info(f"Running {len(chunks)} analysis chunks concurrently ({_MAX_WORKERS} workers)")
        results = [_EMPTY] * len(chunks)
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {}
            for i, chunk in enumerate(chunks):
                transcript = "\n".join(f"[{_seg_time(s)}] {s['ja']}" for s in chunk)
                futures[pool.submit(_analyze_chunk, client, system, jlpt_tiers, transcript, i, len(chunks))] = i
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    log.error(f"Analysis chunk {idx + 1} failed: {exc}")

    merged = _merge_analyses(results)
    log.info(
        f"Analysis ({' / '.join(jlpt_tiers)}): "
        f"{len(merged['highlights'])} highlights, "
        f"{len(merged['vocab'])} vocab, "
        f"{len(merged['grammar'])} grammar, "
        f"{len(merged['expressions'])} expressions"
    )
    return merged


# Hard caps for explain_sentence to prevent runaway token usage.
_EXPLAIN_MAX_INPUT_CHARS = 500   # ~1-3 Japanese sentences; reject anything longer
_EXPLAIN_MAX_TOKENS      = 600   # ~400 words — enough for a thorough breakdown


def explain_sentence(text: str) -> str:
    """Return a detailed grammatical breakdown of a Japanese sentence via LLM.

    Input is capped at _EXPLAIN_MAX_INPUT_CHARS before the API call to prevent
    token-burn via oversized requests.  Output is capped via max_tokens.
    """
    # Truncate at the library level as a backstop (the API endpoint also
    # validates, but defense in depth means we never pass huge text to the LLM).
    if len(text) > _EXPLAIN_MAX_INPUT_CHARS:
        text = text[:_EXPLAIN_MAX_INPUT_CHARS]
        log.warning("explain_sentence: input truncated to %d chars", _EXPLAIN_MAX_INPUT_CHARS)

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    system = (
        "You are a Japanese language expert. Provide a concise but thorough grammatical breakdown "
        "of the Japanese sentence provided by the user. Explain particles, conjugations, and "
        "any difficult vocabulary or idioms. Use Markdown for formatting. "
        "The tone should be helpful and educational. Keep it under 200 words."
    )
    try:
        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=_EXPLAIN_MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Please explain this sentence: {text}"},
            ],
        )
        return response.choices[0].message.content or "Could not generate explanation."
    except Exception as exc:
        log.error(f"Explain sentence failed: {exc}")
        return f"Error: {str(exc)}"
