"""
JLPT level lookup backed by data/jlpt_bank.json.gz (see data/README.md).

Replaces asking the LLM to grade vocabulary — the LLM invents non-words and
mis-grades words it does grade (see analyzer.py history). This module answers
two narrow, deterministic questions instead: "what JLPT level is this word,
if any" and "is this a real word at all."
"""
import gzip
import json
import logging
from pathlib import Path
from typing import Optional

from lib.tokenizer import analyze_tokens

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_BANK_PATH = _PROJECT_ROOT / "data" / "jlpt_bank.json.gz"

_bank: Optional[dict] = None

# Potential-form endings to retry against the dictionary form on a miss.
# janome's base_form usually already gives the dictionary form, but
# occasionally leaves potential-form conjugations untransformed
# (e.g. 買える stays 買える instead of reducing to 買う).
_POTENTIAL_SUFFIXES = ("える", "られる", "れる")

# Frequent function-word forms janome tags as content words (adverbs,
# conjunctions) but that are not JLPT-gradable vocabulary in their own right.
STOP_FORMS = frozenset({
    "そういう", "こういう", "どういう", "ああいう",
    "なので", "ない", "ある程度",
})

CONTENT_POS = frozenset({"名詞", "動詞", "形容詞", "副詞", "連体詞", "接続詞"})
SKIP_POS_SUB = frozenset({"非自立", "代名詞", "数", "接尾", "固有名詞"})

_JP_CHAR_RANGES = ((0x3040, 0x30FF), (0x4E00, 0x9FFF))  # hiragana/katakana, kanji


def _has_japanese_char(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _JP_CHAR_RANGES) for ch in text)


def _load() -> dict:
    global _bank
    if _bank is not None:
        return _bank
    if not _BANK_PATH.exists():
        raise FileNotFoundError(
            f"{_BANK_PATH} not found — run `python scripts/build_jlpt_bank.py` first."
        )
    with gzip.open(_BANK_PATH, "rt", encoding="utf-8") as f:
        data = json.load(f)
    _bank = {
        "jlpt": data["jlpt"],
        "jlpt_by_form": data["jlpt_by_form"],
        "jlpt_en": data.get("jlpt_en", {}),
        "common": frozenset(data["common"]),
    }
    log.info(
        "Loaded JLPT bank v%s: %d keys, %d forms, %d common words",
        data.get("version", "?"), len(_bank["jlpt"]), len(_bank["jlpt_by_form"]),
        len(_bank["common"]),
    )
    return _bank


def lookup(form: str, reading: str = "") -> Optional[int]:
    """Return the JLPT level (1-5, easiest attested) for a form/reading, or None."""
    bank = _load()
    if reading:
        exact = bank["jlpt"].get(f"{form}\t{reading}")
        if exact is not None:
            return exact
    return bank["jlpt_by_form"].get(form)


def gloss(form: str, reading: str = "") -> str:
    """Return the English gloss for a bank entry, if any."""
    bank = _load()
    if reading:
        en = bank["jlpt_en"].get(f"{form}\t{reading}")
        if en:
            return en
    # Fall back to any entry for this form regardless of reading.
    prefix = f"{form}\t"
    for key, en in bank["jlpt_en"].items():
        if key.startswith(prefix):
            return en
    return ""


def is_real_word(form: str) -> bool:
    """Return True if `form` is a common JMdict entry (real word, not tokenizer noise)."""
    return form in _load()["common"]


def _strip_potential(form: str) -> Optional[str]:
    for suf in _POTENTIAL_SUFFIXES:
        if form.endswith(suf) and len(form) > len(suf):
            return form[: -len(suf)] + "る"
    return None


def grade_token(base_form: str, reading: str) -> Optional[int]:
    """Look up a token's JLPT level, retrying the potential-form → dictionary-form
    conversion on a miss."""
    level = lookup(base_form, reading)
    if level is not None:
        return level
    alt = _strip_potential(base_form)
    return lookup(alt) if alt else None


def scan(segments: list[dict], tiers: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Walk segments' Japanese text, grade every content-word lemma against the
    JLPT bank, and split into:
      - candidates:     lemmas whose level falls in `tiers` (e.g. ["N2","N1"])
      - ctx_candidates: lemmas not in the JLPT bank but real JMdict words
                        (domain vocabulary worth surfacing as context-specific)
    Tokenizer noise (neither in the bank nor a real word) is dropped.

    Each returned dict: {word (lemma), reading, level, en, count, surfaces, example}
    - surfaces: distinct inflected surface forms seen (for highlighting the
      text as it actually appears, not the dictionary form)
    - example: the Japanese text of the segment the word first appeared in
    """
    tier_nums = {int(t[1:]) for t in tiers}  # "N2" -> 2
    cand: dict[str, dict] = {}
    ctx: dict[str, dict] = {}

    for seg in segments:
        text = seg.get("ja", "")
        if not text:
            continue
        for tok in analyze_tokens(text):
            pos = tok["pos"].split(",")
            if pos[0] not in CONTENT_POS:
                continue
            if len(pos) > 1 and pos[1] in SKIP_POS_SUB:
                continue

            base = tok["base_form"] if tok["base_form"] != "*" else tok["surface"]
            if not base or base in STOP_FORMS or not _has_japanese_char(base):
                continue

            level = grade_token(base, tok["reading"])
            bucket, entry_level = (
                (cand, f"N{level}") if level is not None and level in tier_nums else
                (ctx, "context-specific") if level is None and is_real_word(base) else
                (None, None)
            )
            if bucket is None:
                continue  # not in the target band, not a real word — tokenizer noise

            entry = bucket.get(base)
            if entry is None:
                entry = bucket[base] = {
                    "word": base,
                    "reading": tok["reading"],
                    "level": entry_level,
                    "en": gloss(base, tok["reading"]),
                    "count": 0,
                    "surfaces": [],
                    "example": text,
                }
            entry["count"] += 1
            if tok["surface"] not in entry["surfaces"]:
                entry["surfaces"].append(tok["surface"])

    candidates = sorted(cand.values(), key=lambda x: -x["count"])
    ctx_candidates = sorted(ctx.values(), key=lambda x: -x["count"])[:40]
    return candidates, ctx_candidates
