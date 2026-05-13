"""Write per-episode flat files: meta, transcript, subtitles, highlights, analysis, cards."""
import csv
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CSV_FIELDS = ["type", "front", "back", "reading", "en", "zh", "register", "level", "example", "tags"]


def write_episode_files(
    episode_dir: Path,
    meta: dict,
    segments: list[dict],
    analysis: dict,
    whisper_result: dict,
) -> None:
    _write_json(episode_dir / "meta.json", meta)
    _write_json(episode_dir / "transcript.json", {"segments": segments})
    _write_vtt(episode_dir / "subtitles.vtt", segments)
    _write_json(episode_dir / "highlights.json", {"highlights": analysis.get("highlights", [])})
    _write_json(episode_dir / "analysis.json", analysis)
    _write_cards(episode_dir / "cards.csv", analysis)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  wrote {path.name}")


def _write_vtt(path: Path, segments: list[dict]) -> None:
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, 1):
        lines += [
            str(i),
            f"{_vtt_time(seg['start'])} --> {_vtt_time(seg['end'])}",
            seg["ja"],
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"  wrote {path.name}")


def _vtt_time(seconds: float) -> str:
    # Handle rounding to 3 decimal places to avoid 60.000s
    milli = int(round(seconds * 1000))
    s, ms = divmod(milli, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _write_cards(path: Path, analysis: dict) -> None:
    rows: list[dict] = []

    for v in analysis.get("vocab", []):
        word = v.get("word", "").strip()
        en   = v.get("en", "").strip()
        zh   = v.get("zh", "").strip()
        if not word or not en:
            log.warning(f"Skipping incomplete vocab card: {v}")
            continue
        lvl = (v.get("level") or "").strip()
        rows.append({
            "type":     "vocab",
            "front":    word,
            "back":     f"{en} / {zh}",
            "reading":  v.get("reading", ""),
            "en":       en,
            "zh":       zh,
            "register": v.get("register", ""),
            "level":    lvl,
            "example":  v.get("example", ""),
            "tags":     " ".join(t for t in ["japanese", "vocab", lvl] if t),
        })

    for g in analysis.get("grammar", []):
        pattern    = g.get("pattern", "").strip()
        meaning_en = g.get("meaning_en", "").strip()
        meaning_zh = g.get("meaning_zh", "").strip()
        if not pattern or not meaning_en:
            log.warning(f"Skipping incomplete grammar card: {g}")
            continue
        lvl = (g.get("level") or "").strip()
        rows.append({
            "type":     "grammar",
            "front":    pattern,
            "back":     f"{meaning_en} / {meaning_zh}",
            "reading":  g.get("reading", ""),
            "en":       meaning_en,
            "zh":       meaning_zh,
            "register": "",
            "level":    lvl,
            "example":  g.get("example", ""),
            "tags":     " ".join(t for t in ["japanese", "grammar", lvl] if t),
        })

    for e in analysis.get("expressions", []):
        expression = e.get("expression", "").strip()
        en         = e.get("en", "").strip()
        zh         = e.get("zh", "").strip()
        if not expression or not en:
            log.warning(f"Skipping incomplete expression card: {e}")
            continue
        rows.append({
            "type":     "expression",
            "front":    expression,
            "back":     f"{en} / {zh}",
            "reading":  e.get("reading", ""),
            "en":       en,
            "zh":       zh,
            "register": "",
            "level":    "",
            "example":  e.get("context", ""),
            "tags":     "japanese expression",
        })

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info(f"  wrote {path.name} ({len(rows)} cards)")
