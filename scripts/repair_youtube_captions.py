#!/usr/bin/env python3
"""Rebuild the Japanese transcript (and downstream translation/analysis) for
one persisted YouTube-sourced episode, using the fixed caption-cue merge in
lib.transcriber.fetch_youtube_transcript.

Unlike backfill_episode_text.py, this re-derives the JA segments themselves
(the old ones came from unmerged, overlapping auto-caption cues), so segment
indices shift. Saved vocab occurrences keep their own immutable text/EN/ZH
snapshot (VocabOccurrence.source_text etc.), so this is safe — only the
"jump to source line" deep link for pre-existing saves may point at a
slightly different line afterward.

All replacement objects are built and validated before any live R2 key is
changed. Existing objects are copied to a timestamped backup prefix first.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.analyzer import analyze_transcript
from lib.tokenizer import tokenize_segments
from lib.transcriber import fetch_youtube_transcript
from lib.translator import ensure_complete_translations, translate_segments
from lib.writer import _write_cards, _write_json
from web.app import _get_r2, _r2_bucket, _r2_get_json
from web.db import Episode, get_db

_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


def run(slug: str) -> None:
    with get_db() as db:
        episode = (
            db.query(Episode)
            .filter(Episode.slug == slug)
            .order_by(Episode.created_at.desc())
            .first()
        )
        if not episode or not episode.r2_prefix:
            raise RuntimeError(f"No R2-backed episode found for slug {slug}")
        prefix = episode.r2_prefix
        level = episode.level

    meta = _r2_get_json(f"{prefix}meta.json")
    video_id = meta.get("video_id") or ""
    if not video_id:
        match = _YT_ID_RE.search(meta.get("url", ""))
        video_id = match.group(1) if match else ""
    if not video_id:
        raise RuntimeError(f"Episode {slug} has no video_id — not a YouTube episode?")

    print(f"Re-fetching captions for video {video_id} ...")
    whisper_result = fetch_youtube_transcript(video_id)
    if whisper_result is None:
        raise RuntimeError("YouTube captions unavailable — cannot repair")
    raw_segments = whisper_result["segments"]
    print(f"Got {len(raw_segments)} merged segments (was overlapping/unmerged before)")

    started = time.perf_counter()
    translated = translate_segments(raw_segments)
    ensure_complete_translations(translated)
    translated = tokenize_segments(translated)
    translation_seconds = time.perf_counter() - started

    analysis_started = time.perf_counter()
    analysis = analyze_transcript(translated, level=level)
    analysis_seconds = time.perf_counter() - analysis_started

    if not any(analysis.get(key) for key in ("vocab", "grammar", "expressions")):
        raise RuntimeError("Analysis completed without any sidebar items; refusing to publish")

    with tempfile.TemporaryDirectory(prefix="mimichan_repair_") as tmp:
        output = Path(tmp)
        files = {
            "transcript.json": {"segments": translated},
            "analysis.json": analysis,
            "highlights.json": {"highlights": analysis.get("highlights", [])},
            f"analysis_{level}.json": analysis,
            f"highlights_{level}.json": {"highlights": analysis.get("highlights", [])},
        }
        for filename, payload in files.items():
            _write_json(output / filename, payload)
        _write_cards(output / "cards.csv", analysis)
        _write_cards(output / f"cards_{level}.csv", analysis)

        # subtitles.vtt regenerated from the new segment timings.
        from lib.writer import _write_vtt
        _write_vtt(output / "subtitles.vtt", translated)

        generated = sorted(path for path in output.iterdir() if path.is_file())
        s3 = _get_r2()
        bucket = _r2_bucket()
        if not s3 or not bucket:
            raise RuntimeError("R2 is not configured")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_prefix = f"repairs/{slug}/{stamp}/original/"
        staging_prefix = f"repairs/{slug}/{stamp}/staging/"

        for path in generated:
            live_key = f"{prefix}{path.name}"
            try:
                s3.copy_object(
                    Bucket=bucket,
                    CopySource={"Bucket": bucket, "Key": live_key},
                    Key=f"{backup_prefix}{path.name}",
                )
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"404", "NoSuchKey", "NotFound"}:
                    raise

            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            s3.upload_file(
                str(path), bucket, f"{staging_prefix}{path.name}",
                ExtraArgs={"ContentType": content_type},
            )

        promoted = []
        try:
            for path in generated:
                staging_key = f"{staging_prefix}{path.name}"
                # Record before the request: a client-side timeout can happen
                # after R2 has already completed the copy.
                promoted.append(path.name)
                s3.copy_object(
                    Bucket=bucket,
                    CopySource={"Bucket": bucket, "Key": staging_key},
                    Key=f"{prefix}{path.name}",
                    ContentType=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    MetadataDirective="REPLACE",
                )
        except Exception:
            for filename in promoted:
                original_key = f"{backup_prefix}{filename}"
                live_key = f"{prefix}{filename}"
                try:
                    s3.copy_object(
                        Bucket=bucket,
                        CopySource={"Bucket": bucket, "Key": original_key},
                        Key=live_key,
                        MetadataDirective="COPY",
                    )
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code", ""))
                    if code in {"404", "NoSuchKey", "NotFound"}:
                        s3.delete_object(Bucket=bucket, Key=live_key)
                    else:
                        raise
            raise

        s3.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": f"{staging_prefix}{path.name}"} for path in generated],
            "Quiet": True,
        })

    print(json.dumps({
        "slug": slug,
        "segments": len(translated),
        "blank_en": sum(not segment.get("en") for segment in translated),
        "blank_zh": sum(not segment.get("zh") for segment in translated),
        "vocab": len(analysis.get("vocab", [])),
        "grammar": len(analysis.get("grammar", [])),
        "expressions": len(analysis.get("expressions", [])),
        "translation_seconds": round(translation_seconds, 3),
        "analysis_seconds": round(analysis_seconds, 3),
        "backup_prefix": backup_prefix,
    }, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    args = parser.parse_args()
    run(args.slug)
