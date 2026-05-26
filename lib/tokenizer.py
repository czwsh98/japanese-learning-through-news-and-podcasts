"""Japanese morphological tokenization and Furigana generation using janome."""
import re
import logging
from janome.tokenizer import Tokenizer

log = logging.getLogger(__name__)

_tokenizer = None
_KANJI_RE = re.compile(r"[\u4e00-\u9faf]")
# Pre-built katakana\u2192hiragana translation table (Unicode code-point shift of 0x60)
_KATA_TO_HIRA = str.maketrans(
    "".join(chr(c) for c in range(0x30A1, 0x30F7)),
    "".join(chr(c - 0x60) for c in range(0x30A1, 0x30F7)),
)


def _get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        log.info("Initializing janome Tokenizer...")
        _tokenizer = Tokenizer()
    return _tokenizer


def has_kanji(text: str) -> bool:
    return bool(_KANJI_RE.search(text))


def katakana_to_hiragana(text: str) -> str:
    return text.translate(_KATA_TO_HIRA)


def tokenize_japanese_text(text: str) -> list[dict]:
    """
    Tokenize Japanese text into word tokens.
    Each token: w (surface), r (hiragana reading), kanji (bool).
    """
    if not text:
        return []

    t = _get_tokenizer()
    tokens = []
    try:
        for token in t.tokenize(text):
            surface = token.surface
            raw_reading = token.reading
            reading = katakana_to_hiragana(raw_reading) if raw_reading and raw_reading != "*" else surface
            tokens.append({"w": surface, "r": reading, "kanji": has_kanji(surface)})
    except Exception as e:
        log.warning(f"Error tokenizing Japanese text: {e}")
        tokens = [{"w": text, "r": text, "kanji": has_kanji(text)}]
    return tokens


def tokenize_segments(segments: list[dict]) -> list[dict]:
    """Add a 'tokens' list to each segment in-place and return the list."""
    for seg in segments:
        if "ja" in seg:
            seg["tokens"] = tokenize_japanese_text(seg["ja"])
    return segments
