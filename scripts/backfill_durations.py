#!/usr/bin/env python3
"""Fill zero episode durations from the final R2 transcript segment."""
import json
import sys
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.app import _get_r2, _r2_bucket
from web.db import Episode, db_available, get_db


def main():
    if not db_available():
        raise SystemExit("Database is not configured")
    r2 = _get_r2()
    if r2 is None:
        raise SystemExit("R2 is not configured")

    with get_db() as db:
        episodes = db.execute(
            select(Episode).where(Episode.duration == 0, Episode.r2_prefix != "")
        ).scalars().all()

    updated = 0
    skipped = 0
    for episode in episodes:
        try:
            obj = r2.get_object(
                Bucket=_r2_bucket(),
                Key=f"{episode.r2_prefix}transcript.json",
            )
            transcript = json.loads(obj["Body"].read())
            segments = transcript.get("segments") or []
            duration = int(round(float(segments[-1].get("end", 0)))) if segments else 0
        except Exception as exc:
            print(f"skip {episode.slug}: {exc}")
            skipped += 1
            continue

        if duration <= 0:
            print(f"skip {episode.slug}: transcript has no usable final end time")
            skipped += 1
            continue

        with get_db() as db:
            result = db.execute(
                update(Episode)
                .where(Episode.id == episode.id, Episode.duration == 0)
                .values(duration=duration)
            )
        if result.rowcount:
            print(f"updated {episode.slug}: {duration}s")
            updated += 1

    print(f"done: {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    main()
