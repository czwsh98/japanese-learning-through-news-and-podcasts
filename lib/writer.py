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
        lvl = (v.get("level") or "").strip()
        rows.append({
            "type": "vocab",
            "front": v["word"],
            "back": f"{v['en']} / {v['zh']}",
            "reading": v.get("reading", ""),
            "en": v["en"],
            "zh": v["zh"],
            "register": v.get("register", ""),
            "level": lvl,
            "example": v.get("example", ""),
            "tags": " ".join([t for t in ["japanese", "vocab", lvl] if t]).strip(),
        })

    for g in analysis.get("grammar", []):
        lvl = (g.get("level") or "").strip()
        rows.append({
            "type": "grammar",
            "front": g["pattern"],
            "back": f"{g['meaning_en']} / {g['meaning_zh']}",
            "reading": g.get("reading", ""),
            "en": g["meaning_en"],
            "zh": g["meaning_zh"],
            "register": "",
            "level": lvl,
            "example": g.get("example", ""),
            "tags": " ".join([t for t in ["japanese", "grammar", lvl] if t]).strip(),
        })

    for e in analysis.get("expressions", []):
        rows.append({
            "type": "expression",
            "front": e["expression"],
            "back": f"{e['en']} / {e['zh']}",
            "reading": e.get("reading", ""),
            "en": e["en"],
            "zh": e["zh"],
            "register": "",
            "level": "",
            "example": e.get("context", ""),
            "tags": "japanese expression",
        })

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info(f"  wrote {path.name} ({len(rows)} cards)")
