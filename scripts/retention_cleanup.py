#!/usr/bin/env python3
"""Soft-delete completed episodes after 30 days and purge trash after 7 more."""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.app import _purge_episode
from web.db import Episode, db_available, get_db


def run(apply=False):
    if not db_available():
        raise SystemExit("Database is not configured")

    now = datetime.now(timezone.utc)
    purge_cutoff = now - timedelta(days=7)
    with get_db() as db:
        to_trash = db.execute(
            select(Episode).where(
                Episode.completed_at.is_not(None),
                Episode.retention_exempt.is_(False),
                Episode.delete_after.is_not(None),
                Episode.delete_after <= now,
                Episode.deleted_at.is_(None),
            ).order_by(Episode.delete_after)
        ).scalars().all()
        to_purge = db.execute(
            select(Episode).where(
                Episode.deleted_at.is_not(None),
                Episode.deleted_at <= purge_cutoff,
            ).order_by(Episode.deleted_at)
        ).scalars().all()

    mode = "APPLY" if apply else "DRY RUN"
    print(f"{mode}: {len(to_trash)} to trash, {len(to_purge)} to purge")
    for episode in to_trash:
        print(f"trash {episode.slug}: {episode.title[:80]}")
    for episode in to_purge:
        print(f"purge {episode.slug}: {episode.title[:80]}")
    if not apply:
        return

    if to_trash:
        ids = [episode.id for episode in to_trash]
        with get_db() as db:
            db.execute(
                update(Episode)
                .where(
                    Episode.id.in_(ids),
                    Episode.deleted_at.is_(None),
                    Episode.retention_exempt.is_(False),
                )
                .values(deleted_at=now)
            )

    errors = 0
    for episode in to_purge:
        try:
            _purge_episode(episode)
        except Exception as exc:
            errors += 1
            print(f"ERROR purging {episode.slug}: {exc}", file=sys.stderr)
    print(f"done: {len(to_trash)} trashed, {len(to_purge) - errors} purged, {errors} errors")
    if errors:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform changes (default is a read-only dry run)",
    )
    args = parser.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    main()
