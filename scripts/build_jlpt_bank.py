"""
Build data/jlpt_bank.json.gz from two CC-BY-SA-4.0 sources:

- stephenmk/yomitan-jlpt-vocab (Jonathan Waller's Tanos JLPT lists,
  cross-referenced against JMdict for standardized spellings)
- scriptin/jmdict-simplified (jmdict-eng-common — "is this a real word?")

Run manually when the upstream data should be refreshed:
    python scripts/build_jlpt_bank.py

See data/README.md for licensing and why these sources were chosen.
"""
import csv
import gzip
import io
import json
import zipfile
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).parent.parent
_OUT_PATH = _PROJECT_ROOT / "data" / "jlpt_bank.json.gz"

_JLPT_CSV_URL = (
    "https://raw.githubusercontent.com/stephenmk/yomitan-jlpt-vocab/main/"
    "original_data/n{level}.csv"
)
_JMDICT_RELEASE = "3.6.2+20260727141257"
_JMDICT_ZIP_URL = (
    "https://github.com/scriptin/jmdict-simplified/releases/download/"
    f"{_JMDICT_RELEASE}/jmdict-eng-common-{_JMDICT_RELEASE}.json.zip"
)


def _build_jlpt_index() -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Return (keyed by 'form\\treading' -> level, keyed by form -> easiest level,
    keyed by 'form\\treading' -> English gloss)."""
    by_key: dict[str, int] = {}
    by_form: dict[str, int] = {}
    en_by_key: dict[str, str] = {}
    for level in (1, 2, 3, 4, 5):
        url = _JLPT_CSV_URL.format(level=level)
        print(f"Fetching N{level} list: {url}")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        print(f"  {len(rows)} rows")
        for row in rows:
            kana = row["kana"].strip()
            kanji = row["kanji"].strip()
            en = row["waller_definition"].strip()
            for form in filter(None, {kanji, kana}):
                key = f"{form}\t{kana}"
                # Higher N number = easier. A word can appear on multiple
                # level lists (rare) — keep the easiest attested level.
                if key not in by_key or level > by_key[key]:
                    by_key[key] = level
                    en_by_key[key] = en
                if form not in by_form or level > by_form[form]:
                    by_form[form] = level
    return by_key, by_form, en_by_key


def _build_common_words() -> list[str]:
    print(f"Fetching JMdict common words: {_JMDICT_ZIP_URL}")
    resp = requests.get(_JMDICT_ZIP_URL, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".json"))
        data = json.loads(zf.read(name))
    forms: set[str] = set()
    for word in data["words"]:
        for k in word.get("kanji", []):
            if k.get("common"):
                forms.add(k["text"])
        for k in word.get("kana", []):
            if k.get("common"):
                forms.add(k["text"])
    print(f"  {len(forms)} common forms (jmdict version {data.get('version')})")
    return sorted(forms)


def main() -> None:
    by_key, by_form, en_by_key = _build_jlpt_index()
    common = _build_common_words()

    bank = {
        "version": "1",
        "jlpt": by_key,
        "jlpt_by_form": by_form,
        "jlpt_en": en_by_key,
        "common": common,
    }

    _OUT_PATH.parent.mkdir(exist_ok=True)
    raw = json.dumps(bank, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(_OUT_PATH, "wb", compresslevel=9) as f:
        f.write(raw)

    print(f"\nWrote {_OUT_PATH} ({_OUT_PATH.stat().st_size:,} bytes gzipped, "
          f"{len(raw):,} bytes raw)")
    print(f"jlpt keys: {len(by_key)}  jlpt forms: {len(by_form)}  common forms: {len(common)}")


if __name__ == "__main__":
    main()
