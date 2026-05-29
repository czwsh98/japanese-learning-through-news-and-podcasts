#!/usr/bin/env python
"""
Restore Episode DB rows from Cloudflare R2.

Use this when the Postgres episodes table has been wiped but R2 still has
all the episode files intact.  The script scans R2 for every meta.json,
reads metadata from each, and inserts the missing Episode rows into Postgres.

Already-existing DB rows (identified by owner_user_id + slug) are left
untouched so any episodes you added after the data loss are preserved.

Usage:
    .venv/bin/python scripts/restore_from_r2.py
    .venv/bin/python scripts/restore_from_r2.py --dry-run
    .venv/bin/python scripts/restore_from_r2.py --user-id <UUID>

How it works:
    R2 keys follow the pattern  episodes/<user_id>/<slug>/meta.json
    The script lists all such keys, downloads each meta.json, looks up
    the matching user in Postgres by UUID, and inserts the Episode row.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m·\033[0m"
WARN = "\033[93m!\033[0m"

# Pattern for valid episode slugs: YYYY-MM-DD or YYYY-MM-DD-N
SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")

# R2 key pattern: episodes/<user_uuid>/<slug>/meta.json
META_KEY_RE = re.compile(
    r"^episodes/([0-9a-f-]{36})/(\d{4}-\d{2}-\d{2}(?:-\d+)?)/meta\.json$"
)

# Inline source_token logic (mirrors _get_source_token in web/app.py)
_YT_ID_RE = re.compile(r"(?:watch\?.*v=|youtu\.be/)([a-zA-Z0-9_-]{11})")


def _get_source_token(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    yt_match = _YT_ID_RE.search(url)
    if yt_match:
        return f"youtube:{yt_match.group(1)}"
    try:
        parsed = urllib.parse.urlparse(url)
        normalized = urllib.parse.urlunparse((
            parsed.scheme.lower(), parsed.netloc.lower(),
            parsed.path, "", "", "",
        ))
    except Exception:
        normalized = url.lower()
    return f"url:{hashlib.sha256(normalized.encode()).hexdigest()}"


def main():
    parser = argparse.ArgumentParser(
        description="Restore Episode DB rows from Cloudflare R2"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be inserted without writing to the DB",
    )
    parser.add_argument(
        "--user-id",
        help="Limit restore to a single user UUID (optional)",
    )
    parser.add_argument(
        "--remap-user-id",
        metavar="OLD_UUID:NEW_UUID",
        action="append",
        default=[],
        help=(
            "Map an old user UUID (from R2 paths) to a current DB user UUID. "
            "Use when you re-registered after data loss. Repeat for multiple remaps. "
            "Example: --remap-user-id 18c99839-...:cb845ae6-..."
        ),
    )
    args = parser.parse_args()

    # Parse --remap-user-id pairs into a dict
    user_id_remap: dict[str, str] = {}
    for pair in args.remap_user_id:
        if ":" not in pair:
            print(f"{FAIL}  Invalid --remap-user-id format (expected OLD:NEW): {pair!r}")
            sys.exit(1)
        old, new = pair.split(":", 1)
        user_id_remap[old.strip()] = new.strip()

    # ── DB connection ─────────────────────────────────────────────────────────
    print("\n── Database ─────────────────────────────────────────────────────────────")
    from web.db import init_db, get_db, User, Episode
    from sqlalchemy import select

    if not init_db():
        print(f"  {FAIL}  DATABASE_URL not set in .env")
        sys.exit(1)
    print(f"  {PASS}  Connected")

    # Pre-load all known users into a dict: uuid_str → User
    with get_db() as db:
        all_users = {
            str(u.id): u
            for u in db.execute(select(User)).scalars().all()
        }
    print(f"  {PASS}  {len(all_users)} user(s) in DB")

    # Pre-load existing (owner_user_id, slug) pairs to detect skips quickly
    with get_db() as db:
        existing_keys = set(
            (str(row.owner_user_id), row.slug)
            for row in db.execute(select(Episode)).scalars().all()
        )
    print(f"  {PASS}  {len(existing_keys)} existing Episode row(s) in DB")

    # ── R2 connection ─────────────────────────────────────────────────────────
    print("\n── Cloudflare R2 ────────────────────────────────────────────────────────")
    import boto3

    r2_endpoint = os.environ.get("R2_ENDPOINT_URL", "")
    r2_key      = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret   = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket   = os.environ.get("R2_BUCKET", "")

    missing = [k for k, v in {
        "R2_ENDPOINT_URL": r2_endpoint,
        "R2_ACCESS_KEY_ID": r2_key,
        "R2_SECRET_ACCESS_KEY": r2_secret,
        "R2_BUCKET": r2_bucket,
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
        print(f"  {FAIL}  Cannot reach R2 bucket: {exc}")
        sys.exit(1)

    # ── Scan R2 for meta.json files ───────────────────────────────────────────
    print("\n── Scanning R2 for episode meta.json files… ─────────────────────────────")

    meta_keys = []  # list of (user_id_str, slug, r2_key)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=r2_bucket, Prefix="episodes/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            m = META_KEY_RE.match(key)
            if not m:
                continue
            user_id_str, slug = m.group(1), m.group(2)
            # Filter by --user-id if provided
            if args.user_id and user_id_str != args.user_id.strip():
                continue
            meta_keys.append((user_id_str, slug, key))

    meta_keys.sort(key=lambda x: (x[0], x[1]))
    print(f"  {PASS}  Found {len(meta_keys)} episode(s) in R2")

    if not meta_keys:
        print("\n  Nothing to restore.")
        sys.exit(0)

    # ── Restore each episode ──────────────────────────────────────────────────
    inserted = skipped = errors = 0

    for user_id_str, slug, meta_key in meta_keys:
        prefix_label = f"episodes/{user_id_str[:8]}…/{slug}"

        # Apply any user-id remap (old UUID → current UUID after re-registration)
        resolved_user_id = user_id_remap.get(user_id_str, user_id_str)

        # Check user exists in DB
        user = all_users.get(resolved_user_id)
        if not user:
            print(f"  {WARN}  {prefix_label} — user {user_id_str} not found in DB, skipping")
            if user_id_str not in user_id_remap:
                print(f"        Hint: if you re-registered, add --remap-user-id {user_id_str}:<your-new-uuid>")
            skipped += 1
            continue

        # Skip if Episode row already exists (use resolved id for the check)
        if (resolved_user_id, slug) in existing_keys:
            print(f"  {INFO}  {prefix_label} — already in DB, skipping")
            skipped += 1
            continue

        # Download meta.json from R2
        try:
            obj = s3.get_object(Bucket=r2_bucket, Key=meta_key)
            meta = json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as exc:
            print(f"  {FAIL}  {prefix_label} — could not read meta.json: {exc}")
            errors += 1
            continue

        title    = meta.get("title", slug)
        channel  = meta.get("channel", "")
        url      = meta.get("url", "")
        duration = int(meta.get("duration") or 0)
        level    = meta.get("level", "advanced")
        source   = meta.get("source", "")
        r2_prefix = f"episodes/{user_id_str}/{slug}/"
        token    = _get_source_token(url or None)

        if args.dry_run:
            print(
                f"  {INFO}  {prefix_label}"
                f"\n        user  : {user.email}"
                f"\n        title : {title[:60]}"
                f"\n        level : {level}  duration: {duration}s"
            )
            inserted += 1
            continue

        # Insert Episode row (owner is the resolved / current user)
        try:
            with get_db() as db:
                ep = Episode(
                    owner_user_id = uuid.UUID(resolved_user_id),
                    slug          = slug,
                    date          = slug[:10],
                    title         = title,
                    channel       = channel,
                    url           = url,
                    thumbnail     = meta.get("thumbnail", ""),
                    duration      = duration,
                    level         = level,
                    source        = source,
                    source_token  = token,
                    r2_prefix     = r2_prefix,
                )
                db.add(ep)
            print(f"  {PASS}  {prefix_label}  — {title[:50]}")
            existing_keys.add((resolved_user_id, slug))  # prevent duplicate if slug appears twice
            inserted += 1
        except Exception as exc:
            print(f"  {FAIL}  {prefix_label} — DB insert failed: {exc}")
            errors += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"""
── Summary {'(dry run) ' if args.dry_run else ''}──────────────────────────────────────────────────────
  restored : {inserted}  episode(s) {"would be " if args.dry_run else ""}inserted into DB
  skipped  : {skipped}  (already in DB or user not found)
  errors   : {errors}
""")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
