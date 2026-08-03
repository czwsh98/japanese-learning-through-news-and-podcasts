"""
Extract JLPT vocabulary, grammar, and expressions from a Japanese transcript.

Vocabulary and its JLPT level come from lib/jlpt_bank.py — a real JLPT word
list — not from LLM grading. Asking the LLM to grade vocabulary was tried
twice (see git history) and never worked: it invented non-words, stamped
words with whatever level was asked for regardless of their true difficulty,
and mislabelled easy words as advanced. The bank only answers "is this word
JLPT-graded, and at what level" — a lookup, not a guess.

The LLM keeps two jobs it's actually suited for:
  - curating which bank-graded candidates are worth a flashcard, and writing
    the Chinese gloss + register for them (Pass B)
  - extracting grammar patterns and expressions, which have no equivalent
    open dataset here (Pass C, same spirit as before)
"""
import json
import hashlib
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from openai import OpenAI

from lib import jlpt_bank
from lib.api_usage import record_chat_failure, record_chat_usage

log = logging.getLogger(__name__)

_PROVIDER = os.environ.get("ANALYSIS_PROVIDER", "deepseek")
_MODEL = (
    os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash") if _PROVIDER == "deepseek"
    else os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini")
)
_MAX_WORKERS = 4  # concurrent LLM requests
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds


def _get_client() -> OpenAI:
    if _PROVIDER == "deepseek":
        return OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


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

# ── Pass B: LLM curates bank-graded candidates (cannot invent words/levels) ──

_CURATE_MAX = 80  # cap curated JLPT-tier vocab per episode
_CTX_MAX    = 30  # cap curated context-specific vocab per episode

def _curate_schema(is_context: bool) -> dict:
    # Two distinct schemas rather than one schema with a conditional field:
    # tested with a real 40-candidate context-specific batch, a single schema
    # phrased as "fill 'en' only if it's empty" made the model pattern-match
    # to the majority case in the batch (every context-specific candidate's
    # bank 'en' IS empty) and leave its own 'en' blank too, even though it
    # filled it correctly on a small hand-picked example. Splitting into two
    # unconditional schemas — "always write en" for context-specific batches,
    # "never touch en, it's already correct" for JLPT-tier batches — removes
    # the ambiguity instead of fighting it with more prompt engineering.
    props = {
        "word":     {"type": "string", "description": "Copied EXACTLY from the candidate list — do not alter or re-conjugate it"},
        "zh":       {"type": "string", "description": "Concise Chinese gloss (简体)"},
        "register": {"type": "string", "description": "e.g. formal, casual, written, literary, spoken"},
    }
    required = ["word", "zh", "register"]
    if is_context:
        props["en"] = {"type": "string", "description": "Concise English gloss"}
        required.append("en")
    return {
        "type": "object",
        "properties": {
            "picked": {
                "type": "array",
                "description": "The candidates most useful for a learner to study.",
                "items": {"type": "object", "properties": props, "required": required},
            },
        },
        "required": ["picked"],
    }


def _build_curate_system(jlpt_tiers: list[str], is_context: bool) -> str:
    band = "domain-specific, technical, or literary" if is_context else " and ".join(jlpt_tiers)
    gloss_instructions = (
        "write a concise English gloss, a concise Chinese gloss, and its register "
        "(formal/casual/written/literary/spoken)."
        if is_context else
        "write a concise, natural Chinese gloss plus its register "
        "(formal/casual/written/literary/spoken). Do not write an English gloss — "
        "these candidates already have a correct one."
    )
    return (
        f"You are curating a Japanese study deck. Below is a JSON list of {band} vocabulary "
        "candidates already found and graded from a real transcript — 'count' is how many times "
        "each appeared. Select the ones most useful and interesting for a learner to study: favor "
        "words that recur, that carry real meaning in context, and that the learner is likely to "
        "encounter again. Skip candidates that are redundant with each other or too obscure to be "
        "worth a flashcard.\n\n"
        f"For each word you select, copy its 'word' field EXACTLY as given — do not alter, "
        f"translate, re-conjugate, or invent a variant of it — and {gloss_instructions}"
    )


