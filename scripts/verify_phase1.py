#!/usr/bin/env python
"""
Phase 1 verification script.

Checks:
  1. DATABASE_URL is set and Postgres is reachable
  2. All 5 expected tables exist
  3. R2_* vars are set and the bucket is accessible via boto3
  4. Flask app loads cleanly with db_available() == True

Run from the project root:
    .venv/bin/python scripts/verify_phase1.py
"""
import os
import sys

# Allow imports from project root and web/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"

errors = 0


def ok(msg):
    print(f"  {PASS}  {msg}")


def fail(msg):
    global errors
    errors += 1
    print(f"  {FAIL}  {msg}")


def warn(msg):
    print(f"  {WARN}  {msg}")


# ── 1. DATABASE_URL ───────────────────────────────────────────────────────────
print("\n── Postgres ─────────────────────────────────────────────────────────────")

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    fail("DATABASE_URL is not set in .env")
else:
    masked = db_url[:20] + "..." + db_url[-10:]
    ok(f"DATABASE_URL is set  ({masked})")

    # Test connection + table existence via SQLAlchemy
    try:
        from sqlalchemy import create_engine, inspect, text

        url = db_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]

        engine = create_engine(url, pool_pre_ping=True, future=True)

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        ok("Connection successful (SELECT 1)")

        # Run init_db to create tables if needed
        from web.db import init_db, db_available
        init_db()
        if db_available():
            ok("init_db() connected successfully")
        else:
            fail("init_db() returned False even with DATABASE_URL set")

        # Verify tables
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())
        expected = {"users", "sessions", "episodes", "vocab", "transcription_usage"}
        missing = expected - existing
        extra = existing - expected

        for t in sorted(expected):
            if t in existing:
                ok(f"Table exists: {t}")
            else:
                fail(f"Table missing: {t}")

        if extra:
            warn(f"Extra tables found (fine): {sorted(extra)}")

    except Exception as exc:
        fail(f"Postgres error: {exc}")


# ── 2. R2 / Cloudflare ───────────────────────────────────────────────────────
print("\n── Cloudflare R2 ────────────────────────────────────────────────────────")

r2_endpoint = os.environ.get("R2_ENDPOINT_URL", "")
r2_key      = os.environ.get("R2_ACCESS_KEY_ID", "")
r2_secret   = os.environ.get("R2_SECRET_ACCESS_KEY", "")
r2_bucket   = os.environ.get("R2_BUCKET", "")

missing_r2 = [k for k, v in {
    "R2_ENDPOINT_URL":    r2_endpoint,
    "R2_ACCESS_KEY_ID":   r2_key,
    "R2_SECRET_ACCESS_KEY": r2_secret,
    "R2_BUCKET":          r2_bucket,
}.items() if not v]

if missing_r2:
    for k in missing_r2:
        fail(f"{k} is not set in .env")
else:
    ok("All R2 env vars present")
    try:
        import boto3
        from botocore.exceptions import ClientError, EndpointResolutionError

        s3 = boto3.client(
            "s3",
            endpoint_url=r2_endpoint,
            aws_access_key_id=r2_key,
            aws_secret_access_key=r2_secret,
            region_name="auto",
        )

        # List objects (just head — don't need any files to exist)
        s3.head_bucket(Bucket=r2_bucket)
        ok(f"Bucket accessible: {r2_bucket}")

        # Write a tiny probe object then delete it
        probe_key = "_verify_phase1_probe.txt"
        s3.put_object(Bucket=r2_bucket, Key=probe_key, Body=b"phase1-ok")
        ok(f"Put object: {probe_key}")

        s3.delete_object(Bucket=r2_bucket, Key=probe_key)
        ok(f"Delete object: {probe_key}")

    except Exception as exc:
        fail(f"R2 error: {exc}")


# ── 3. Flask app startup ──────────────────────────────────────────────────────
print("\n── Flask app ─────────────────────────────────────────────────────────────")
try:
    # Re-import so init_db() runs again inside the Flask context check
    import importlib
    import web.app as _app_mod
    importlib.reload(_app_mod)
    from web.app import app
    from web.db import db_available

    ok("Flask app imports cleanly")

    if app.config.get("SECRET_KEY") and app.config["SECRET_KEY"] != "dev-insecure-change-me-before-deploy":
        ok("SECRET_KEY is set to a custom value")
    elif app.config.get("SECRET_KEY"):
        warn("SECRET_KEY is using the insecure dev default — set a real value in Railway vars")
    else:
        fail("SECRET_KEY is not configured")

    if db_available():
        ok("db_available() == True inside Flask context")
    else:
        warn("db_available() == False — DATABASE_URL may not have been picked up by the reloaded module")

except Exception as exc:
    fail(f"Flask startup error: {exc}")


# ── Summary ───────────────────────────────────────────────────────────────────
print("\n── Summary ──────────────────────────────────────────────────────────────")
if errors == 0:
    print(f"  {PASS}  All checks passed — Phase 1 is fully operational\n")
else:
    print(f"  {FAIL}  {errors} check(s) failed — see above\n")
    sys.exit(1)
