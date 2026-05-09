#!/usr/bin/env python3
"""
Japanese Learning Pipeline
--------------------------
Downloads the latest episode from each source in sources.json, transcribes it
with Whisper, translates segments with Claude, identifies N1/N2 vocabulary and
grammar with Claude, writes per-episode flat files (including an Anki-importable CSV).

Usage:
  python pipeline.py                     # process today's episode
  python pipeline.py --date 2026-05-01   # reprocess a specific date
  python pipeline.py --url <URL>         # override source
  python pipeline.py --dry-run           # stub all API calls
"""
import json
import logging
import os
import sys
from argparse import ArgumentParser
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

for _var in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
    if not os.environ.get(_var):
        print(f"ERROR: {_var} not set. Copy .env.example → .env and fill in your keys.")
        sys.exit(1)

from lib.analyzer import analyze_transcript, LEVELS, DEFAULT_LEVEL
from lib.downloader import download_latest
from lib.transcriber import transcribe_audio
from lib.translator import translate_segments
from lib.writer import write_episode_files

_PROJECT_ROOT = Path(__file__).parent
_episodes_env = os.environ.get("EPISODES_DIR", "")
EPISODES_DIR = (Path(_episodes_env) if Path(_episodes_env).is_absolute()
                else _PROJECT_ROOT / (_episodes_env or "episodes"))
SOURCES_FILE = _PROJECT_ROOT / "sources.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def load_source_urls() -> list[str]:
    if not SOURCES_FILE.exists():
        log.error("sources.json not found — see sources.json example in the repo")
        sys.exit(1)
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    urls = [s["url"] for s in data.get("sources", [])]
    if not urls:
        log.error("sources.json has no 'sources' entries")
        sys.exit(1)
    return urls


def _stub_whisper() -> dict:
    return {
        "language": "ja",
        "duration": 60.0,
        "text": "これはテストです。日本語の勉強をしましょう。",
        "segments": [
            {"index": 0, "start": 0.0, "end": 3.5, "ja": "これはテストです。"},
            {"index": 1, "start": 3.5, "end": 7.0, "ja": "日本語の勉強をしましょう。"},
        ],
    }


def _stub_segments() -> list[dict]:
    return [
        {"index": 0, "start": 0.0, "end": 3.5, "time": "00:00:00",
         "ja": "これはテストです。", "en": "This is a test.", "zh": "这是一个测试。"},
        {"index": 1, "start": 3.5, "end": 7.0, "time": "00:00:03",
         "ja": "日本語の勉強をしましょう。", "en": "Let's study Japanese.", "zh": "我们来学日语吧。"},
    ]


def _stub_analysis() -> dict:
    return {"highlights": [], "vocab": [], "grammar": [], "expressions": []}


def run(episode_date: date, url_override: str | None, dry_run: bool, level: str = DEFAULT_LEVEL) -> bool:
    ep_dir = EPISODES_DIR / episode_date.isoformat()
    ep_dir.mkdir(parents=True, exist_ok=True)

    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])

    bar = "=" * 52
    log.info(bar)
    log.info(f"  Episode date : {episode_date}")
    log.info(f"  Output dir   : {ep_dir}")
    log.info(f"  Level        : {level} ({' / '.join(jlpt_tiers)})")
    log.info(f"  Dry run      : {dry_run}")
    log.info(bar)

    # ── Step 1: Download ────────────────────────────────────────────────────
    log.info("Step 1/5 — Download audio")
    urls = [url_override] if url_override else load_source_urls()
    audio_path, meta = download_latest(urls, ep_dir, dry_run=dry_run)
    if not audio_path:
        log.error("Download failed — aborting")
        return False

    # ── Step 2: Transcribe ──────────────────────────────────────────────────
    log.info("Step 2/5 — Transcribe (Whisper API)")
    whisper_result = _stub_whisper() if dry_run else transcribe_audio(audio_path)

    # ── Step 3: Translate ───────────────────────────────────────────────────
    log.info("Step 3/5 — Translate EN + ZH (Claude — Call 1)")
    segments = _stub_segments() if dry_run else translate_segments(whisper_result["segments"])

    # ── Step 4: Analyze ─────────────────────────────────────────────────────
    log.info(f"Step 4/5 — Analyze {' / '.join(jlpt_tiers)} vocabulary and grammar (Claude — Call 2)")
    analysis = _stub_analysis() if dry_run else analyze_transcript(segments, level=level)

    # ── Step 5: Write files ─────────────────────────────────────────────────
    log.info("Step 5/5 — Write episode files")
    write_episode_files(ep_dir, meta, segments, analysis, whisper_result)

    log.info(bar)
    log.info(f"  Pipeline complete ✓  ({ep_dir})")
    log.info(bar)
    return True


def main() -> None:
    parser = ArgumentParser(description="Japanese learning pipeline")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Episode date (default: today)")
    parser.add_argument("--url", metavar="URL", help="Override source URL for this run")
    parser.add_argument(
        "--level",
        default=DEFAULT_LEVEL,
        choices=list(LEVELS.keys()),
        help="JLPT level focus for vocabulary/grammar analysis (default: advanced)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip all API calls — write stub files to verify file layout",
    )
    args = parser.parse_args()

    episode_date = date.fromisoformat(args.date) if args.date else date.today()
    success = run(episode_date, args.url, args.dry_run, level=args.level)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
