"""
Re-grade all existing episodes with the new JLPT-bank-based analyzer.

Re-runs analyze_transcript() against each episode's stored transcript.json
(never rewritten) and overwrites analysis_<level>.json / highlights_<level>.json
/ cards_<level>.csv in R2. Same shape as the 2026-07-04 vocab-level backfill.

Usage:
    python scripts/backfill_jlpt_levels.py            # all episodes
    python scripts/backfill_jlpt_levels.py 2026-08-02  # one slug, for a quick look
"""
import io
import json
import logging
import mimetypes
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

from lib.analyzer import analyze_transcript
from lib.writer import _write_cards
from web.app import _get_r2, _r2_bucket, _r2_get_json
from web.db import Episode, get_db, init_db

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")


def _put_json(s3, bucket: str, key: str, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def _level_distribution(analysis: dict) -> Counter:
    return Counter(v.get("level", "?") for v in analysis.get("vocab", []))


def backfill_episode(s3, bucket: str, slug: str, r2_prefix: str, level: str):
    """Returns (old_dist, new_dist) Counters, or None on failure."""
    try:
        transcript = _r2_get_json(r2_prefix + "transcript.json")
    except Exception as exc:
        log.error(f"{slug}: could not read transcript.json — {exc}")
        return None

    segments = transcript.get("segments", [])
    if not segments:
        log.warning(f"{slug}: empty transcript, skipping")
        return None

    old_dist = Counter()
    try:
        old_analysis = _r2_get_json(r2_prefix + f"analysis_{level}.json")
        old_dist = _level_distribution(old_analysis)
    except Exception:
        pass  # no prior level-specific analysis — fine, nothing to compare against

    log.info(f"{slug}: re-analyzing {len(segments)} segments (level={level})...")
    analysis = analyze_transcript(segments, level=level)
    new_dist = _level_distribution(analysis)

    # Same three files the app's own "re-analyze at a different level" path
    # writes (web/app.py _pipeline_thread, cloned_level != level branch) —
    # no base (unprefixed) highlights.json; nothing reads it back.
    _put_json(s3, bucket, r2_prefix + f"analysis_{level}.json", analysis)
    _put_json(s3, bucket, r2_prefix + f"highlights_{level}.json", {"highlights": analysis.get("highlights", [])})

    # Cards CSV — regenerate in-memory and upload directly (no local temp file needed).
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=True) as tmp:
        _write_cards(Path(tmp.name), analysis)
        tmp.seek(0)
        mimetypes.add_type("text/csv", ".csv")
        s3.upload_file(tmp.name, bucket, r2_prefix + f"cards_{level}.csv",
                        ExtraArgs={"ContentType": "text/csv"})

    log.info(f"{slug}: before={dict(old_dist) or '(none)'}  after={dict(new_dist)}")
    return old_dist, new_dist


def main() -> None:
    only_slug = sys.argv[1] if len(sys.argv) > 1 else None

    if not init_db():
        log.error("DATABASE_URL not set — cannot enumerate episodes")
        sys.exit(1)
    s3 = _get_r2()
    if s3 is None:
        log.error("R2 not configured — cannot read/write episode analysis files")
        sys.exit(1)
    bucket = _r2_bucket()

    with get_db() as db:
        episodes = [
            (e.slug, e.r2_prefix, e.level) for e in db.query(Episode).all()
            if e.r2_prefix and (only_slug is None or e.slug == only_slug)
        ]

    if not episodes:
        log.warning("No matching episodes with r2_prefix set found.")
        return

    log.info(f"Backfilling {len(episodes)} episode(s)...")
    total_before, total_after = Counter(), Counter()
    ok = 0
    for slug, r2_prefix, level in episodes:
        try:
            result = backfill_episode(s3, bucket, slug, r2_prefix, level)
            if result:
                old_dist, new_dist = result
                total_before += old_dist
                total_after += new_dist
                ok += 1
        except Exception as exc:
            log.error(f"{slug}: FAILED — {exc}")

    log.info(f"\nDone: {ok}/{len(episodes)} episodes re-analyzed.")
    log.info(f"TOTAL before: {dict(total_before)}")
    log.info(f"TOTAL after:  {dict(total_after)}")


if __name__ == "__main__":
    main()
