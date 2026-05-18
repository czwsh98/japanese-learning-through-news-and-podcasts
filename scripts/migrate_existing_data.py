#!/usr/bin/env python
"""
One-time migration: import existing vocab.json into the Postgres vocab table
under a specific user account.

Run AFTER:
  1. The db branch is deployed to Railway
  2. You have registered your account at your Railway URL (first user = admin)

Usage:
    .venv/bin/python scripts/migrate_existing_data.py --email you@example.com

The script is idempotent — it skips words that already exist for your account.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from sqlalchemy import select

from web.db import User, VocabItem, init_db, get_db

PASS  = "\033[92m✓\033[0m"
FAIL  = "\033[91m✗\033[0m"
INFO  = "\033[94m·\033[0m"


def main():
    parser = argparse.ArgumentParser(description="Migrate vocab.json → Postgres")
    parser.add_argument("--email", required=True, help="Your registered account email")
    parser.add_argument(
        "--vocab-file",
        default=str(Path(__file__).parent.parent / "vocab.json"),
        help="Path to vocab.json (default: project root)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without writing to the DB",
    )
    args = parser.parse_args()

    # ── Connect ───────────────────────────────────────────────────────────────
    print("\n── Connecting to database ───────────────────────────────────────────────")
    if not init_db():
        print(f"  {FAIL}  DATABASE_URL is not set in .env")
        sys.exit(1)
    print(f"  {PASS}  Connected")

    # ── Find user ─────────────────────────────────────────────────────────────
    print(f"\n── Looking up user: {args.email} ───────────────────────────────────────")
    with get_db() as db:
        user = db.execute(
            select(User).where(User.email == args.email.strip().lower())
        ).scalar_one_or_none()

        if user is None:
            print(f"  {FAIL}  No account found for {args.email!r}")
            print("       Register at your Railway URL first, then re-run this script.")
            sys.exit(1)

        print(f"  {PASS}  Found: {user.email}  (admin={user.is_admin}, id={user.id})")
        user_id = user.id

    # ── Read vocab.json ───────────────────────────────────────────────────────
    print(f"\n── Reading {args.vocab_file} ─────────────────────────────────────────")
    vocab_path = Path(args.vocab_file)
    if not vocab_path.exists():
        print(f"  {FAIL}  File not found: {vocab_path}")
        sys.exit(1)

    data  = json.loads(vocab_path.read_text(encoding="utf-8"))
    items = data.get("items", [])
    print(f"  {PASS}  {len(items)} items found")

    if not items:
        print("  Nothing to migrate.")
        sys.exit(0)

    # ── Migrate ───────────────────────────────────────────────────────────────
    print(f"\n── {'DRY RUN — ' if args.dry_run else ''}Migrating to vocab table ─────────────────────────────────────")

    imported = skipped = errors = 0

    with get_db() as db:
        # Load existing words for this user so we can deduplicate
        existing_words = set(
            row[0] for row in db.execute(
                select(VocabItem.word).where(VocabItem.user_id == user_id)
            ).all()
        )
        print(f"  {INFO}  {len(existing_words)} words already in DB for this account\n")

        for item in items:
            word = item.get("word", "").strip()
            if not word:
                errors += 1
                continue

            if word in existing_words:
                print(f"  {INFO}  skip (exists): {word}")
                skipped += 1
                continue

            if args.dry_run:
                print(f"  {PASS}  would import: {word}  [{item.get('level','')}]  {item.get('en','')[:40]}")
                imported += 1
                continue

            try:
                db_item = VocabItem(
                    user_id        = user_id,
                    word           = word,
                    reading        = item.get("reading", ""),
                    en             = item.get("en", ""),
                    zh             = item.get("zh", ""),
                    example        = item.get("example", ""),
                    level          = item.get("level", ""),
                    type           = item.get("type", "vocab"),
                    source_episode = item.get("source_episode", ""),
                )
                db.add(db_item)
                existing_words.add(word)
                print(f"  {PASS}  imported: {word}  [{item.get('level','')}]  {item.get('en','')[:40]}")
                imported += 1
            except Exception as exc:
                print(f"  {FAIL}  error on {word!r}: {exc}")
                errors += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"""
── Summary ──────────────────────────────────────────────────────────────
  {'(dry run) ' if args.dry_run else ''}imported : {imported}
  skipped  : {skipped}  (already existed)
  errors   : {errors}
""")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
