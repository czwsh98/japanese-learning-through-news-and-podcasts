#!/usr/bin/env python3
"""
Japanese Learning Pipeline
--------------------------
Downloads the latest episode from each source in sources.json, transcribes it
with Whisper, translates segments with Gemini Flash, identifies N1/N2 vocabulary and
grammar with OpenAI gpt-4o-mini, writes per-episode flat files (including an Anki-importable CSV).

Usage:
  python pipeline.py                     # process today's episode
  python pipeline.py --date 2026-05-01   # reprocess a specific date
  python pipeline.py --url <URL>         # override source
  python pipeline.py --dry-run           # stub all API calls
  python pipeline.py --force             # reprocess even if output files exist
"""
import json
import logging
import os
import sys
import time
from argparse import ArgumentParser
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

for _var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
    if not os.environ.get(_var):
        print(f"ERROR: {_var} not set. Copy .env.example → .env and fill in your keys.")
        sys.exit(1)

from lib.analyzer import analyze_transcript, LEVELS, DEFAULT_LEVEL
from lib.downloader import download_latest
from lib.transcriber import transcribe_audio
from lib.translator import translate_segments
from lib.writer import write_episode_files
from lib.tokenizer import tokenize_segments

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


def _timed(label: str):
    """Context manager that logs elapsed time for a pipeline step."""
    class _Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self
        def __exit__(self, *_):
            elapsed = time.perf_counter() - self.start
            log.info(f"  ⏱  {label} completed in {elapsed:.1f}s")
    return _Timer()


def run(episode_date: date, url_override: str | None, dry_run: bool,
        level: str = DEFAULT_LEVEL, force: bool = False) -> bool:
    ep_dir = EPISODES_DIR / episode_date.isoformat()
    ep_dir.mkdir(parents=True, exist_ok=True)

    _, jlpt_tiers = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])

    bar = "=" * 52
    log.info(bar)
    log.info(f"  Episode date : {episode_date}")
    log.info(f"  Output dir   : {ep_dir}")
    log.info(f"  Level        : {level} ({' / '.join(jlpt_tiers)})")
    log.info(f"  Dry run      : {dry_run}")
    log.info(f"  Force        : {force}")
    log.info(bar)

    pipeline_start = time.perf_counter()

    # ── Checkpoint helpers ───────────────────────────────────────────────────
    transcript_file = ep_dir / "transcript.json"
    analysis_file   = ep_dir / "analysis.json"

    def _has_transcript():
        return not force and transcript_file.exists()

    def _has_translations():
        if force or not transcript_file.exists():
            return False
        try:
            data = json.loads(transcript_file.read_text(encoding="utf-8"))
            segs = data.get("segments", [])
            return segs and all(s.get("en") for s in segs[:3])
        except Exception:
            return False

    def _has_analysis():
        return not force and analysis_file.exists()

    # ── Step 1: Download ────────────────────────────────────────────────────
    log.info("Step 1/5 — Download audio")
    with _timed("Download"):
        urls = [url_override] if url_override else load_source_urls()
        audio_path, meta = download_latest(urls, ep_dir, dry_run=dry_run)
        if not audio_path:
            log.error("Download failed — aborting")
            return False

    # ── Step 2: Transcribe ──────────────────────────────────────────────────
    if _has_transcript() and not dry_run:
        log.info("Step 2/5 — Transcribe (SKIPPED — transcript.json exists, use --force to redo)")
        whisper_result = json.loads(transcript_file.read_text(encoding="utf-8"))
        # Ensure top-level keys exist for downstream steps
        if "segments" not in whisper_result:
            whisper_result = {"segments": whisper_result.get("segments", []),
                              "language": "ja", "duration": 0.0, "text": ""}
    else:
        log.info("Step 2/5 — Transcribe (Whisper API)")
        with _timed("Transcribe"):
            whisper_result = _stub_whisper() if dry_run else transcribe_audio(audio_path)

    # ── Step 3: Translate ───────────────────────────────────────────────────
    if _has_translations() and not dry_run:
        log.info("Step 3/5 — Translate (SKIPPED — translations already present, use --force to redo)")
        segments = json.loads(transcript_file.read_text(encoding="utf-8")).get("segments", [])
    else:
        log.info("Step 3/5 — Translate EN + ZH (Gemini Flash)")
        with _timed("Translate"):
            segments = _stub_segments() if dry_run else translate_segments(whisper_result["segments"])

    log.info("Tokenizing Japanese text for Furigana…")
    segments = tokenize_segments(segments)

    # ── Step 4: Analyze ─────────────────────────────────────────────────────
    if _has_analysis() and not dry_run:
        log.info(f"Step 4/5 — Analyze (SKIPPED — analysis.json exists, use --force to redo)")
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    else:
        log.info(f"Step 4/5 — Analyze {' / '.join(jlpt_tiers)} vocabulary and grammar (OpenAI gpt-4o-mini)")
        with _timed("Analyze"):
            analysis = _stub_analysis() if dry_run else analyze_transcript(segments, level=level)

    # ── Step 5: Write files ─────────────────────────────────────────────────
    log.info("Step 5/5 — Write episode files")
    with _timed("Write files"):
        write_episode_files(ep_dir, meta, segments, analysis, whisper_result)

    total_elapsed = time.perf_counter() - pipeline_start
    log.info(bar)
    log.info(f"  Pipeline complete ✓  ({ep_dir})")
    log.info(f"  Total time: {total_elapsed:.1f}s")
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all steps even if output files already exist",
    )
    args = parser.parse_args()

    episode_date = date.fromisoformat(args.date) if args.date else date.today()
    success = run(episode_date, args.url, args.dry_run, level=args.level, force=args.force)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
