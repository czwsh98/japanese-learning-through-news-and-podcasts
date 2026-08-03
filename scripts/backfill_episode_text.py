#!/usr/bin/env python3
"""Rebuild translation and analysis for one persisted episode.

The original Japanese transcript remains the source of truth. All replacement
objects are built and validated before any live R2 key is changed. Existing
objects are copied to a timestamped backup prefix first.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.analyzer import analyze_transcript, sanitize_analysis_result
from lib.tokenizer import tokenize_segments
from lib.translator import ensure_complete_translations, translate_segments
from lib.writer import _write_cards, _write_json
from web.app import _get_r2, _r2_bucket, _r2_get_json
from web.db import Episode, get_db


def _source_segments(transcript: dict) -> list[dict]:
    segments = []
    for position, segment in enumerate(transcript.get("segments", [])):
        text = str(segment.get("ja", "")).strip()
        if not text:
            continue
        segments.append({
            "index": int(segment.get("index", position)),
            "start": float(segment.get("start", 0)),
            "end": float(segment.get("end", segment.get("start", 0))),
            "ja": text,
        })
    if not segments:
        raise RuntimeError("Persisted transcript has no Japanese segments")
    return segments


def run(slug: str, *, clean_only: bool = False) -> None:
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

    transcript = _r2_get_json(f"{prefix}transcript.json")
    if clean_only:
        translated = transcript.get("segments", [])
        ensure_complete_translations(translated)
        analysis = sanitize_analysis_result(_r2_get_json(f"{prefix}analysis.json"))
        translation_seconds = 0.0
        analysis_seconds = 0.0
    else:
        raw_segments = _source_segments(transcript)
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

    with tempfile.TemporaryDirectory(prefix="mimichan_backfill_") as tmp:
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

        for path in generated:
            staging_key = f"{staging_prefix}{path.name}"
            s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": staging_key},
                Key=f"{prefix}{path.name}",
                ContentType=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                MetadataDirective="REPLACE",
            )

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
    parser.add_argument("--clean-only", action="store_true")
    args = parser.parse_args()
    run(args.slug, clean_only=args.clean_only)