def _curate_vocab(
    client: OpenAI, candidates: list[dict], jlpt_tiers: list[str],
    is_context: bool = False, cap: int = _CURATE_MAX,
) -> list[dict]:
    """Ask the LLM to pick the most useful candidates and gloss them.

    The LLM can only select from `candidates` — any returned 'word' not
    found there (a hallucination) is dropped, never trusted. Level, English
    gloss, reading, example, and surface forms all come from the bank scan,
    never from the model.
    """
    if not candidates:
        return []

    system = _build_curate_system(jlpt_tiers, is_context)
    schema = _curate_schema(is_context)
    # JLPT-tier candidates always have a bank 'en' already — don't even show
    # the model an 'en' field there, so there's nothing to (mis)copy or omit.
    payload = [
        {"word": c["word"], "reading": c["reading"], "level": c["level"], "count": c["count"],
         **({} if is_context else {"en": c["en"]})}
        for c in candidates
    ]

    picked: list[dict] = []
    for attempt in range(_MAX_RETRIES):
        response = None
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                max_tokens=4096,
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "pick_vocab",
                        "description": "Select the most useful vocabulary candidates to study.",
                        "parameters": schema,
                    },
                }],
                tool_choice={"type": "function", "function": {"name": "pick_vocab"}},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            record_chat_usage(
                response, provider=_PROVIDER, model=_MODEL,
                stage="vocab_context_curation" if is_context else "vocab_curation",
                attempt=attempt + 1,
            )
            for tc in (response.choices[0].message.tool_calls or []):
                if tc.function.name == "pick_vocab":
                    picked = _coerce_list_of_dicts(json.loads(tc.function.arguments).get("picked", []))
                    break
            break
        except Exception as exc:
            if response is None:
                record_chat_failure(
                    provider=_PROVIDER, model=_MODEL,
                    stage="vocab_context_curation" if is_context else "vocab_curation",
                    attempt=attempt + 1, exc=exc,
                )
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(f"Vocab curation attempt {attempt + 1} failed: {exc} — retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                log.error(f"Vocab curation failed after {_MAX_RETRIES} attempts: {exc}")

    by_word = {c["word"]: c for c in candidates}
    out, seen = [], set()
    for item in picked:
        word = item.get("word", "")
        cand = by_word.get(word)
        if cand is None or word in seen:
            continue  # not in the candidate list — a hallucination, drop it
        seen.add(word)
        # Bank's own gloss (JLPT-tier words) wins when present; context-specific
        # words aren't in the bank (empty "en"), so fall back to the LLM's gloss.
        out.append({
            "word":     cand["word"],
            "reading":  cand["reading"],
            "en":       cand["en"] or item.get("en", ""),
            "zh":       item.get("zh", ""),
            "level":    cand["level"],
            "example":  cand["example"],
            "register": item.get("register", ""),
            "surfaces": cand["surfaces"],
        })
    return out[:cap]


# ── Pass C: chunked LLM call for grammar patterns + expressions only ────────
# Vocabulary highlights are no longer requested here — they come from the
# bank-graded Pass A/B above. This schema only covers what has no equivalent
# open dataset: grammar patterns (still LLM-graded, unchanged) and set
# expressions/idioms.

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "description": (
                "Every occurrence of a GRAMMAR PATTERN found in the transcript, so the UI can "
                "underline it inline. Do not include vocabulary words here — those are handled "
                "separately. 'word' must be the exact surface form as it appears in the text."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "word":     {"type": "string", "description": "Exact surface form from the transcript"},
                    "reading":  {"type": "string", "description": "Hiragana reading"},
                    "en":       {"type": "string", "description": "English gloss (concise)"},
                    "zh":       {"type": "string", "description": "Chinese gloss (concise)"},
                    "type":     {"type": "string", "enum": ["grammar"]},
                    "level":    {"type": "string", "enum": ["N1", "N2", "N3", "N4", "N5", "context-specific"]},
                    "register": {"type": "string", "description": "e.g. formal, casual, written, literary, spoken"},
                },
                "required": ["word", "reading", "en", "zh", "type", "level", "register"],
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
    "required": ["highlights", "grammar", "expressions"],
}

_EMPTY = {"highlights": [], "grammar": [], "expressions": []}

def _seg_time(seg: dict) -> str:
    if "time" in seg:
        return seg["time"]
    t = int(seg["start"])
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"

