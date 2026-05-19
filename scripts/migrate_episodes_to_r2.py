#!/usr/bin/env python
"""
One-time migration: upload local episodes/ to Cloudflare R2 and create
Episode rows in Postgres under your user account.

Run AFTER you have registered your account on Railway.

Usage:
    .venv/bin/python scripts/migrate_episodes_to_r2.py --email you@example.com
    .venv/bin/python scripts/migrate_episodes_to_r2.py --email you@example.com --dry-run

What it does per episode slug:
  1. Reads meta.json for metadata
  2. Uploads every file (audio, json, vtt, csv) to R2 at
       episodes/{user_id}/{slug}/{filename}
  3. Creates (or skips) an Episode row in Postgres

The R2 key prefix is stored in Episode.r2_prefix so Phase 4 can serve files
from R2 without any further migration.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m·\033[0m"
WARN = "\033[93m!\033[0m"

SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")

EPISODE_FILES = [
    "meta.json",
    "transcript.json",
    "analysis.json",
    "highlights.json",
    "subtitles.vtt",
    "cards.csv",
    "audio.mp3",
    "audio.m4a",
    "audio.wav",
    "audio.ogg",
    "audio.webm",
    "audio.flac",
    "audio.aac",
    "audio.opus",
]

import mimetypes
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("text/csv", ".csv")


def file_mimetype(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


def upload_file(s3, bucket: str, key: str, path: Path, dry_run: bool) -> bool:
    size_kb = path.stat().st_size // 1024
    if dry_run:
        print(f"      {INFO}  would upload: {key}  ({size_kb:,} KB)")
        return True
    try:
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={"ContentType": file_mimetype(path)},
        )
        print(f"      {PASS}  {key}  ({size_kb:,} KB)")
        return True
    except Exception as exc:
        print(f"      {FAIL}  {key}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Migrate local episodes to R2 + Postgres")
    parser.add_argument("--email", required=True, help="Your registered account email")
    parser.add_argument(
        "--episodes-dir",
        default=str(Path(__file__).parent.parent / "episodes"),
        help="Local episodes directory (default: project root/episodes)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing anything")
    args = parser.parse_args()

    episodes_dir = Path(args.episodes_dir)
    if not episodes_dir.exists():
        print(f"{FAIL}  Episodes dir not found: {episodes_dir}")
        sys.exit(1)

    # ── DB connection ─────────────────────────────────────────────────────────
    print("\n── Database ─────────────────────────────────────────────────────────────")
    from web.db import init_db, get_db, User, Episode
    from sqlalchemy import select

    if not init_db():
        print(f"  {FAIL}  DATABASE_URL not set")
        sys.exit(1)
    print(f"  {PASS}  Connected")

    with get_db() as db:
        user = db.execute(
            select(User).where(User.email == args.email.strip().lower())
        ).scalar_one_or_none()
        if not user:
            print(f"  {FAIL}  No account for {args.email!r} — register first")
            sys.exit(1)
        user_id = user.id
        print(f"  {PASS}  User: {user.email}  (admin={user.is_admin})")

    # ── R2 connection ─────────────────────────────────────────────────────────
    print("\n── Cloudflare R2 ────────────────────────────────────────────────────────")
    import boto3

    r2_endpoint = os.environ.get("R2_ENDPOINT_URL", "")
    r2_key      = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret   = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket   = os.environ.get("R2_BUCKET", "")

    missing = [k for k, v in {
        "R2_ENDPOINT_URL": r2_endpoint, "R2_ACCESS_KEY_ID": r2_key,
        "R2_SECRET_ACCESS_KEY": r2_secret, "R2_BUCKET": r2_bucket,
    }.items() if not v]
    if missing:
        for k in missing:
            print(f"  {FAIL}  {k} not set in .env")
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        endpoint_url=r2_endpoint,
        aws_access_key_id=r2_key,
        aws_secret_access_key=r2_secret,
        region_name="auto",
    )
    try:
        s3.head_bucket(Bucket=r2_bucket)
        print(f"  {PASS}  Bucket accessible: {r2_bucket}")
    except Exception as exc:
        print(f"  {FAIL}  Cannot reach bucket: {exc}")
        sys.exit(1)

    # ── Scan local episodes ───────────────────────────────────────────────────
    slugs = sorted(
        [d.name for d in episodes_dir.iterdir()
         if d.is_dir() and SLUG_RE.match(d.name)]
    )
    print(f"\n── Found {len(slugs)} local episode(s) ──────────────────────────────────")
    for s in slugs:
        print(f"  {INFO}  {s}")

    if not slugs:
        print("  Nothing to migrate.")
        sys.exit(0)

    # ── Migrate each episode ──────────────────────────────────────────────────
    imported = skipped = errors = 0

    for slug in slugs:
        print(f"\n  ── {slug} {'(DRY RUN) ' if args.dry_run else ''}─────────────────────────────")
        ep_dir = episodes_dir / slug

        # Read meta.json
        meta_path = ep_dir / "meta.json"
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        r2_prefix = f"episodes/{user_id}/{slug}/"

        # Check if Episode row already exists in DB
        with get_db() as db:
            existing = db.execute(
                select(Episode).where(
                    Episode.owner_user_id == user_id,
                    Episode.slug == slug,
                )
            ).scalar_one_or_none()

        if existing:
            print(f"    {WARN}  Episode row already exists in DB — skipping DB insert")
            db_exists = True
        else:
            db_exists = False

        # Check if already uploaded to R2 (probe for meta.json)
        r2_exists = False
        if not args.dry_run:
            try:
                s3.head_object(Bucket=r2_bucket, Key=f"{r2_prefix}meta.json")
                r2_exists = True
                print(f"    {INFO}  Already in R2 — re-uploading to sync any changes")
            except Exception:
                pass

        # Upload files
        upload_ok = True
        for filename in EPISODE_FILES:
            fpath = ep_dir / filename
            if not fpath.exists():
                continue
            key = f"{r2_prefix}{filename}"
            ok = upload_file(s3, r2_bucket, key, fpath, args.dry_run)
            if not ok:
                upload_ok = False
                errors += 1

        if not upload_ok:
            print(f"    {FAIL}  Some files failed to upload — skipping DB row")
            errors += 1
            continue

        # Create Episode row in DB
        if not db_exists and not args.dry_run:
            date_str = slug[:10]  # YYYY-MM-DD
            with get_db() as db:
                ep_row = Episode(
                    owner_user_id = user_id,
                    slug          = slug,
                    date          = date_str,
                    title         = meta.get("title", slug),
                    channel       = meta.get("channel", ""),
                    url           = meta.get("url", ""),
                    thumbnail     = meta.get("thumbnail", ""),
                    duration      = meta.get("duration", 0),
                    level         = meta.get("level", "advanced"),
                    source        = meta.get("source", ""),
                    r2_prefix     = r2_prefix,
                )
                db.add(ep_row)
            print(f"    {PASS}  Episode row created in DB")
            imported += 1
        elif not db_exists and args.dry_run:
            print(f"    {INFO}  would create Episode row: slug={slug}  title={meta.get('title', '')[:40]}")
            imported += 1
        else:
            skipped += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"""
── Summary {'(dry run) ' if args.dry_run else ''}──────────────────────────────────────────────────────
  imported : {imported}  episodes
  skipped  : {skipped}  (already existed in DB)
  errors   : {errors}
""")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