_CHUNK_CHARS  = 1500   # target Japanese chars per analysis chunk
# Hard ceiling on analysis API calls per job.  A 60-min transcript at typical
# speaking pace (~300 chars/min) ≈ 12 chunks; 40 gives generous headroom while
# bounding worst-case spend per job.
_MAX_CHUNKS   = 40


def _build_system(jlpt_tiers: list[str]) -> str:
    tiers_str = " and ".join(jlpt_tiers)
    return (
        f"You are a Japanese language expert and JLPT examiner. Analyse the transcript below and "
        f"extract grammar patterns, set phrases, and idioms in the {tiers_str} difficulty band. "
        "Vocabulary words are handled separately — do not extract individual vocabulary here.\n\n"

        "For 'highlights': mark every occurrence of a GRAMMAR PATTERN in the text (never a plain "
        "vocabulary word). Copy the exact surface form from the text so the UI can underline it "
        "inline.\n\n"

        "For 'grammar': one flashcard entry per distinct pattern found, with construction notes and "
        "an example from the transcript. Grade each pattern's TRUE JLPT level following the standard "
        "JLPT grammar lists — do not stamp every pattern with the target level; when unsure between "
        "two adjacent levels, choose the LOWER (more common) one. Only include patterns whose true "
        f"level falls in the {tiers_str} band; skip patterns clearly below it.\n\n"

        "For 'expressions': set phrases, idioms, and notable collocations worth studying, regardless "
        "of level.\n\n"

        "Additionally, use level 'context-specific' for grammar patterns beyond N1 that are "
        "important for understanding this specific content — domain-specific constructions, "
        "advanced literary or formal patterns, or topical jargon. These appear separately from "
        "JLPT levels in the UI."
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
        "highlights": set(), "grammar": set(), "expressions": set()
    }
    merged: dict[str, list] = {k: [] for k in seen}
    keys = {"highlights": "word", "grammar": "pattern", "expressions": "expression"}

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
        response = None
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                max_tokens=8192,
                tools=[{
                    "type": "function",
                    "function": {
                        "name":        "write_analysis",
                        "description": (
                            "Output grammar pattern analysis for a Japanese transcript, including "
                            "highlight markers, flashcards, and expressions."
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
                            f"Analyse this Japanese transcript for {' and '.join(jlpt_tiers)} grammar "
                            f"(chunk {chunk_idx + 1}/{total}):\n\n" + transcript
                        ),
                    },
                ],
            )
            record_chat_usage(
                response, provider=_PROVIDER, model=_MODEL,
                stage="grammar_analysis", attempt=attempt + 1,
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
                        "grammar":     _coerce_list_of_dicts(raw.get("grammar", [])),
                        "expressions": _coerce_list_of_dicts(raw.get("expressions", [])),
                    }

        except Exception as exc:
            if response is None:
                record_chat_failure(
                    provider=_PROVIDER, model=_MODEL, stage="grammar_analysis",
                    attempt=attempt + 1, exc=exc,
                )
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(f"Chunk {chunk_idx + 1} attempt {attempt + 1} failed: {exc} — retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                log.error(f"Chunk {chunk_idx + 1} attempt {attempt + 1} failed: {exc} — skipping chunk")

    return _EMPTY


def analyze_transcript(segments: list[dict], level: str = DEFAULT_LEVEL) -> dict:
    """Return analysis dict: highlights, vocab, grammar, expressions.

    Pass A (deterministic): grade every content word against the JLPT bank.
    Pass B (1 LLM call):    curate the bank-graded candidates down to the
                            most useful ones; the model can't invent a word
                            or a level, only pick from what the bank found
                            and write glosses.
    Pass C (chunked LLM):   extract grammar patterns + expressions, same
                            spirit as before — no open grammar-level dataset
                            exists yet, so grammar levels stay LLM-graded.
    """
    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    client = _get_client()

    # ── Pass A ───────────────────────────────────────────────────────────
    candidates, ctx_candidates = jlpt_bank.scan(segments, jlpt_tiers)
    log.info(
        f"Bank scan ({' / '.join(jlpt_tiers)}): {len(candidates)} candidates, "
        f"{len(ctx_candidates)} context-specific candidates"
    )

    # ── Pass B ───────────────────────────────────────────────────────────
    vocab = _curate_vocab(client, candidates, jlpt_tiers, is_context=False, cap=_CURATE_MAX)
    vocab += _curate_vocab(client, ctx_candidates, jlpt_tiers, is_context=True, cap=_CTX_MAX)

    highlights = [
        {
            "word": surface, "reading": v["reading"], "en": v["en"], "zh": v["zh"],
            "type": "vocab", "level": v["level"], "register": v.get("register", ""),
        }
        for v in vocab for surface in v["surfaces"]
    ]

    # ── Pass C ───────────────────────────────────────────────────────────
    system = _build_system(jlpt_tiers)
    chunks = _chunk_segments(segments)
    if len(chunks) > _MAX_CHUNKS:
        log.warning(
            "Transcript produced %d chunks (limit %d) — truncating to cap API spend.",
            len(chunks), _MAX_CHUNKS,
        )
        chunks = chunks[:_MAX_CHUNKS]
    log.info(f"Extracting grammar/expressions from {len(segments)} segments in {len(chunks)} chunk(s) via {_MODEL}")

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
    highlights += merged["highlights"]

    log.info(
        f"Analysis ({' / '.join(jlpt_tiers)}): "
        f"{len(highlights)} highlights, "
        f"{len(vocab)} vocab, "
        f"{len(merged['grammar'])} grammar, "
        f"{len(merged['expressions'])} expressions"
    )
    return {
        "highlights": highlights,
        "vocab": vocab,
        "grammar": merged["grammar"],
        "expressions": merged["expressions"],
    }


# Hard caps for explain_sentence to prevent runaway token usage.
_EXPLAIN_MAX_INPUT_CHARS = 500   # ~1-3 Japanese sentences; reject anything longer
_EXPLAIN_MAX_TOKENS      = 600   # ~400 words — enough for a thorough breakdown
_EXPLAIN_PROVIDER = os.environ.get("EXPLAIN_PROVIDER", "deepseek").strip().lower()
_EXPLAIN_MODEL = os.environ.get(
    "EXPLAIN_MODEL",
    os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if _EXPLAIN_PROVIDER == "deepseek"
    else os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o-mini"),
)
_EXPLAIN_PROMPT_VERSION = "sentence-explain-v2"
_EXPLAIN_SYSTEM = (
    "You are a Japanese language expert. Provide a concise but thorough grammatical breakdown "
    "of the Japanese sentence provided by the user. Explain particles, conjugations, and "
    "any difficult vocabulary or idioms. Use Markdown for formatting. "
    "The tone should be helpful and educational. Keep it under 200 words."
)


def explanation_cache_key(text: str) -> str:
    """Stable, privacy-safe key for a sentence explanation result."""
    identity = "\0".join((
        _EXPLAIN_PROMPT_VERSION,
        _EXPLAIN_PROVIDER,
        _EXPLAIN_MODEL,
        text.strip(),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def explanation_cache_metadata() -> tuple[str, str]:
    """Provider/model identity stored alongside cached explanations."""
    return _EXPLAIN_PROVIDER, _EXPLAIN_MODEL


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

    if _EXPLAIN_PROVIDER == "deepseek":
        client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    elif _EXPLAIN_PROVIDER == "openai":
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    else:
        raise ValueError(f"Unsupported EXPLAIN_PROVIDER: {_EXPLAIN_PROVIDER}")
    response = None
    try:
        response = client.chat.completions.create(
            model=_EXPLAIN_MODEL,
            max_tokens=_EXPLAIN_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _EXPLAIN_SYSTEM},
                {"role": "user", "content": f"Please explain this sentence: {text}"},
            ],
        )
        record_chat_usage(
            response, provider=_EXPLAIN_PROVIDER, model=_EXPLAIN_MODEL,
            stage="sentence_explanation",
        )
        return response.choices[0].message.content or "Could not generate explanation."
    except Exception as exc:
        if response is None:
            record_chat_failure(
                provider=_EXPLAIN_PROVIDER, model=_EXPLAIN_MODEL,
                stage="sentence_explanation", attempt=1, exc=exc,
            )
        log.error(f"Explain sentence failed: {exc}")
        return f"Error: {str(exc)}"
