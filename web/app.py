"""Flask web UI for the Japanese Learning Pipeline — localhost:5000."""
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# Make lib/ importable when running web/app.py directly
sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_ROOT = Path(__file__).parent.parent
_episodes_env = os.environ.get("EPISODES_DIR", "")
EPISODES_DIR = (Path(_episodes_env) if Path(_episodes_env).is_absolute()
                else _PROJECT_ROOT / (_episodes_env or "episodes"))
_sources_env = os.environ.get("SOURCES_FILE", "")
SOURCES_FILE = (Path(_sources_env) if Path(_sources_env).is_absolute()
                else _PROJECT_ROOT / (_sources_env or "sources.json"))
VOCAB_FILE = (Path(os.environ.get("VOCAB_FILE", "")) if os.environ.get("VOCAB_FILE")
              else _PROJECT_ROOT / "vocab.json")
# Recent-episodes-per-source cache for the subscriptions page. Written by the
# Telegram bot's scheduled check via POST /api/subscriptions/recent; read on
# the /subscriptions page. Regenerated on each bot run, so ephemeral loss on a
# container rebuild is fine.
_recent_env = os.environ.get("RECENT_EPISODES_CACHE_FILE", "")
RECENT_EPISODES_CACHE_FILE = (Path(_recent_env) if Path(_recent_env).is_absolute()
                              else _PROJECT_ROOT / (_recent_env or "recent_episodes_cache.json"))

UPLOAD_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".aac", ".opus"}

# Slug pattern: YYYY-MM-DD or YYYY-MM-DD-N
_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")

# YouTube video id (matches lib/downloader.py). The DB Episode model does not
# store video_id, so it is re-derived from the stored URL at render time.
_YT_ID_RE = re.compile(r"(?:watch\?.*v=|youtu\.be/)([a-zA-Z0-9_-]{11})")

from flask_cors import CORS

from web.auth import (
    authenticate_user,
    clear_auth_cookie,
    get_current_user,
    login_required,
    logout_token,
    register_user,
    set_auth_cookie,
)
from web.db import (
    Episode, PlaybackProgress, ProcessingArtifact, ProcessingJob,
    RecommendationDismissal, TranscriptionUsage,
    ReviewLog, VocabItem, VocabOccurrence, db_available, get_db, init_db,
)
from web.recommendations import load_catalog, normalize_url, rank_recommendations
from sqlalchemy import delete, func, or_, select, text as sa_text, update
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    static_folder="static",
    template_folder="templates",
)
# Trust X-Forwarded-Proto/Host from Railway's edge proxy so url_for generates
# https:// URLs and redirects work correctly behind TLS termination.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
# SECRET_KEY is required for signed cookies / auth tokens (Phase 2+).
# Falls back to an insecure default in local dev so the app still starts.
_SK_DEFAULT = "dev-insecure-change-me-before-deploy"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", _SK_DEFAULT)

# Connect to Postgres and ensure all tables exist.
# Silently skips if DATABASE_URL is not set (local dev without Postgres).
init_db()

# Bug #1 fix: when DATABASE_URL is set (i.e. we are in production or any
# environment that expects a DB), refuse to start if the connection failed.
# This prevents the app from silently booting into a fully unauthenticated,
# shared-filesystem mode due to a mis-typed env var or a transient DB outage.
# Set REQUIRE_DB=0 explicitly to suppress this guard (e.g. during local dev
# when DATABASE_URL is intentionally absent — the guard never fires then
# anyway because init_db() would have already skipped cleanly).
_DATABASE_URL_SET = bool(os.environ.get("DATABASE_URL", ""))
if _DATABASE_URL_SET and not db_available():
    raise RuntimeError(
        "DATABASE_URL is set but the database connection failed. "
        "Fix the connection or unset DATABASE_URL for local dev."
    )

# Guard: refuse to serve auth-protected data with a known-public key.
if db_available() and app.config["SECRET_KEY"] == _SK_DEFAULT:
    raise RuntimeError(
        "SECRET_KEY env var is not set or uses the insecure default. "
        "Set a strong random value before running in production."
    )

# Bug #5 fix: reconcile stale 'started' usage rows left by a previous worker
# crash / restart.  Any row older than 30 minutes that is still 'started' will
# never be completed — flip them to 'failed' so they don't consume quota.
if db_available():
    try:
        from datetime import timedelta
        from sqlalchemy import update as _sa_update
        _stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        with get_db() as _db:
            _db.execute(
                _sa_update(TranscriptionUsage)
                .where(
                    TranscriptionUsage.status == "started",
                    TranscriptionUsage.created_at < _stale_cutoff,
                )
                .values(status="failed")
            )
        log.info("Reconciled stale 'started' transcription usage rows on startup")
    except Exception as _e:
        log.warning("Could not reconcile stale usage rows: %s", _e)


@app.context_processor
def _inject_auth():
    """Make current_user and quota info available in every Jinja template."""
    user = get_current_user()
    unlimited = bool(user and _is_unlimited(user))
    return {
        "current_user":          user,
        "current_user_unlimited": unlimited,
        "db_available":          db_available(),
    }


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if get_current_user():
            return redirect(url_for("index"))
        return render_template("login.html", error=None)

    email    = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    result   = authenticate_user(email, password) if db_available() else None

    if result is None:
        return render_template("login.html", error="Invalid email or password.")

    _, token = result
    resp = make_response(redirect(url_for("index")))
    set_auth_cookie(resp, token)
    return resp


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "GET":
        if get_current_user():
            return redirect(url_for("index"))
        return render_template("register.html", error=None)

    email    = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if password != confirm:
        return render_template("register.html", error="Passwords do not match.")

    if not db_available():
        return render_template("register.html", error="Database not configured.")

    try:
        _, token = register_user(email, password, allowed_emails=_get_registration_whitelist())
    except ValueError as exc:
        return render_template("register.html", error=str(exc))

    resp = make_response(redirect(url_for("index")))
    set_auth_cookie(resp, token)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get("session_token")
    logout_token(token)
    resp = make_response(redirect(url_for("login_page")))
    clear_auth_cookie(resp)
    return resp


# ── Auth API (for Vite SPA / Capacitor iOS) ───────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    if not db_available():
        return jsonify({"error": "Database not configured"}), 503
    data     = request.get_json(force=True) or {}
    email    = data.get("email", "")
    password = data.get("password", "")
    confirm  = data.get("confirm", password)
    if password != confirm:
        return jsonify({"error": "Passwords do not match"}), 400
    try:
        user, token = register_user(email, password, allowed_emails=_get_registration_whitelist())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"token": token, "user": {
        "id": str(user.id), "email": user.email,
        "is_admin": user.is_admin, "is_unlimited": _is_unlimited(user),
    }})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    if not db_available():
        return jsonify({"error": "Database not configured"}), 503
    data     = request.get_json(force=True) or {}
    email    = data.get("email", "")
    password = data.get("password", "")
    result   = authenticate_user(email, password)
    if result is None:
        return jsonify({"error": "Invalid email or password"}), 401
    user, token = result
    return jsonify({"token": token, "user": {
        "id": str(user.id), "email": user.email,
        "is_admin": user.is_admin, "is_unlimited": _is_unlimited(user),
    }})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    token = _extract_bearer()
    logout_token(token)
    return jsonify({"ok": True})


@app.route("/api/auth/me")
@login_required
def api_auth_me():
    user = get_current_user()
    unlimited = _is_unlimited(user)
    return jsonify({
        "id":                  str(user.id),
        "email":               user.email,
        "is_admin":            user.is_admin,
        "is_unlimited":        unlimited,
        "transcription_limit": -1 if unlimited else user.transcription_limit,
    })


@app.route("/api/quota")
@login_required
def api_quota():
    """Return the current user's transcription quota."""
    user = get_current_user()
    if not db_available() or not user:
        return jsonify({"unlimited": True, "used": 0, "limit": -1, "allowed": True})
    allowed, used, limit = _check_quota(user)
    return jsonify({
        "unlimited": limit == -1,
        "used":      used,
        "limit":     limit,
        "allowed":   allowed,
    })


def _extract_bearer() -> str | None:
    """Pull bearer token from Authorization header (used by SPA/iOS logout)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.cookies.get("session_token")


@app.before_request
def _enforce_https():
    # Only redirect when the request arrived over plain HTTP through the proxy.
    # X-Forwarded-Proto is set by Railway; absent locally so this is a no-op in dev.
    if request.headers.get("X-Forwarded-Proto") == "http":
        return redirect(request.url.replace("http://", "https://", 1), code=301)

# ── Background job tracking ───────────────────────────────────────────────────

# job_id → {status, slug, step, step_num, total_steps, error, started_at}
# status: "processing" | "done" | "error"
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 50  # prune old jobs when exceeding this count

_vocab_lock = threading.Lock()
_sources_lock = threading.Lock()
_recent_lock = threading.Lock()

# Per-user explain call counter.
# Structure: {user_id_str: {"day": "YYYY-MM-DD", "counts": {episode_slug: int}}}
# Resets daily per-user.  When no episode slug is sent (direct API call) we
# use the sentinel "_global_" so the limit still applies (Bug #5 fix).
_EXPLAIN_LIMIT = 5
_explain_counts: dict[str, dict] = {}
_explain_lock = threading.Lock()
_EXPLAIN_MAX_USERS = 500   # cap dict size to avoid unbounded growth

# ── R2 helpers ────────────────────────────────────────────────────────────────

_r2_client      = None
_r2_client_lock = threading.Lock()

# Files uploaded to R2 per episode (same list as the migration script).
_EPISODE_UPLOAD_FILES = [
    "meta.json", "transcript.json", "analysis.json", "highlights.json",
    "subtitles.vtt", "cards.csv",
    "audio.mp3", "audio.m4a", "audio.wav", "audio.ogg",
    "audio.webm", "audio.flac", "audio.aac", "audio.opus",
]


def _get_r2():
    """Return a cached boto3 R2 client, or None when R2 is not configured."""
    global _r2_client
    if _r2_client is not None:
        return _r2_client
    with _r2_client_lock:
        if _r2_client is not None:
            return _r2_client
        endpoint = os.environ.get("R2_ENDPOINT_URL", "")
        key      = os.environ.get("R2_ACCESS_KEY_ID", "")
        secret   = os.environ.get("R2_SECRET_ACCESS_KEY", "")
        bucket   = os.environ.get("R2_BUCKET", "")
        if not all([endpoint, key, secret, bucket]):
            return None
        import boto3
        _r2_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            region_name="auto",
        )
    return _r2_client


def _r2_bucket() -> str:
    return os.environ.get("R2_BUCKET", "")


def _r2_presigned(key: str, expires: int = 3600) -> str:
    """Generate a presigned GET URL for an R2 object."""
    return _get_r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": _r2_bucket(), "Key": key},
        ExpiresIn=expires,
    )


def _r2_get_json(key: str) -> dict:
    """Fetch and JSON-parse an object from R2."""
    obj = _get_r2().get_object(Bucket=_r2_bucket(), Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _r2_key_exists(key: str) -> bool:
    """Check if an object key exists in R2.

    Returns False only for a definitive 404 / NoSuchKey response.
    Re-raises for all other errors (transient 5xx, auth failures, timeouts)
    so callers can decide how to handle them rather than silently treating
    a transient error as "file does not exist".
    """
    try:
        _get_r2().head_object(Bucket=_r2_bucket(), Key=key)
        return True
    except Exception as exc:
        from botocore.exceptions import ClientError
        if isinstance(exc, ClientError) and exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def _r2_find_audio(r2_prefix: str) -> "tuple[str, str] | None":
    """Locate the audio file under r2_prefix.

    Tries known extensions via HEAD (most-common-first) instead of list_objects_v2.
    HEAD on a specific key is faster than a prefix listing.
    Returns (key, mimetype) or None.
    """
    s3     = _get_r2()
    bucket = _r2_bucket()
    for ext in (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".webm", ".flac", ".opus", ".mp4"):
        key = f"{r2_prefix}audio{ext}"
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return key, mimetypes.guess_type(key)[0] or "audio/mpeg"
        except Exception:
            continue
    return None


def _r2_upload_episode(ep_dir: Path, r2_prefix: str) -> None:
    """Upload all known episode files from ep_dir to R2 at r2_prefix.

    Bug #3 fix: raises RuntimeError if the audio file (the only truly required
    file) fails to upload.  Other files log a warning and continue.
    """
    mimetypes.add_type("text/vtt", ".vtt")
    mimetypes.add_type("text/csv", ".csv")
    s3     = _get_r2()
    bucket = _r2_bucket()
    for filename in _EPISODE_UPLOAD_FILES:
        fpath = ep_dir / filename
        if not fpath.exists():
            continue
        key = f"{r2_prefix}{filename}"
        mt  = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        is_audio = filename.startswith("audio.")
        try:
            s3.upload_file(str(fpath), bucket, key, ExtraArgs={"ContentType": mt})
            log.info(f"R2 uploaded: {key}")
        except Exception as exc:
            if is_audio:
                raise RuntimeError(
                    f"Failed to upload audio to R2 ({key}): {exc}"
                ) from exc
            log.warning(f"R2 upload failed (non-critical) for {key}: {exc}")


# ── Transcription quota helpers ───────────────────────────────────────────────

# Files larger than this trigger Whisper chunking; we disallow them for non-unlimited users.
_TRANSCRIPTION_MAX_BYTES = 23 * 1024 * 1024   # 23 MB


def _get_whitelist() -> set:
    """Return lowercase email set from TRANSCRIPTION_WHITELIST env var.
    Used only for unlimited-quota grants — NOT for gating registration.
    """
    raw = os.environ.get("TRANSCRIPTION_WHITELIST", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _get_registration_whitelist() -> "set | None":
    """Return lowercase email set from REGISTRATION_WHITELIST env var.
    When set, only these addresses may register (admin/bootstrap always exempt).
    When unset or empty, registration is open to everyone.
    """
    raw = os.environ.get("REGISTRATION_WHITELIST", "")
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return emails if emails else None


def _is_unlimited(user) -> bool:
    """Admins, whitelisted emails, and users with limit=-1 have no transcription cap."""
    if user.is_admin:
        return True
    if user.transcription_limit == -1:
        return True
    return user.email.lower() in _get_whitelist()


def _check_quota(user) -> "tuple[bool, int, int]":
    """
    Returns (allowed, used, limit).
    limit = -1 means unlimited.
    'used' counts rows with status 'started' or 'completed'.
    """
    with get_db() as db:
        used = db.execute(
            select(func.count()).select_from(TranscriptionUsage).where(
                TranscriptionUsage.user_id == user.id,
                TranscriptionUsage.status.in_(["started", "completed"]),
            )
        ).scalar() or 0

    if _is_unlimited(user):
        return True, used, -1

    limit = user.transcription_limit
    return used < limit, used, limit


def _atomic_quota_insert(user, audio_bytes: int = 0) -> "TranscriptionUsage | None":
    """
    Bug #2 fix: atomically check quota and insert a 'started' usage row.

    Uses a Postgres per-user advisory lock (pg_advisory_xact_lock) so that
    concurrent requests for the same user serialize at the DB level — the
    READ COMMITTED default isolation alone would let two concurrent SELECT
    COUNT(*) calls both see the same count before either INSERT commits.

    Returns the new TranscriptionUsage row (with .id populated) if allowed,
    or raises ValueError with a user-facing message when the cap is hit.
    Skips the lock entirely for unlimited users (admin / whitelisted).
    """
    with get_db() as db:
        if not _is_unlimited(user):
            # Serialize all concurrent quota checks for this user.
            # pg_advisory_xact_lock is released automatically when the
            # transaction commits or rolls back.
            # Derive the lock key from the UUID's own integer value — NOT
            # Python's hash(), which is salted per-process (PYTHONHASHSEED) and
            # so produces a DIFFERENT key in each gunicorn worker, silently
            # defeating cross-process serialization the moment --workers > 1.
            # pg advisory-lock keys are signed int64, so fold the 128-bit UUID
            # into the positive int64 range with a mask.
            uid_hash = user.id.int & 0x7FFFFFFFFFFFFFFF
            db.execute(sa_text("SELECT pg_advisory_xact_lock(:h)"), {"h": uid_hash})

            # Re-read the count inside the lock — now safe against concurrent writers.
            used = db.execute(
                select(func.count()).select_from(TranscriptionUsage).where(
                    TranscriptionUsage.user_id == user.id,
                    TranscriptionUsage.status.in_(["started", "completed"]),
                )
            ).scalar() or 0
            limit = user.transcription_limit
            if used >= limit:
                raise ValueError(
                    f"Transcription limit reached ({used}/{limit}). "
                    "Contact the admin if you need more."
                )

        usage = TranscriptionUsage(
            user_id     = user.id,
            audio_bytes = audio_bytes,
            status      = "started",
        )
        db.add(usage)
        db.flush()   # populate usage.id before the session closes
        db.expunge(usage)

    return usage


def _lookup_episode(slug: str) -> "Episode | None":
    """
    Look up an Episode row owned by the current user.
    - Returns the Episode row if found.
    - aborts(404) if the DB is available but the episode is not found for this user.
    - Returns None when the DB is not available (caller falls back to local filesystem).
    """
    if not db_available():
        return None
    user = get_current_user()
    if user is None:
        abort(401)
    if not _SLUG_RE.match(slug):
        abort(400)
    with get_db() as db:
        row = db.execute(
            select(Episode).where(
                Episode.owner_user_id == user.id,
                Episode.slug == slug,
            )
        ).scalar_one_or_none()
    if row is None:
        abort(404)
    return row


def _set_step(job_id: str, step: str, step_num: int = 0) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["step"] = step
            if step_num:
                _jobs[job_id]["step_num"] = step_num
    if db_available():
        try:
            values = {"current_step": step, "updated_at": datetime.now(timezone.utc)}
            if step_num:
                values["step_num"] = step_num
            with get_db() as db:
                db.execute(update(ProcessingJob).where(ProcessingJob.id == uuid.UUID(job_id)).values(**values))
        except Exception as exc:
            log.warning("Could not persist step for job %s: %s", job_id, exc)


def _persist_processing_job(job_id: str, slug: str, user_id, source_url: str,
                            level: str, total_steps: int, meta: dict,
                            usage_id=None, unlimited=False, clone_from_id=None) -> None:
    if not (db_available() and user_id):
        return
    source_token = _get_source_token(source_url)
    artifact_prefix = f"jobs/{user_id}/{job_id}/" if _get_r2() else ""
    with get_db() as db:
        db.add(ProcessingJob(
            id=uuid.UUID(job_id),
            user_id=user_id,
            usage_id=uuid.UUID(str(usage_id)) if usage_id else None,
            slug=slug,
            source_url=source_url or "",
            source_token=source_token,
            level=level,
            status="queued",
            current_step="Starting…",
            total_steps=total_steps,
            unlimited=bool(unlimited),
            clone_from_id=uuid.UUID(str(clone_from_id)) if clone_from_id else None,
            meta_json=meta or {},
            artifact_prefix=artifact_prefix,
        ))


def _claim_processing_job(job_id: str) -> bool:
    if not db_available():
        return True
    try:
        now = datetime.now(timezone.utc)
        with get_db() as db:
            result = db.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == uuid.UUID(job_id),
                    ProcessingJob.status.in_(["queued", "retrying"]),
                )
                .values(
                    status="running",
                    started_at=now,
                    updated_at=now,
                    attempt_count=ProcessingJob.attempt_count + 1,
                    error_code="",
                    error_message="",
                )
            )
            return result.rowcount == 1
    except Exception as exc:
        log.warning("Could not claim durable job %s: %s", job_id, exc)
        return False


def _finish_processing_job(job_id: str, *, success: bool, episode_id=None,
                           error_code="", error_message="", retry_from="download") -> None:
    now = datetime.now(timezone.utc)
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "done" if success else "error"
            _jobs[job_id]["step"] = "Complete" if success else "Failed"
            _jobs[job_id]["error"] = error_message
    if not db_available():
        return
    try:
        values = {
            "status": "completed" if success else "failed",
            "current_step": "Complete" if success else "Failed",
            "updated_at": now,
            "finished_at": now,
            "error_code": error_code,
            "error_message": error_message[:4000],
            "retry_from": retry_from,
            "artifacts_expire_at": None if success else now + timedelta(days=7),
        }
        if episode_id is not None:
            values["episode_id"] = episode_id
        with get_db() as db:
            db.execute(update(ProcessingJob).where(ProcessingJob.id == uuid.UUID(job_id)).values(**values))
    except Exception as exc:
        log.warning("Could not finish durable job %s: %s", job_id, exc)


def _job_error_code(exc: Exception, step_num: int) -> str:
    text = str(exc).lower()
    if "429" in text or "rate limit" in text:
        return "provider_rate_limit"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    if step_num <= 1:
        return "download_failed"
    if step_num == 2:
        return "transcription_failed"
    if step_num == 3:
        return "translation_failed"
    if step_num == 4:
        return "analysis_failed"
    return "storage_failed"


def _job_to_dict(row: ProcessingJob) -> dict:
    status_map = {"queued": "processing", "running": "processing", "retrying": "processing",
                  "completed": "done", "failed": "error"}
    return {
        "job_id": str(row.id),
        "slug": row.slug,
        "status": status_map.get(row.status, row.status),
        "step": row.current_step,
        "step_num": row.step_num,
        "total_steps": row.total_steps,
        "error": row.error_message,
        "error_code": row.error_code,
        "retry_from": row.retry_from,
        "attempt_count": row.attempt_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "title": (row.meta_json or {}).get("title") or row.slug,
        "can_retry": row.status == "failed" and row.attempt_count < 3,
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checkpoint_artifact(job_id: str, stage: str, path: Path) -> None:
    """Upload and validate one retry checkpoint, then record its manifest row."""
    if not (db_available() and _get_r2() and path.exists()):
        return
    with get_db() as db:
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        if not job or not job.artifact_prefix:
            return
        object_key = f"{job.artifact_prefix}{path.name}"
    size = path.stat().st_size
    checksum = _sha256_path(path)
    _get_r2().upload_file(
        str(path), _r2_bucket(), object_key,
        ExtraArgs={"ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream"},
    )
    head = _get_r2().head_object(Bucket=_r2_bucket(), Key=object_key)
    if int(head.get("ContentLength", -1)) != size:
        raise RuntimeError(f"Checkpoint validation failed for {stage}: uploaded size mismatch")
    with get_db() as db:
        artifact = db.execute(select(ProcessingArtifact).where(
            ProcessingArtifact.job_id == uuid.UUID(job_id),
            ProcessingArtifact.stage == stage,
        )).scalar_one_or_none()
        values = {
            "object_key": object_key,
            "filename": path.name,
            "size_bytes": size,
            "sha256": checksum,
            "validated": True,
        }
        if artifact:
            for key, value in values.items():
                setattr(artifact, key, value)
        else:
            db.add(ProcessingArtifact(job_id=uuid.UUID(job_id), stage=stage, **values))


def _checkpoint_json(job_id: str, stage: str, path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _checkpoint_artifact(job_id, stage, path)


def _try_checkpoint_artifact(job_id: str, stage: str, path: Path, payload: dict | None = None) -> bool:
    try:
        if payload is None:
            _checkpoint_artifact(job_id, stage, path)
        else:
            _checkpoint_json(job_id, stage, path, payload)
        return True
    except Exception as exc:
        log.warning("Checkpoint %s failed for job %s; pipeline will continue: %s", stage, job_id, exc)
        return False


def _restore_job_artifacts(job_id: str, ep_dir: Path) -> dict[str, Path]:
    """Download validated checkpoints and verify hashes before any stage reuse."""
    restored: dict[str, Path] = {}
    if not (db_available() and _get_r2()):
        return restored
    with get_db() as db:
        rows = db.execute(
            select(ProcessingArtifact)
            .where(
                ProcessingArtifact.job_id == uuid.UUID(job_id),
                ProcessingArtifact.validated.is_(True),
            )
            .order_by(ProcessingArtifact.created_at)
        ).scalars().all()
    for artifact in rows:
        target = ep_dir / artifact.filename
        try:
            _get_r2().download_file(_r2_bucket(), artifact.object_key, str(target))
            if target.stat().st_size != artifact.size_bytes or _sha256_path(target) != artifact.sha256:
                target.unlink(missing_ok=True)
                log.warning("Ignoring invalid checkpoint %s for job %s", artifact.stage, job_id)
                continue
            if target.suffix == ".json":
                json.loads(target.read_text(encoding="utf-8"))
            restored[artifact.stage] = target
        except Exception as exc:
            target.unlink(missing_ok=True)
            log.warning("Could not restore checkpoint %s for job %s: %s", artifact.stage, job_id, exc)
    return restored


def _delete_job_artifacts(job_id: str) -> None:
    """Remove promoted temporary objects after successful episode persistence."""
    if not (db_available() and _get_r2()):
        return
    with get_db() as db:
        job = db.get(ProcessingJob, uuid.UUID(job_id))
        prefix = job.artifact_prefix if job else ""
    if prefix:
        paginator = _get_r2().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_r2_bucket(), Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                _get_r2().delete_objects(Bucket=_r2_bucket(), Delete={"Objects": objects})
    with get_db() as db:
        db.execute(delete(ProcessingArtifact).where(
            ProcessingArtifact.job_id == uuid.UUID(job_id)
        ))


def _prune_old_jobs() -> None:
    """Remove oldest completed/failed jobs when the pool exceeds _MAX_JOBS."""
    with _jobs_lock:
        if len(_jobs) <= _MAX_JOBS:
            return
        finished = [
            (jid, j) for jid, j in _jobs.items()
            if j["status"] in ("done", "error")
        ]
        finished.sort(key=lambda x: x[1].get("started_at", 0))
        to_remove = len(_jobs) - _MAX_JOBS
        for jid, _ in finished[:to_remove]:
            del _jobs[jid]


def _pipeline_thread(
    job_id: str,
    slug: str,
    ep_dir: Path,
    source_url: str | None,
    audio_path: Path | None,
    meta: dict,
    level: str,
    user_id=None,
    usage_id=None,
    unlimited=False,
    clone_from_id=None,
) -> None:
    """Runs the full pipeline in a background thread."""
    from lib.transcriber import transcribe_audio
    from lib.translator import translate_segments
    from lib.analyzer import analyze_transcript
    from lib.writer import write_episode_files, _write_cards
    from lib.tokenizer import tokenize_segments

    if not _claim_processing_job(job_id):
        log.warning("Job %s was not claimed; another worker may own it", job_id)
        return
    restored_artifacts = _restore_job_artifacts(job_id, ep_dir)

    if clone_from_id and db_available() and _get_r2():
        try:
            with get_db() as db:
                cloned_ep = db.get(Episode, uuid.UUID(clone_from_id))
                if not cloned_ep:
                    raise RuntimeError("Cloned episode not found in database")
                cloned_prefix = cloned_ep.r2_prefix
                cloned_level  = cloned_ep.level
                meta_data = {
                    "title":    cloned_ep.title,
                    "channel":  cloned_ep.channel,
                    "url":      cloned_ep.url,
                    "thumbnail": cloned_ep.thumbnail,
                    "duration": cloned_ep.duration,
                    "source":   cloned_ep.source,
                }
                source_token = cloned_ep.source_token

            if cloned_level != level:
                # Levels differ — re-run analysis only, skip download and transcription.
                with _jobs_lock:
                    _jobs[job_id]["total_steps"] = 3
                _set_step(job_id, "Fetching original transcript…", 1)
                segments = _r2_get_json(f"{cloned_prefix}transcript.json").get("segments", [])
                _set_step(job_id, f"Analysing vocabulary and grammar for {level} level…", 2)
                analysis = analyze_transcript(segments, level=level)
                _set_step(job_id, "Saving analysis to cloud storage…", 3)
                (ep_dir / f"analysis_{level}.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
                (ep_dir / f"highlights_{level}.json").write_text(json.dumps({"highlights": analysis.get("highlights", [])}, ensure_ascii=False, indent=2), encoding="utf-8")
                _write_cards(ep_dir / f"cards_{level}.csv", analysis)
                s3 = _get_r2()
                bucket = _r2_bucket()
                for fn in [f"analysis_{level}.json", f"highlights_{level}.json", f"cards_{level}.csv"]:
                    fpath = ep_dir / fn
                    key = f"{cloned_prefix}{fn}"
                    mt = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
                    s3.upload_file(str(fpath), bucket, key, ExtraArgs={"ContentType": mt})
                done_step = 3
            else:
                done_step = 1

            with get_db() as db:
                db.add(Episode(
                    owner_user_id = user_id,
                    slug          = slug,
                    date          = slug[:10],
                    title         = meta_data["title"],
                    channel       = meta_data["channel"],
                    url           = meta_data["url"],
                    thumbnail     = meta_data["thumbnail"],
                    duration      = meta_data["duration"],
                    level         = level,
                    source        = meta_data["source"],
                    source_token  = source_token,
                    r2_prefix     = cloned_prefix,
                ))
            shutil.rmtree(ep_dir, ignore_errors=True)
            with _jobs_lock:
                _jobs[job_id]["step_num"]    = done_step
                _jobs[job_id]["total_steps"] = done_step
            _finish_processing_job(job_id, success=True)
            return

        except Exception as exc:
            tb = traceback.format_exc()
            log.error(f"Job {job_id} fast-path failed:\n{tb}")
            shutil.rmtree(ep_dir, ignore_errors=True)
            _finish_processing_job(
                job_id, success=False, error_code="clone_failed",
                error_message=str(exc), retry_from="analysis",
            )
            return

    total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    def _mark_usage(status: str, episode_id=None) -> None:
        if usage_id and db_available():
            try:
                with get_db() as db:
                    row = db.get(TranscriptionUsage, usage_id)
                    if row:
                        row.status = status
                        if episode_id is not None:
                            row.episode_id = episode_id
                        if audio_path and audio_path.exists():
                            row.audio_bytes = audio_path.stat().st_size
            except Exception as e:
                log.warning(f"Could not update TranscriptionUsage {usage_id}: {e}")

    try:
        # ── Step 1: Download ────────────────────────────────────────────────
        if "audio" in restored_artifacts:
            audio_path = restored_artifacts["audio"]
            _set_step(job_id, "Reusing validated audio checkpoint…", 1)
        elif source_url and audio_path is None:
            from lib.downloader import download_latest
            _set_step(job_id, "Downloading audio…", 1)
            _ovr_title   = (meta or {}).get("title", "")
            _ovr_channel = (meta or {}).get("channel", "")
            _ovr_duration = (meta or {}).get("duration", 0)
            audio_path, meta = download_latest([source_url], ep_dir)
            if not audio_path:
                raise RuntimeError("Could not download audio — check the URL")
            # Prefer caller-supplied metadata (e.g. RSS title/channel from the
            # bot) over file-derived values — podcast enclosures frequently lack
            # ID3 tags, so yt-dlp falls back to the raw file id.
            if _ovr_title and _ovr_title != source_url:
                meta["title"] = _ovr_title
            if _ovr_channel:
                meta["channel"] = _ovr_channel
            if _ovr_duration:
                meta["duration"] = _ovr_duration

            # Byte size check (catches files whose bitrate makes them cheap to
            # download but expensive for the chunked-transcription path).
            if not unlimited and audio_path.stat().st_size > _TRANSCRIPTION_MAX_BYTES:
                size_mb = audio_path.stat().st_size // (1024 * 1024)
                raise RuntimeError(
                    f"Audio is {size_mb} MB — only files under 23 MB are supported. "
                    "Contact the admin if you need longer content."
                )

        # Duration cap — runs for both URL downloads and file uploads.
        # Byte size alone doesn't bound Whisper cost (low-bitrate = long audio).
        from lib.transcriber import check_audio_duration
        check_audio_duration(audio_path, unlimited=unlimited)
        if "audio" not in restored_artifacts:
            _try_checkpoint_artifact(job_id, "audio", audio_path)

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        if "transcription" in restored_artifacts:
            _set_step(job_id, "Reusing validated transcript checkpoint…", 2)
            whisper_result = json.loads(restored_artifacts["transcription"].read_text(encoding="utf-8"))
        else:
            _set_step(job_id, "Transcribing with Whisper…", 2)
            whisper_result = transcribe_audio(audio_path)
            _try_checkpoint_artifact(job_id, "transcription", ep_dir / "checkpoint_transcription.json", whisper_result)

        # ── Step 3: Translate ────────────────────────────────────────────────
        if "translation" in restored_artifacts:
            _set_step(job_id, "Reusing validated translation checkpoint…", 3)
            segments = json.loads(restored_artifacts["translation"].read_text(encoding="utf-8"))["segments"]
        else:
            _set_step(job_id, "Translating EN + ZH with Gemini…", 3)
            segments = translate_segments(whisper_result["segments"])
            segments = tokenize_segments(segments)
            _try_checkpoint_artifact(job_id, "translation", ep_dir / "checkpoint_translation.json", {"segments": segments})

        # ── Step 4: Analyse ──────────────────────────────────────────────────
        if "analysis" in restored_artifacts:
            _set_step(job_id, "Reusing validated analysis checkpoint…", 4)
            analysis = json.loads(restored_artifacts["analysis"].read_text(encoding="utf-8"))
        else:
            _set_step(job_id, "Analysing vocabulary and grammar…", 4)
            analysis = analyze_transcript(segments, level=level)
            _try_checkpoint_artifact(job_id, "analysis", ep_dir / "checkpoint_analysis.json", analysis)

        # ── Step 5: Write files ──────────────────────────────────────────────
        _set_step(job_id, "Writing episode files…", 5)
        write_episode_files(ep_dir, meta, segments, analysis, whisper_result)
        (ep_dir / f"analysis_{level}.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        (ep_dir / f"highlights_{level}.json").write_text(json.dumps({"highlights": analysis.get("highlights", [])}, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_cards(ep_dir / f"cards_{level}.csv", analysis)

        # ── Step 6: Upload to R2 + persist Episode row ───────────────────────
        # Always persist the Episode row when the DB is available, regardless
        # of whether R2 is configured.  r2_prefix is left empty when R2 is not
        # configured; asset routes fall back to local disk in that case.
        r2_prefix = ""
        episode_row_id = None
        if user_id and db_available():
            if _get_r2():
                _set_step(job_id, "Saving to cloud storage…", 6)
                r2_prefix = f"episodes/{user_id}/{slug}/"
                _r2_upload_episode(ep_dir, r2_prefix)
                # Level-specific files are not in _EPISODE_UPLOAD_FILES — upload separately.
                s3 = _get_r2()
                bucket = _r2_bucket()
                for fn in [f"analysis_{level}.json", f"highlights_{level}.json", f"cards_{level}.csv"]:
                    fpath = ep_dir / fn
                    key = f"{r2_prefix}{fn}"
                    mt = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
                    s3.upload_file(str(fpath), bucket, key, ExtraArgs={"ContentType": mt})

            # re-read meta (download may have enriched it)
            meta_path = ep_dir / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))

            with get_db() as db:
                existing = db.execute(
                    select(Episode).where(
                        Episode.owner_user_id == user_id,
                        Episode.slug == slug,
                    )
                ).scalar_one_or_none()
                if not existing:
                    ep_row = Episode(
                        owner_user_id = user_id,
                        slug          = slug,
                        date          = slug[:10],
                        title         = meta.get("title", slug),
                        channel       = meta.get("channel", ""),
                        url           = meta.get("url", ""),
                        thumbnail     = meta.get("thumbnail", ""),
                        duration      = meta.get("duration", 0),
                        level         = meta.get("level", level),
                        source        = meta.get("source", ""),
                        source_token  = _get_source_token(source_url or meta.get("url", "")),
                        r2_prefix     = r2_prefix,
                    )
                    db.add(ep_row)
                    db.flush()   # populate ep_row.id for the usage-row link below
                    episode_row_id = ep_row.id
                else:
                    # Skeleton row exists — update with enriched pipeline data
                    existing.title        = meta.get("title", existing.title)
                    existing.channel      = meta.get("channel", existing.channel)
                    existing.url          = meta.get("url", existing.url)
                    existing.thumbnail    = meta.get("thumbnail", existing.thumbnail)
                    existing.duration     = meta.get("duration", existing.duration)
                    existing.level        = meta.get("level", level)
                    existing.source       = meta.get("source", existing.source)
                    existing.source_token = _get_source_token(source_url or meta.get("url", ""))
                    if r2_prefix:
                        existing.r2_prefix = r2_prefix
                    episode_row_id = existing.id

        # Link the usage row to the Episode so the admin history join resolves
        # (episode_id was previously never set, leaving the FK permanently null).
        _mark_usage("completed", episode_id=episode_row_id)

        # Bug #4 fix: after a successful R2 upload the local ep_dir is no longer
        # needed (canonical copy is in R2).  Remove it to prevent unbounded disk
        # growth on long-lived workers.  When R2 is not configured we keep the
        # local files (they are the only copy).
        if r2_prefix:
            shutil.rmtree(ep_dir, ignore_errors=True)
            log.info(f"Cleaned up local ep_dir after R2 upload: {ep_dir}")

        with _jobs_lock:
            _jobs[job_id]["step_num"] = total_steps
        _finish_processing_job(job_id, success=True, episode_id=episode_row_id)
        try:
            _delete_job_artifacts(job_id)
        except Exception as exc:
            log.warning("Could not clean promoted checkpoints for job %s: %s", job_id, exc)

    except Exception as exc:
        tb = traceback.format_exc()
        log.error(f"Job {job_id} failed:\n{tb}")
        _mark_usage("failed")
        # Bug #4 fix (failure path): always clean up ep_dir on error — a
        # partially-written directory is worse than nothing, and it prevents
        # disk fill-up from accumulated failed jobs.
        shutil.rmtree(ep_dir, ignore_errors=True)
        # Remove the skeleton Episode row inserted at submission time so the user
        # can re-submit without hitting the dedup redirect.  Only delete rows that
        # never got an r2_prefix (i.e. incomplete skeleton rows, not finished ones).
        if user_id and db_available():
            try:
                with get_db() as db:
                    skel = db.execute(
                        select(Episode).where(
                            Episode.owner_user_id == user_id,
                            Episode.slug          == slug,
                            Episode.r2_prefix     == "",
                        )
                    ).scalar_one_or_none()
                    if skel:
                        db.delete(skel)
            except Exception as _del_exc:
                log.warning("Could not clean up skeleton Episode row for %s: %s", slug, _del_exc)
        with _jobs_lock:
            failed_step = int(_jobs.get(job_id, {}).get("step_num", 0) or 0)
        retry_stage = {1: "download", 2: "transcription", 3: "translation", 4: "analysis", 5: "write", 6: "storage"}.get(failed_step, "download")
        _finish_processing_job(
            job_id,
            success=False,
            error_code=_job_error_code(exc, failed_step),
            error_message=str(exc),
            retry_from=retry_stage,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ep_dir(date_str: str) -> Path:
    if not _SLUG_RE.match(date_str):
        abort(400)
    ep = EPISODES_DIR / date_str
    # Prevent path traversal
    if not ep.resolve().is_relative_to(EPISODES_DIR.resolve()):
        abort(400)
    if not ep.is_dir():
        abort(404)
    return ep


def _read_json(path: Path) -> dict:
    if not path.exists():
        abort(404)
    return json.loads(path.read_text(encoding="utf-8"))


def _find_audio(ep_dir: Path) -> Path | None:
    """Return first audio.* file found in the episode directory."""
    for ext in (".mp3", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".aac", ".opus", ".mp4"):
        p = ep_dir / f"audio{ext}"
        if p.exists():
            return p
    return None


def _get_source_token(url: str | None) -> str | None:
    """Generate a unique token for the YouTube video or RSS feed URL."""
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
        # Apple Podcasts page URLs identify the specific episode via the
        # "i" query param (the path is just the show id) — dropping it
        # collapsed every episode of a show onto the same token.
        query = ""
        if parsed.netloc.lower() == "podcasts.apple.com":
            episode_id = urllib.parse.parse_qs(parsed.query).get("i", [None])[0]
            if episode_id:
                query = urllib.parse.urlencode({"i": episode_id})
        normalized_url = urllib.parse.urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            "", query, ""
        ))
    except Exception:
        normalized_url = url.lower()

    sha_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"url:{sha_hash}"


def _unique_ep_slug(base: str, user_id=None) -> str:
    """Return a slug not already used for this user (DB) or on disk (fallback)."""
    slug    = base
    counter = 2
    if user_id and db_available():
        with get_db() as db:
            while (
                db.execute(select(Episode).where(
                    Episode.owner_user_id == user_id,
                    Episode.slug == slug,
                )).scalar_one_or_none() is not None
                or db.execute(select(ProcessingJob.id).where(
                    ProcessingJob.user_id == user_id,
                    ProcessingJob.slug == slug,
                ).limit(1)).scalar_one_or_none() is not None
            ):
                slug = f"{base}-{counter}"
                counter += 1
    else:
        while (EPISODES_DIR / slug / "transcript.json").exists():
            slug = f"{base}-{counter}"
            counter += 1
    return slug


def _find_source_clone(source_url: str, user_id) -> tuple[str | None, str | None, str | None]:
    """Check episode deduplication by source_token (R2 mode only).

    Returns one of:
    - (existing_slug, None, None)       — this user already owns the URL
    - (None, clone_id, clone_level)     — another user owns it; clone from them
    - (None, None, None)                — no duplicate found
    """
    source_token = _get_source_token(source_url)
    if not (source_token and db_available() and _get_r2()):
        return None, None, None
    with get_db() as db:
        own = db.execute(
            select(Episode).where(
                Episode.owner_user_id == user_id,
                Episode.source_token  == source_token,
                Episode.deleted_at.is_(None),
            )
        ).scalars().first()
        if own:
            return own.slug, None, None
        other = db.execute(
            select(Episode).where(
                Episode.source_token == source_token,
                Episode.r2_prefix    != "",
                Episode.deleted_at.is_(None),
            )
        ).scalars().first()
        if other:
            return None, str(other.id), other.level
    return None, None, None


def _make_response_cached(response):
    """Add cache headers to a response for per-user episode data.

    'private' restricts caching to the individual user's browser.
    'max-age=300' lets the browser serve from cache for 5 minutes without a
    round-trip, which matters most for large transcript/analysis JSON on Railway.
    'must-revalidate' means don't serve stale content if the cache entry expires
    and the server is unreachable.
    """
    response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
    return response


# ── Pages ─────────────────────────────────────────────────────────────────────


def _group_by_channel(episodes: list) -> list:
    """Group episode list by channel, sorted by most recent episode first."""
    channel_map: dict[str, list] = {}
    for ep in episodes:
        key = (ep["meta"].get("channel") or "").strip() or "Podcast"
        channel_map.setdefault(key, []).append(ep)
    return sorted(channel_map.items(), key=lambda kv: kv[1][0]["date"], reverse=True)


def _group_by_date(episodes: list) -> list:
    """Group episodes by month (YYYY-MM), newest month first."""
    month_map: dict[str, list] = {}
    for ep in episodes:
        ym = (ep.get("date") or "")[:7]  # slug starts with YYYY-MM-DD
        month_map.setdefault(ym, []).append(ep)

    def _label(ym: str) -> str:
        try:
            return datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
        except ValueError:
            return ym or "Undated"

    ordered = sorted(month_map.items(), key=lambda kv: kv[0], reverse=True)
    return [(_label(ym), eps) for ym, eps in ordered]


def _grouped(episodes: list):
    """Pick grouping from the ?group= query param. Returns (groups, mode)."""
    mode = request.args.get("group", "channel")
    if mode == "date":
        return _group_by_date(episodes), "date"
    return _group_by_channel(episodes), "channel"


def _episode_summary(row: Episode) -> dict:
    position = max(0.0, row.max_position or row.resume_position or 0.0)
    if row.duration > 0:
        position = min(position, float(row.duration))
        progress = 100 if row.completed_at else min(100, round(position / row.duration * 100))
    else:
        progress = 100 if row.completed_at else 0
    return {
        "date": row.slug,
        "meta": {
            "title": row.title, "channel": row.channel, "url": row.url,
            "thumbnail": row.thumbnail, "duration": row.duration,
            "level": row.level, "source": row.source,
        },
        "has_audio": bool(row.r2_prefix), "has_transcript": bool(row.r2_prefix),
        "position": position, "progress": progress,
        "completed": row.completed_at is not None,
        "resume_updated_at": row.resume_updated_at,
    }


@app.route("/")
@login_required
def index():
    user = get_current_user()
    resume_episode = None
    recent_episodes = []
    due_count = 0
    jobs = []
    inbox = []
    if db_available() and user:
        with get_db() as db:
            rows = db.execute(
                select(Episode).where(
                    Episode.owner_user_id == user.id,
                    Episode.deleted_at.is_(None),
                ).order_by(Episode.created_at.desc())
            ).scalars().all()
            summaries = [_episode_summary(row) for row in rows]
            resumable = [item for item in summaries if
                         not item["completed"] and item["position"] > 5 and
                         item["progress"] < 90 and item["resume_updated_at"] is not None]
            if resumable:
                resume_episode = max(resumable, key=lambda item: item["resume_updated_at"])
            recent_episodes = [item for item in summaries if item is not resume_episode][:3]
            due_count = db.execute(select(func.count()).select_from(VocabItem).where(
                VocabItem.user_id == user.id,
                VocabItem.suspended.is_(False),
                or_(VocabItem.due_at.is_(None), VocabItem.due_at <= datetime.now(timezone.utc)),
            )).scalar_one()
            job_rows = db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.user_id == user.id,
                    ProcessingJob.status.in_(["queued", "running", "retrying", "failed"]),
                ).order_by(ProcessingJob.updated_at.desc()).limit(3)
            ).scalars().all()
            jobs = [_job_to_dict(row) for row in job_rows]
        sources_data = _load_sources()
        _, subscription_inbox, _ = _build_subscription_view(
            sources_data.get("sources", []), _load_recent_cache(), user)
        inbox = [item for item in subscription_inbox if not item["processed"] and not item["active"]][:3]
    return render_template(
        "today.html", resume_episode=resume_episode, recent_episodes=recent_episodes,
        due_count=due_count, jobs=jobs, inbox=inbox,
    )


@app.route("/episodes")
@login_required
def library():
    episodes = []
    resume_episode = None

    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user:
            with get_db() as db:
                rows = db.execute(
                    select(Episode)
                    .where(
                        Episode.owner_user_id == user.id,
                        Episode.deleted_at.is_(None),
                    )
                    .order_by(Episode.date.desc(), Episode.created_at.desc())
                ).scalars().all()
            episodes = [_episode_summary(row) for row in rows]
            resumable = [
                ep for ep in episodes
                if not ep["completed"] and ep["position"] > 5 and ep["progress"] < 90
                and ep["resume_updated_at"] is not None
            ]
            if resumable:
                resume_episode = max(resumable, key=lambda ep: ep["resume_updated_at"])
        grouped_episodes = [ep for ep in episodes if ep is not resume_episode]
        groups, group_mode = _grouped(grouped_episodes)
        return render_template(
            "index.html",
            episodes=episodes,
            groups=groups,
            group_mode=group_mode,
            resume_episode=resume_episode,
        )

    # ── File fallback ─────────────────────────────────────────────────────────
    if EPISODES_DIR.exists():
        for ep in sorted(EPISODES_DIR.iterdir(), reverse=True):
            if not ep.is_dir() or not _SLUG_RE.match(ep.name):
                continue
            meta_file = ep / "meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            has_audio      = _find_audio(ep) is not None
            has_transcript = (ep / "transcript.json").exists()
            episodes.append({
                "date":           ep.name,
                "meta":           meta,
                "has_audio":      has_audio,
                "has_transcript": has_transcript,
            })
    groups, group_mode = _grouped(episodes)
    return render_template(
        "index.html", episodes=episodes, groups=groups,
        group_mode=group_mode, resume_episode=None,
    )


@app.route("/episode/<date_str>")
@login_required
def episode(date_str: str):
    # ── DB path ───────────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        _yt = _YT_ID_RE.search(ep_row.url or "")
        meta = {
            "title":     ep_row.title,
            "channel":   ep_row.channel,
            "url":       ep_row.url,
            "thumbnail": ep_row.thumbnail,
            "duration":  ep_row.duration,
            "level":     ep_row.level,
            "source":    ep_row.source,
            "video_id":  _yt.group(1) if _yt else "",
        }
        # Embed presigned R2 URLs in the page so JS can fetch directly in one
        # round trip instead of going Flask → 302 → R2.  Pure HMAC signing,
        # no network call.  JS falls back to /api/ routes if these are absent
        # or if the URL has expired (403).
        r2_urls = {}
        if ep_row.r2_prefix and _get_r2():
            level = ep_row.level or ""
            try:
                has_level_analysis = bool(level and _r2_key_exists(f"{ep_row.r2_prefix}analysis_{level}.json"))
            except Exception as exc:
                log.warning("R2 error checking level analysis key for %s, using base: %s", date_str, exc)
                has_level_analysis = False
            analysis_key = (
                f"{ep_row.r2_prefix}analysis_{level}.json"
                if has_level_analysis
                else f"{ep_row.r2_prefix}analysis.json"
            )
            r2_urls = {
                "transcript": _r2_presigned(f"{ep_row.r2_prefix}transcript.json"),
                "analysis":   _r2_presigned(analysis_key),
            }
        return render_template("episode.html", date=date_str, meta=meta, r2_urls=r2_urls)

    # ── File fallback ─────────────────────────────────────────────────────────
    ep = _ep_dir(date_str)
    meta_file = ep / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    return render_template("episode.html", date=date_str, meta=meta, r2_urls={})


def _purge_episode(ep_row: Episode) -> None:
    """Permanently remove one Episode row and unshared R2 objects."""
    if ep_row.r2_prefix and _get_r2():
        # Cross-user clones can share a prefix; keep objects until the final row
        # referencing that prefix is purged.
        with get_db() as db:
            shared_count = db.execute(
                select(func.count()).select_from(Episode).where(
                    Episode.r2_prefix == ep_row.r2_prefix,
                    Episode.id        != ep_row.id,
                )
            ).scalar_one()
        if shared_count == 0:
            paginator = _get_r2().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=_r2_bucket(), Prefix=ep_row.r2_prefix):
                objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objects:
                    _get_r2().delete_objects(
                        Bucket=_r2_bucket(), Delete={"Objects": objects}
                    )
    with get_db() as db:
        row = db.get(Episode, ep_row.id)
        if row:
            db.delete(row)


@app.route("/trash")
@login_required
def trash_page():
    if not db_available():
        return render_template("trash.html", episodes=[])
    user = get_current_user()
    with get_db() as db:
        rows = db.execute(
            select(Episode).where(
                Episode.owner_user_id == user.id,
                Episode.deleted_at.is_not(None),
            ).order_by(Episode.deleted_at.desc())
        ).scalars().all()
    now = datetime.now(timezone.utc)
    episodes = []
    for row in rows:
        purge_at = row.deleted_at + timedelta(days=7)
        episodes.append({
            "slug": row.slug,
            "title": row.title,
            "channel": row.channel,
            "deleted_at": row.deleted_at,
            "purge_at": purge_at,
            "days_remaining": max(0, (purge_at - now).days + 1),
        })
    return render_template("trash.html", episodes=episodes)


@app.route("/episode/<date_str>/restore", methods=["POST"])
@login_required
def episode_restore(date_str: str):
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        with get_db() as db:
            db.execute(
                update(Episode)
                .where(Episode.id == ep_row.id)
                .values(completed_at=None, delete_after=None, deleted_at=None)
            )
        log.info("Restored episode %s from trash as unfinished", date_str)
    return redirect(url_for("library"))


@app.route("/episode/<date_str>/delete", methods=["POST"])
@login_required
def episode_delete(date_str: str):
    # ── DB path ───────────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        _purge_episode(ep_row)
        log.info(f"Deleted episode {date_str} from DB/R2")
        return redirect(url_for("library"))

    # ── File fallback ─────────────────────────────────────────────────────────
    ep = _ep_dir(date_str)
    shutil.rmtree(ep)
    log.info(f"Deleted episode {date_str} from filesystem")
    return redirect(url_for("library"))


def _load_recent_cache() -> dict:
    """Recent-episodes-per-source map written by the bot; {} if absent/broken."""
    try:
        if RECENT_EPISODES_CACHE_FILE.exists():
            return json.loads(RECENT_EPISODES_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON beside its destination, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_sources() -> dict:
    """Load sources with backward-compatible subscription settings."""
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
    except (OSError, json.JSONDecodeError):
        data = {"sources": []}
    raw_sources = data.get("sources", []) if isinstance(data, dict) else []
    sources = []
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        normalized = dict(source)
        normalized["enabled"] = source.get("enabled", True) is not False
        normalized["digest_enabled"] = source.get("digest_enabled", True) is not False
        sources.append(normalized)
    return {**(data if isinstance(data, dict) else {}), "sources": sources}


def _parse_cached_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _source_kind(source: dict) -> str:
    target = f"{source.get('url', '')} {source.get('rss_url', '')}".lower()
    return "youtube" if "youtube.com" in target or "youtu.be" in target else "podcast"


def _source_initials(name: str) -> str:
    words = re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]+", name or "")
    if not words:
        return "•"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _build_subscription_view(sources: list[dict], recent_cache: dict, user) -> tuple[list[dict], list[dict], dict]:
    """Join the feed cache to this user's live episode and job state."""
    db_rows = []
    if user is not None and db_available():
        with get_db() as db:
            db_rows = db.execute(
                select(Episode).where(
                    Episode.owner_user_id == user.id,
                    Episode.deleted_at.is_(None),
                ).order_by(Episode.created_at.desc())
            ).scalars().all()

    rows_by_url = {}
    rows_by_token = {}
    for row in db_rows:  # query is newest-first; retain the newest match
        if row.url:
            rows_by_url.setdefault(row.url, row)
        if row.source_token:
            rows_by_token.setdefault(row.source_token, row)
    user_id = str(user.id) if user is not None else None
    with _jobs_lock:
        active_jobs = [dict(job) for job in _jobs.values()
                       if job.get("status") == "processing" and job.get("user_id") == user_id]
    if user is not None and db_available():
        with get_db() as db:
            durable_jobs = db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.user_id == user.id,
                    ProcessingJob.status.in_(("queued", "running", "retrying")),
                )
            ).scalars().all()
        active_jobs.extend({
            "slug": job.slug,
            "source_token": job.source_token,
        } for job in durable_jobs)
    active_slugs = {job.get("slug") for job in active_jobs}
    active_tokens = {job.get("source_token") for job in active_jobs if job.get("source_token")}

    source_views = []
    inbox = []
    latest_refresh = None
    feed_order = 0
    for source_order, source in enumerate(sources):
        source_url = str(source.get("url", ""))
        cache_entry = recent_cache.get(source_url, {}) if isinstance(recent_cache, dict) else {}
        cached = cache_entry.get("episodes", []) if isinstance(cache_entry, dict) else []
        fetched_at = _parse_cached_datetime(cache_entry.get("fetched_at") if isinstance(cache_entry, dict) else None)
        if fetched_at and (latest_refresh is None or fetched_at > latest_refresh):
            latest_refresh = fetched_at
        view = {
            **source,
            "kind": _source_kind(source),
            "initials": _source_initials(str(source.get("name", ""))),
            "cached_count": len(cached) if isinstance(cached, list) else 0,
            "last_refresh": fetched_at,
        }
        source_views.append(view)
        if not source.get("enabled", True) or not isinstance(cached, list):
            continue
        for item_order, cached_ep in enumerate(cached):
            if not isinstance(cached_ep, dict):
                continue
            ep_url = str(cached_ep.get("url", "")).strip()
            token = _get_source_token(ep_url)
            row = rows_by_url.get(ep_url) or (rows_by_token.get(token) if token else None)
            active = bool((row and row.slug in active_slugs) or (token and token in active_tokens))
            try:
                duration = max(0, int(float(cached_ep.get("duration") or (row.duration if row else 0) or 0)))
            except (TypeError, ValueError):
                duration = max(0, int(row.duration or 0)) if row else 0
            position = max(0.0, (row.max_position or row.resume_position or 0.0)) if row else 0.0
            completed = bool(row and row.completed_at is not None)
            progress = 100 if completed else (min(100, round(position / duration * 100)) if duration else 0)
            published_at = _parse_cached_datetime(cached_ep.get("published_at"))
            inbox.append({
                **cached_ep,
                "url": ep_url,
                "duration": duration,
                "published_at": published_at,
                "source_name": source.get("name") or cached_ep.get("channel") or "Subscription",
                "source_url": source_url,
                "source_kind": view["kind"],
                "source_initials": view["initials"],
                "source_order": source_order,
                "feed_order": feed_order,
                "item_order": item_order,
                "processed": bool(row and not active),
                "active": active,
                "slug": row.slug if row else None,
                "progress": progress,
                "completed": completed,
                "digest_eligible": source.get("digest_enabled", True) is not False,
            })
            feed_order += 1

    # Known publication times win; otherwise preserve source/feed ordering.
    inbox.sort(key=lambda ep: (
        ep["published_at"] is not None,
        ep["published_at"].timestamp() if ep["published_at"] else -ep["feed_order"],
    ), reverse=True)
    needs = [ep for ep in inbox if not ep["processed"]]
    eligible_sources = {ep["source_url"] for ep in needs if ep["digest_eligible"] and not ep["active"]}
    now = datetime.now(timezone.utc)
    freshness = "Missing"
    if latest_refresh:
        freshness = "Fresh" if now - latest_refresh <= timedelta(hours=26) else "Stale"
    metrics = {
        "active_sources": sum(1 for source in sources if source.get("enabled", True)),
        "needs_processing": len(needs),
        "daily_picks": min(2, len(eligible_sources)),
        "last_refresh": latest_refresh,
        "freshness": freshness,
    }
    return source_views, inbox, metrics


def _recommendations_for_user(sources: list[dict]) -> tuple[list[dict], bool]:
    catalog = load_catalog()
    if not catalog:
        return [], False
    user = get_current_user()
    episodes, playback, dismissed = [], {}, set()
    if db_available() and user:
        with get_db() as db:
            episodes = list(db.execute(
                select(Episode).where(Episode.owner_user_id == user.id)
            ).scalars())
            playback = {
                str(row.episode_id): {"percent": row.percent / 100.0, "finished": row.finished}
                for row in db.execute(select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id
                )).scalars()
            }
            dismissed = set(db.execute(select(RecommendationDismissal.candidate_id).where(
                RecommendationDismissal.user_id == user.id
            )).scalars())
    return rank_recommendations(catalog, sources, episodes, playback, dismissed,
                                str(user.id) if user else "local"), True


@app.route("/subscriptions", methods=["GET"])
@login_required
def subscriptions_page():
    sources_data = _load_sources()
    sources, inbox, metrics = _build_subscription_view(
        sources_data.get("sources", []), _load_recent_cache(), get_current_user())
    recommendations, catalog_available = _recommendations_for_user(sources)
    return render_template(
        "subscriptions.html", sources=sources, inbox=inbox, metrics=metrics,
        recommendations=recommendations, catalog_available=catalog_available,
        show_source_manager=False,
    )


@app.route("/sources", methods=["GET"])
@login_required
def sources_page():
    sources_data = _load_sources()
    sources, _, metrics = _build_subscription_view(
        sources_data.get("sources", []), _load_recent_cache(), get_current_user())
    return render_template(
        "subscriptions.html", sources=sources, inbox=[], metrics=metrics,
        recommendations=[], catalog_available=True, show_source_manager=True,
    )


@app.route("/api/subscriptions/recent", methods=["POST"])
@login_required
def api_subscriptions_recent():
    """Receive the recent-episodes cache from the bot's scheduled check and
    persist it for the /subscriptions page.

    Payload: form field `payload` = JSON string mapping each source url to
    {"fetched_at": str, "episodes": [{"title": str, "description": str}, …]}.
    """
    try:
        payload = json.loads(request.form.get("payload", "{}"))
    except Exception:
        return jsonify({"error": "invalid payload JSON"}), 400
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be an object"}), 400

    # Validate + cap sizes (only the owner bot calls this, but stay defensive).
    clean: dict = {}
    for url, entry in list(payload.items())[:100]:
        if not isinstance(entry, dict) or not isinstance(entry.get("episodes"), list):
            continue
        episodes = []
        for ep in entry["episodes"][:10]:
            if not isinstance(ep, dict):
                continue
            try:
                duration = max(0, int(float(ep.get("duration", 0) or 0)))
            except (TypeError, ValueError):
                duration = 0
            episodes.append({
                "title":       str(ep.get("title", ""))[:300],
                "description": str(ep.get("description", ""))[:400],
                "url":         str(ep.get("url", ""))[:1000],
                "channel":     str(ep.get("channel", ""))[:200],
                "published_at": str(ep.get("published_at", ""))[:40],
                "duration":     duration,
            })
        clean[str(url)] = {
            "fetched_at": str(entry.get("fetched_at", ""))[:32],
            "episodes":   episodes,
        }

    with _recent_lock:
        _atomic_write_json(RECENT_EPISODES_CACHE_FILE, clean)
    return jsonify({"ok": True, "sources": len(clean)})


_APPLE_PODCAST_ID_RE = re.compile(r"podcasts\.apple\.com/.*/id(\d+)")


def _resolve_apple_podcast_rss(url: str) -> str | None:
    """For an Apple Podcasts page URL, look up its real RSS feed via Apple's
    iTunes Lookup API. Returns None (never raises) if the URL isn't an Apple
    Podcasts link or the lookup fails — caller just saves without rss_url,
    same as before this existed. yt-dlp has no extractor for podcasts.apple.com
    itself, so without this, subscriptions added via this form silently never
    surface new episodes (bot/preview both rely on rss_url as the fast path)."""
    m = _APPLE_PODCAST_ID_RE.search(url)
    if not m:
        return None
    try:
        import requests
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": m.group(1)},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        feed_url = results[0].get("feedUrl") if results else None
        return feed_url or None
    except Exception as exc:
        log.warning(f"Apple Podcasts RSS lookup failed for {url}: {exc}")
        return None


@app.route("/subscriptions/add", methods=["POST"])
@login_required
def subscriptions_add():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    desc = request.form.get("description", "").strip()

    if not name or not url:
        return redirect(url_for("sources_page", error="Name and URL are required."))

    new_source = {"name": name, "url": url, "description": desc,
                  "enabled": True, "digest_enabled": True}
    rss_url = _resolve_apple_podcast_rss(url)
    if rss_url:
        new_source["rss_url"] = rss_url

    with _sources_lock:
        sources_data = _load_sources()
        if "sources" not in sources_data: sources_data["sources"] = []
        existing_urls = {
            normalize_url(candidate)
            for source in sources_data["sources"]
            for candidate in (source.get("url", ""), source.get("rss_url", ""))
            if candidate
        }
        if normalize_url(url) in existing_urls or (rss_url and normalize_url(rss_url) in existing_urls):
            return jsonify({"error": "That source is already subscribed."}), 409
        sources_data["sources"].append(new_source)
        _atomic_write_json(SOURCES_FILE, sources_data)
    return redirect(url_for("sources_page"))


@app.route("/subscriptions/update", methods=["POST"])
@login_required
def subscriptions_update():
    original_url = request.form.get("original_url", "").strip()
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    description = request.form.get("description", "").strip()
    if not original_url or not name or not url:
        abort(400)
    enabled = request.form.get("enabled") in ("1", "true", "on")
    digest_enabled = request.form.get("digest_enabled") in ("1", "true", "on")
    with _sources_lock:
        data = _load_sources()
        match = next((source for source in data.get("sources", [])
                      if source.get("url") == original_url), None)
        if match is None:
            abort(404)
        url_changed = url != original_url
        match.update(name=name, url=url, description=description,
                     enabled=enabled, digest_enabled=digest_enabled)
        if url_changed:
            match.pop("rss_url", None)
            rss_url = _resolve_apple_podcast_rss(url)
            if rss_url:
                match["rss_url"] = rss_url
        _atomic_write_json(SOURCES_FILE, data)
    return redirect(url_for("sources_page"))


def _catalog_candidate_from_request() -> dict:
    payload = request.get_json(silent=True) if request.is_json else request.form
    candidate_id = str((payload or {}).get("candidate_id", "")).strip()
    if not candidate_id:
        abort(400, "candidate_id is required")
    candidate = next((item for item in load_catalog() if item["id"] == candidate_id), None)
    if candidate is None:
        abort(404, "unknown recommendation candidate")
    return candidate


@app.route("/subscriptions/recommendations/subscribe", methods=["POST"])
@login_required
def recommendation_subscribe():
    candidate = _catalog_candidate_from_request()
    rss_url = candidate.get("rss_url")
    if candidate["type"] == "podcast" and not rss_url:
        rss_url = _resolve_apple_podcast_rss(candidate["url"])
        if not rss_url:
            return jsonify({"error": "The podcast feed is temporarily unavailable."}), 503
    new_source = {
        "name": candidate["name"],
        "url": candidate["url"],
        "description": candidate["description"],
        "enabled": True,
        "digest_enabled": True,
    }
    if rss_url:
        new_source["rss_url"] = rss_url

    with _sources_lock:
        data = _load_sources()
        existing = {
            normalize_url(value)
            for source in data["sources"]
            for value in (source.get("url", ""), source.get("rss_url", ""))
            if value
        }
        candidate_urls = {normalize_url(candidate["url"])}
        if rss_url:
            candidate_urls.add(normalize_url(rss_url))
        if existing & candidate_urls:
            return jsonify({"error": "That source is already subscribed."}), 409
        data["sources"].append(new_source)
        _atomic_write_json(SOURCES_FILE, data)
    return redirect(url_for("subscriptions_page"))


@app.route("/subscriptions/recommendations/dismiss", methods=["POST"])
@login_required
def recommendation_dismiss():
    candidate = _catalog_candidate_from_request()
    user = get_current_user()
    if not db_available() or not user:
        return jsonify({"error": "Dismissals require an authenticated account."}), 503
    with get_db() as db:
        exists = db.execute(select(RecommendationDismissal.id).where(
            RecommendationDismissal.user_id == user.id,
            RecommendationDismissal.candidate_id == candidate["id"],
        )).scalar_one_or_none()
        if exists is None:
            db.add(RecommendationDismissal(user_id=user.id, candidate_id=candidate["id"]))
    return redirect(url_for("subscriptions_page"))


@app.route("/subscriptions/delete", methods=["POST"])
@login_required
def subscriptions_delete():
    url = request.form.get("url", "").strip()
    if not url:
        abort(400)

    with _sources_lock:
        sources_data = _load_sources()
        sources_data["sources"] = [s for s in sources_data.get("sources", []) if s.get("url") != url]
        _atomic_write_json(SOURCES_FILE, sources_data)
    return redirect(url_for("sources_page"))


@app.route("/vocab")
@login_required
def vocab_page():
    return render_template("vocab.html")


def _vocab_occurrence_to_dict(row: VocabOccurrence) -> dict:
    return {
        "id":                     str(row.id),
        "episode_slug":           row.episode_slug_snapshot,
        "episode_title":          row.episode_title_snapshot,
        "segment_index":          row.segment_index,
        "start_time":             row.start_time,
        "end_time":               row.end_time,
        "source_text":            row.source_text,
        "source_en":              row.source_en,
        "source_zh":              row.source_zh,
        "clip_available":         bool(row.clip_key),
        "source_episode_available": row.episode_id is not None,
        "saved_at": row.saved_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.saved_at else "",
    }


def _vocab_item_to_dict(row: VocabItem, occurrences: list[VocabOccurrence] | None = None) -> dict:
    """Serialize a VocabItem ORM row to the same dict shape the frontend expects."""
    return {
        "id":             str(row.id),
        "word":           row.word,
        "reading":        row.reading,
        "en":             row.en,
        "zh":             row.zh,
        "example":        row.example,
        "level":          row.level,
        "type":           row.type,
        "source_episode": row.source_episode,
        "saved_at":       row.saved_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.saved_at else "",
        "occurrences":     [_vocab_occurrence_to_dict(o) for o in (occurrences or [])],
        "review": {
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "interval_days": row.interval_days or 0,
            "repetitions": row.repetitions or 0,
            "lapses": row.lapses or 0,
            "suspended": bool(row.suspended),
            "last_reviewed_at": row.last_reviewed_at.isoformat() if row.last_reviewed_at else None,
        },
    }


def _context_number(value, *, integer: bool = False):
    if value is None or value == "":
        return None
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _context_text(value, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _occurrences_for_rows(db, rows: list[VocabItem]) -> dict:
    occurrences_by_item: dict[uuid.UUID, list[VocabOccurrence]] = {}
    if rows:
        occurrences = db.execute(
            select(VocabOccurrence)
            .where(VocabOccurrence.vocab_item_id.in_([r.id for r in rows]))
            .order_by(VocabOccurrence.saved_at.desc())
        ).scalars().all()
        for occurrence in occurrences:
            occurrences_by_item.setdefault(occurrence.vocab_item_id, []).append(occurrence)
    return occurrences_by_item


@app.route("/api/vocab", methods=["GET"])
@login_required
def api_vocab_get():
    q     = request.args.get("q", "").lower().strip()
    level = request.args.get("level", "").lower().strip()
    vtype = request.args.get("type", "").lower().strip()

    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user is None:
            return jsonify([])
        with get_db() as db:
            stmt = select(VocabItem).where(VocabItem.user_id == user.id)
            if level and level != "all":
                stmt = stmt.where(VocabItem.level == level)
            if vtype and vtype != "all":
                stmt = stmt.where(VocabItem.type == vtype)
            rows = db.execute(stmt).scalars().all()
            occurrences_by_item = _occurrences_for_rows(db, rows)
        items = [_vocab_item_to_dict(r, occurrences_by_item.get(r.id, [])) for r in rows]
        if q:
            items = [i for i in items if
                     q in i["word"].lower() or q in i["reading"].lower() or
                     q in i["en"].lower()   or q in i["zh"].lower()]
        return jsonify(items)

    # ── File fallback (local dev without Postgres) ────────────────────────────
    with _vocab_lock:
        data = json.loads(VOCAB_FILE.read_text(encoding="utf-8")) if VOCAB_FILE.exists() else {"items": []}
    items = data.get("items", [])
    if q:
        items = [i for i in items if q in i.get("word", "").lower() or q in i.get("reading", "").lower() or q in i.get("en", "").lower() or q in i.get("zh", "").lower()]
    if level and level != "all":
        items = [i for i in items if i.get("level", "").lower() == level]
    if vtype and vtype != "all":
        items = [i for i in items if i.get("type", "").lower() == vtype]
    return jsonify(items)


@app.route("/review")
@login_required
def review_page():
    return render_template("review.html")


@app.route("/api/review/due")
@login_required
def api_review_due():
    if not db_available():
        return jsonify({"items": [], "total_due": 0})
    user = get_current_user()
    if user is None:
        return jsonify({"items": [], "total_due": 0})
    try:
        limit = min(50, max(1, int(request.args.get("limit", "10"))))
    except ValueError:
        limit = 10
    now = datetime.now(timezone.utc)
    due_filter = (
        VocabItem.user_id == user.id,
        VocabItem.suspended.is_(False),
        or_(VocabItem.due_at.is_(None), VocabItem.due_at <= now),
    )
    with get_db() as db:
        total_due = db.execute(
            select(func.count()).select_from(VocabItem).where(*due_filter)
        ).scalar_one()
        rows = db.execute(
            select(VocabItem)
            .where(*due_filter)
            .order_by(VocabItem.due_at.asc().nullsfirst(), VocabItem.saved_at.asc())
            .limit(limit)
        ).scalars().all()
        occurrences_by_item = _occurrences_for_rows(db, rows)
    return jsonify({
        "items": [_vocab_item_to_dict(row, occurrences_by_item.get(row.id, [])) for row in rows],
        "total_due": total_due,
    })


@app.route("/api/review/<item_id>/answer", methods=["POST"])
@login_required
def api_review_answer(item_id: str):
    if not db_available():
        return jsonify({"error": "Review scheduling requires the database"}), 503
    user = get_current_user()
    if user is None:
        return jsonify({"error": "Not authenticated"}), 401
    rating = _context_text((request.json or {}).get("rating"), 16).lower()
    if rating not in {"again", "hard", "good"}:
        return jsonify({"error": "Rating must be again, hard, or good"}), 400
    try:
        item_uuid = uuid.UUID(item_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Not found"}), 404

    now = datetime.now(timezone.utc)
    with get_db() as db:
        row = db.execute(select(VocabItem).where(
            VocabItem.id == item_uuid,
            VocabItem.user_id == user.id,
        )).scalar_one_or_none()
        if row is None:
            return jsonify({"error": "Not found"}), 404

        log_row = ReviewLog(
            vocab_item_id=row.id,
            user_id=user.id,
            rating=rating,
            previous_due_at=row.due_at,
            previous_interval_days=row.interval_days or 0,
            previous_repetitions=row.repetitions or 0,
            previous_lapses=row.lapses or 0,
            previous_last_reviewed_at=row.last_reviewed_at,
        )
        db.add(log_row)

        old_interval = row.interval_days or 0
        old_repetitions = row.repetitions or 0
        if rating == "again":
            row.interval_days = 0
            row.repetitions = 0
            row.lapses = (row.lapses or 0) + 1
            row.due_at = now + timedelta(minutes=10)
        elif rating == "hard":
            row.interval_days = max(1.0, round(old_interval * 1.5, 2))
            row.due_at = now + timedelta(days=row.interval_days)
        else:
            row.interval_days = 3.0 if old_repetitions == 0 else max(
                old_interval + 1.0, round(old_interval * 2.3, 2)
            )
            row.repetitions = old_repetitions + 1
            row.due_at = now + timedelta(days=row.interval_days)
        row.last_reviewed_at = now
        db.flush()
        log_id = str(log_row.id)
        due_at = row.due_at.isoformat()

    return jsonify({
        "status": "scheduled",
        "rating": rating,
        "due_at": due_at,
        "undo_id": log_id,
    })


@app.route("/api/review/undo", methods=["POST"])
@login_required
def api_review_undo():
    if not db_available():
        return jsonify({"error": "Review scheduling requires the database"}), 503
    user = get_current_user()
    if user is None:
        return jsonify({"error": "Not authenticated"}), 401
    undo_id = _context_text((request.json or {}).get("undo_id"), 64)
    try:
        undo_uuid = uuid.UUID(undo_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Undo is no longer available"}), 404
    with get_db() as db:
        log_row = db.execute(select(ReviewLog).where(
            ReviewLog.id == undo_uuid,
            ReviewLog.user_id == user.id,
        )).scalar_one_or_none()
        if log_row is None:
            return jsonify({"error": "Undo is no longer available"}), 404
        latest_id = db.execute(
            select(ReviewLog.id)
            .where(
                ReviewLog.vocab_item_id == log_row.vocab_item_id,
                ReviewLog.user_id == user.id,
            )
            .order_by(ReviewLog.reviewed_at.desc(), ReviewLog.id.desc())
            .limit(1)
        ).scalar_one()
        if latest_id != log_row.id:
            return jsonify({"error": "Only the latest answer can be undone"}), 409
        item = db.get(VocabItem, log_row.vocab_item_id)
        if item is None:
            return jsonify({"error": "Review item no longer exists"}), 404
        item.due_at = log_row.previous_due_at
        item.interval_days = log_row.previous_interval_days
        item.repetitions = log_row.previous_repetitions
        item.lapses = log_row.previous_lapses
        item.last_reviewed_at = log_row.previous_last_reviewed_at
        db.delete(log_row)
    return jsonify({"status": "restored"})


@app.route("/api/vocab", methods=["POST"])
@login_required
def api_vocab_add():
    new_item = request.json
    if not new_item or not new_item.get("front"):
        return jsonify({"error": "Invalid data"}), 400

    word = new_item["front"]

    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user is None:
            return jsonify({"error": "Not authenticated"}), 401
        with get_db() as db:
            existing = db.execute(
                select(VocabItem).where(
                    VocabItem.user_id == user.id,
                    VocabItem.word    == word,
                )
            ).scalar_one_or_none()
            created = existing is None
            row = existing or VocabItem(
                user_id        = user.id,
                word           = word,
                reading        = _context_text(new_item.get("reading"), 500),
                en             = _context_text(new_item.get("en"), 2000),
                zh             = _context_text(new_item.get("zh"), 2000),
                example        = _context_text(new_item.get("example")),
                level          = _context_text(new_item.get("level"), 100),
                type           = _context_text(new_item.get("type", "vocab"), 100),
                source_episode = _context_text(new_item.get("source_episode"), 100),
            )
            if created:
                db.add(row)
                db.flush()

            occurrence_added = False
            source_slug = _context_text(new_item.get("source_episode"), 100)
            if source_slug and _SLUG_RE.match(source_slug):
                episode = db.execute(
                    select(Episode).where(
                        Episode.owner_user_id == user.id,
                        Episode.slug == source_slug,
                    )
                ).scalar_one_or_none()
                if episode:
                    start_time = _context_number(new_item.get("source_start"))
                    occurrence_stmt = select(VocabOccurrence).where(
                        VocabOccurrence.vocab_item_id == row.id,
                        VocabOccurrence.episode_id == episode.id,
                    )
                    if start_time is not None:
                        occurrence_stmt = occurrence_stmt.where(VocabOccurrence.start_time == start_time)
                    else:
                        occurrence_stmt = occurrence_stmt.where(
                            VocabOccurrence.segment_index == _context_number(
                                new_item.get("source_segment_index"), integer=True
                            )
                        )
                    occurrence = db.execute(occurrence_stmt).scalar_one_or_none()
                    if occurrence is None:
                        db.add(VocabOccurrence(
                            vocab_item_id=row.id,
                            episode_id=episode.id,
                            episode_slug_snapshot=episode.slug,
                            episode_title_snapshot=episode.title,
                            segment_index=_context_number(new_item.get("source_segment_index"), integer=True),
                            start_time=start_time,
                            end_time=_context_number(new_item.get("source_end")),
                            source_text=_context_text(new_item.get("source_text") or new_item.get("example")),
                            source_en=_context_text(new_item.get("source_en"), 4000),
                            source_zh=_context_text(new_item.get("source_zh"), 4000),
                        ))
                        occurrence_added = True

            status = "success" if created else ("occurrence_added" if occurrence_added else "exists")
            response_id = str(row.id)
        return jsonify({"status": status, "id": response_id}), 201 if created else 200

    # ── File fallback ─────────────────────────────────────────────────────────
    with _vocab_lock:
        data = json.loads(VOCAB_FILE.read_text(encoding="utf-8")) if VOCAB_FILE.exists() else {"items": []}
        items = data.get("items", [])

        existing = next((i for i in items if i.get("word") == word), None)
        if existing:
            occurrences = existing.setdefault("occurrences", [])
            source_start = _context_number(new_item.get("source_start"))
            duplicate = any(
                o.get("episode_slug") == new_item.get("source_episode")
                and o.get("start_time") == source_start
                for o in occurrences
            )
            if not duplicate and new_item.get("source_episode"):
                occurrences.append({
                    "id": str(uuid.uuid4())[:8],
                    "episode_slug": new_item.get("source_episode", ""),
                    "episode_title": "",
                    "segment_index": _context_number(new_item.get("source_segment_index"), integer=True),
                    "start_time": source_start,
                    "end_time": _context_number(new_item.get("source_end")),
                    "source_text": _context_text(new_item.get("source_text") or new_item.get("example")),
                    "source_en": _context_text(new_item.get("source_en")),
                    "source_zh": _context_text(new_item.get("source_zh")),
                    "source_episode_available": True,
                })
                VOCAB_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                return jsonify({"status": "occurrence_added", "id": existing.get("id")}), 200
            return jsonify({"status": "exists", "id": existing.get("id")}), 200

        item = {
            "id": str(uuid.uuid4())[:8],
            "word": word,
            "reading": new_item.get("reading", ""),
            "en": new_item.get("en", ""),
            "zh": new_item.get("zh", ""),
            "example": new_item.get("example", ""),
            "level": new_item.get("level", ""),
            "type": new_item.get("type", ""),
            "source_episode": new_item.get("source_episode", ""),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "occurrences": [{
                "id": str(uuid.uuid4())[:8],
                "episode_slug": new_item.get("source_episode", ""),
                "episode_title": "",
                "segment_index": _context_number(new_item.get("source_segment_index"), integer=True),
                "start_time": _context_number(new_item.get("source_start")),
                "end_time": _context_number(new_item.get("source_end")),
                "source_text": _context_text(new_item.get("source_text") or new_item.get("example")),
                "source_en": _context_text(new_item.get("source_en")),
                "source_zh": _context_text(new_item.get("source_zh")),
                "source_episode_available": True,
            }] if new_item.get("source_episode") else [],
        }
        items.append(item)
        data["items"] = items
        VOCAB_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return jsonify({"status": "success", "id": item["id"]}), 201


@app.route("/api/vocab/<item_id>", methods=["DELETE"])
@login_required
def api_vocab_delete(item_id):
    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user is None:
            return jsonify({"error": "Not authenticated"}), 401
        with get_db() as db:
            row = db.execute(
                select(VocabItem).where(
                    VocabItem.user_id == user.id,
                    VocabItem.id      == item_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return jsonify({"error": "Not found"}), 404
            db.delete(row)
        return jsonify({"status": "deleted"}), 200

    # ── File fallback ─────────────────────────────────────────────────────────
    if not VOCAB_FILE.exists():
        return jsonify({"error": "Not found"}), 404

    with _vocab_lock:
        data = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))
        items = data.get("items", [])
        new_items = [i for i in items if i.get("id") != item_id]

        if len(items) == len(new_items):
            return jsonify({"error": "Not found"}), 404

        data["items"] = new_items
        VOCAB_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return jsonify({"status": "deleted"}), 200


@app.route("/vocab/export.csv")
@login_required
def vocab_export():
    import csv
    import io

    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user is None:
            return "Not authenticated", 401
        with get_db() as db:
            rows = db.execute(
                select(VocabItem).where(VocabItem.user_id == user.id)
                .order_by(VocabItem.saved_at)
            ).scalars().all()
        items = [_vocab_item_to_dict(r) for r in rows]
    else:
        # ── File fallback ─────────────────────────────────────────────────────
        if not VOCAB_FILE.exists():
            return "No vocab found", 404
        data  = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))
        items = data.get("items", [])

    if not items:
        return "No vocab found", 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Front", "Reading", "English", "Chinese", "Example", "Level", "Type"])
    for i in items:
        writer.writerow([
            i.get("word", ""),
            i.get("reading", ""),
            i.get("en", ""),
            i.get("zh", ""),
            i.get("example", ""),
            i.get("level", ""),
            i.get("type", ""),
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    output.close()

    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"vocab-export-{time.strftime('%Y%m%d')}.csv",
    )


def _quota_context(user) -> dict:
    """Build the quota dict passed to upload.html."""
    if not db_available() or not user:
        return {"unlimited": True, "used": 0, "limit": -1, "allowed": True}
    allowed, used, limit = _check_quota(user)
    return {"unlimited": limit == -1, "used": used, "limit": limit, "allowed": allowed}


@app.route("/upload", methods=["GET"])
@login_required
def upload_page():
    user = get_current_user()
    return render_template("upload.html", quota=_quota_context(user))


@app.route("/upload", methods=["POST"])
@login_required
def upload_process():
    from lib.analyzer import LEVELS, DEFAULT_LEVEL

    level = request.form.get("level", DEFAULT_LEVEL).strip()
    if level not in LEVELS:
        level = DEFAULT_LEVEL

    current_user = get_current_user()
    user_id      = current_user.id if current_user else None

    # Determine if this user is exempt from size / quota limits.
    # The atomic quota check+insert happens later (after the file is staged)
    # so that the check and insert share a single transaction (Bug #1 fix).
    unlimited = bool(current_user and _is_unlimited(current_user))
    quota     = _quota_context(current_user)

    source_url  = request.form.get("source_url", "").strip()
    job_id      = str(uuid.uuid4())
    base_slug   = date.today().isoformat()
    slug        = _unique_ep_slug(base_slug, user_id=user_id)
    ep_dir      = EPISODES_DIR / slug
    ep_dir.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = None
    meta: dict = {}
    clone_from_id: str | None = None
    clone_from_level: str | None = None

    if source_url:
        own_slug, clone_from_id, clone_from_level = _find_source_clone(source_url, user_id)
        if own_slug:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return redirect(url_for("episode", date_str=own_slug))

        # URL path — download + size check happens inside the thread
        title_override = request.form.get("title", "").strip()
        meta = {
            "title":       title_override or source_url,
            "channel":     request.form.get("channel", "").strip(),
            "upload_date": date.today().strftime("%Y%m%d"),
            "duration":    0,
            "url":         source_url,
            "thumbnail":   "",
            "description": "",
            "video_id":    "",
            "source":      "url",
            "level":       level,
        }
    else:
        # File upload — save synchronously, check size, then process in thread
        if "audio" not in request.files:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template("upload.html", quota=quota, error="No file or URL provided."), 400

        f = request.files["audio"]
        if not f.filename:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template("upload.html", quota=quota, error="No file selected."), 400

        suffix = Path(f.filename).suffix.lower()
        if suffix not in UPLOAD_EXTENSIONS:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template(
                "upload.html",
                quota=quota,
                error=f"Unsupported format '{suffix}'. Accepted: {', '.join(sorted(UPLOAD_EXTENSIONS))}",
            ), 400

        title      = request.form.get("title", "").strip() or Path(f.filename).stem
        audio_path = ep_dir / f"audio{suffix}"

        # Bug #2 fix: reject oversized uploads before writing to disk using the
        # Content-Length header.  The definitive stat() check below still runs
        # after the save so spoofed headers are caught too.
        if not unlimited:
            cl = request.content_length
            if cl and cl > _TRANSCRIPTION_MAX_BYTES:
                shutil.rmtree(ep_dir, ignore_errors=True)
                return render_template(
                    "upload.html",
                    quota=quota,
                    error=f"Audio file is too large — only files under 23 MB are supported. "
                          "Contact the admin if you need longer content.",
                ), 400

        f.save(audio_path)

        # Definitive size cap (catches cases where Content-Length was missing/wrong)
        if not unlimited and audio_path.stat().st_size > _TRANSCRIPTION_MAX_BYTES:
            size_mb = audio_path.stat().st_size // (1024 * 1024)
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template(
                "upload.html",
                quota=quota,
                error=f"Audio file is {size_mb} MB — only files under 23 MB are supported. "
                      "Contact the admin if you need longer content.",
            ), 400

        meta = {
            "title":             title,
            "channel":           "Upload",
            "upload_date":       date.today().strftime("%Y%m%d"),
            "duration":          0,
            "url":               "",
            "thumbnail":         "",
            "description":       f"Uploaded file: {f.filename}",
            "video_id":          "",
            "source":            "upload",
            "original_filename": f.filename,
            "level":             level,
        }

    # ── Atomic quota check + usage insert ────────────────────────────────────
    usage_id = None
    if not clone_from_id and db_available() and current_user:
        try:
            audio_bytes = audio_path.stat().st_size if audio_path else 0
            usage = _atomic_quota_insert(current_user, audio_bytes=audio_bytes)
            usage_id = str(usage.id)
        except ValueError as qe:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template(
                "upload.html",
                quota=_quota_context(current_user),
                error=str(qe),
            ), 429

    if clone_from_id:
        total_steps = 1 if (clone_from_level == level) else 3
    else:
        total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    # ── Skeleton Episode row ──────────────────────────────────────────────────
    # Insert a minimal Episode row now (before the thread starts) so that any
    # concurrent submission of the same URL by this user is caught immediately
    # by _find_source_clone rather than after the pipeline finishes (minutes later).
    # The thread updates this row with enriched metadata on completion, or
    # deletes it on failure.  Only for URL submissions — file uploads have no
    # source_token and don't benefit from dedup.
    if source_url and user_id and db_available() and not clone_from_id:
        _skel_token = _get_source_token(source_url)
        if _skel_token:
            try:
                with get_db() as db:
                    db.add(Episode(
                        owner_user_id = user_id,
                        slug          = slug,
                        date          = slug[:10],
                        title         = meta.get("title", source_url),
                        channel       = meta.get("channel", ""),
                        url           = source_url,
                        thumbnail     = "",
                        duration      = 0,
                        level         = level,
                        source        = meta.get("source", "url"),
                        source_token  = _skel_token,
                        r2_prefix     = "",
                    ))
            except Exception as _skel_exc:
                log.warning("Could not insert skeleton Episode row for %s: %s", slug, _skel_exc)

    # Register job and start background thread
    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "processing",
            "slug":        slug,
            "user_id":     str(user_id) if user_id else None,
            "step":        "Starting…",
            "step_num":    0,
            "total_steps": total_steps,
            "error":       "",
            "started_at":  time.time(),
            "source_token": _get_source_token(source_url),
        }

    try:
        _persist_processing_job(
            job_id, slug, user_id, source_url, level, total_steps, meta,
            usage_id=usage_id, unlimited=unlimited, clone_from_id=clone_from_id,
        )
    except IntegrityError:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        shutil.rmtree(ep_dir, ignore_errors=True)
        if usage_id:
            with get_db() as db:
                db.execute(update(TranscriptionUsage).where(
                    TranscriptionUsage.id == uuid.UUID(str(usage_id))
                ).values(status="failed"))
                db.execute(delete(Episode).where(
                    Episode.owner_user_id == user_id,
                    Episode.slug == slug,
                    Episode.r2_prefix == "",
                ))
        if user_id and _get_source_token(source_url):
            with get_db() as db:
                active = db.execute(select(ProcessingJob).where(
                    ProcessingJob.user_id == user_id,
                    ProcessingJob.source_token == _get_source_token(source_url),
                    ProcessingJob.status.in_(["queued", "running", "retrying"]),
                )).scalar_one_or_none()
            if active:
                return redirect(url_for("job_page", job_id=str(active.id)))
        raise

    _prune_old_jobs()

    t = threading.Thread(
        target=_pipeline_thread,
        args=(job_id, slug, ep_dir, source_url or None, audio_path, meta, level),
        kwargs={
            "user_id": user_id,
            "usage_id": usage_id,
            "unlimited": unlimited,
            "clone_from_id": clone_from_id
        },
        daemon=True,
    )
    t.start()
    log.info(f"Started job {job_id} for slug {slug!r}")

    return redirect(url_for("job_page", job_id=job_id))


# ── Job status ────────────────────────────────────────────────────────────────

def _owned_processing_job(job_id: str) -> "ProcessingJob | None":
    if not db_available():
        return None
    user = get_current_user()
    if user is None:
        return None
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, TypeError):
        return None
    with get_db() as db:
        return db.execute(select(ProcessingJob).where(
            ProcessingJob.id == parsed,
            ProcessingJob.user_id == user.id,
        )).scalar_one_or_none()

@app.route("/job/<job_id>")
@login_required
def job_page(job_id: str):
    durable = _owned_processing_job(job_id)
    if durable is not None:
        return render_template("job.html", job_id=job_id, slug=durable.slug)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return render_template("job.html", job_id=job_id, slug=job["slug"])


@app.route("/api/job/<job_id>/status")
@login_required
def api_job_status(job_id: str):
    durable = _owned_processing_job(job_id)
    if durable is not None:
        return jsonify(_job_to_dict(durable))
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":      job["status"],
        "step":        job.get("step", ""),
        "step_num":    job.get("step_num", 0),
        "total_steps": job.get("total_steps", 5),
        "slug":        job["slug"],
        "error":       job.get("error", ""),
    })


@app.route("/api/jobs/active")
@login_required
def api_jobs_active():
    """Return all in-progress jobs belonging to the current user."""
    user = get_current_user()
    uid = str(user.id) if user else None
    if db_available() and user:
        with get_db() as db:
            rows = db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.user_id == user.id,
                    ProcessingJob.status.in_(["queued", "running", "retrying"]),
                )
                .order_by(ProcessingJob.created_at.desc())
            ).scalars().all()
        return jsonify({"jobs": [_job_to_dict(row) for row in rows]})
    with _jobs_lock:
        jobs = [
            {
                "job_id":      jid,
                "slug":        j["slug"],
                "status":      j["status"],
                "step":        j.get("step", ""),
                "step_num":    j.get("step_num", 0),
                "total_steps": j.get("total_steps", 5),
            }
            for jid, j in _jobs.items()
            if j.get("status") == "processing" and j.get("user_id") == uid
        ]
    return jsonify({"jobs": jobs})


@app.route("/api/jobs/history")
@login_required
def api_jobs_history():
    if not db_available():
        return jsonify({"jobs": []})
    user = get_current_user()
    if user is None:
        return jsonify({"jobs": []})
    with get_db() as db:
        rows = db.execute(
            select(ProcessingJob)
            .where(ProcessingJob.user_id == user.id)
            .order_by(ProcessingJob.created_at.desc())
            .limit(50)
        ).scalars().all()
    return jsonify({"jobs": [_job_to_dict(row) for row in rows]})


@app.route("/activity")
@login_required
def activity_page():
    return render_template("activity.html")


@app.route("/api/job/<job_id>/retry", methods=["POST"])
@login_required
def api_job_retry(job_id: str):
    job = _owned_processing_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job.status != "failed":
        return jsonify({"error": "Only failed jobs can be retried"}), 409
    if job.attempt_count >= 3:
        return jsonify({"error": "Retry limit reached"}), 409

    with get_db() as db:
        stages = set(db.execute(select(ProcessingArtifact.stage).where(
            ProcessingArtifact.job_id == job.id,
            ProcessingArtifact.validated.is_(True),
        )).scalars().all())
    if not job.source_url and "audio" not in stages:
        return jsonify({"error": "The uploaded audio checkpoint has expired; upload the file again."}), 409

    ep_dir = EPISODES_DIR / job.slug
    shutil.rmtree(ep_dir, ignore_errors=True)
    ep_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job.id, ProcessingJob.status == "failed")
            .values(
                status="retrying",
                current_step=f"Retrying from {job.retry_from}…",
                error_code="",
                error_message="",
                finished_at=None,
                updated_at=now,
                artifacts_expire_at=None,
            )
        )
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "processing",
            "slug": job.slug,
            "user_id": str(job.user_id),
            "step": f"Retrying from {job.retry_from}…",
            "step_num": 0,
            "total_steps": job.total_steps,
            "error": "",
            "started_at": time.time(),
            "source_token": job.source_token,
        }
    threading.Thread(
        target=_pipeline_thread,
        args=(job_id, job.slug, ep_dir, job.source_url or None, None, job.meta_json or {}, job.level),
        kwargs={
            "user_id": job.user_id,
            "usage_id": job.usage_id,
            "unlimited": job.unlimited,
            "clone_from_id": job.clone_from_id,
        },
        daemon=True,
    ).start()
    return jsonify({"status": "retrying", "job_id": job_id, "slug": job.slug})


# ── Static episode assets ─────────────────────────────────────────────────────

@app.route("/episode/<date_str>/audio")
@login_required
def episode_audio(date_str: str):
    # ── DB / R2 path ──────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        if ep_row.r2_prefix and _get_r2():
            result = _r2_find_audio(ep_row.r2_prefix)
            if result:
                key, _ = result
                return redirect(_r2_presigned(key, expires=7200))
        # Bug #4 fix: fall through to local disk when r2_prefix is empty
        # (R2 not configured) rather than aborting 404.
        if ep_row.r2_prefix:
            abort(404)

    # ── File fallback ─────────────────────────────────────────────────────────
    audio = _find_audio(_ep_dir(date_str))
    if not audio:
        abort(404)
    mimetype = mimetypes.guess_type(audio.name)[0] or "audio/mpeg"
    return send_file(audio, mimetype=mimetype, conditional=True)


@app.route("/episode/<date_str>/subtitles.vtt")
@login_required
def episode_vtt(date_str: str):
    # ── DB / R2 path ──────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        if ep_row.r2_prefix and _get_r2():
            return redirect(_r2_presigned(f"{ep_row.r2_prefix}subtitles.vtt"))
        if ep_row.r2_prefix:
            abort(404)

    # ── File fallback ─────────────────────────────────────────────────────────
    vtt = _ep_dir(date_str) / "subtitles.vtt"
    if not vtt.exists():
        abort(404)
    return _make_response_cached(send_file(vtt, mimetype="text/vtt"))


@app.route("/episode/<date_str>/cards.csv")
@login_required
def episode_cards(date_str: str):
    # ── DB / R2 path ──────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        target_file = f"cards_{ep_row.level}.csv"
        if ep_row.r2_prefix and _get_r2():
            try:
                use_level_cards = _r2_key_exists(f"{ep_row.r2_prefix}{target_file}")
            except Exception as exc:
                log.warning("R2 error checking level cards key for %s, using base: %s", date_str, exc)
                use_level_cards = False
            if use_level_cards:
                return redirect(_r2_presigned(f"{ep_row.r2_prefix}{target_file}"))
            return redirect(_r2_presigned(f"{ep_row.r2_prefix}cards.csv"))
        if ep_row.r2_prefix:
            abort(404)

    # ── File fallback ─────────────────────────────────────────────────────────
    target_file = "cards.csv"
    if ep_row is not None:
        level_file = f"cards_{ep_row.level}.csv"
        if (_ep_dir(date_str) / level_file).exists():
            target_file = level_file

    csv_file = _ep_dir(date_str) / target_file
    if not csv_file.exists():
        abort(404)
    return send_file(
        csv_file,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"cards-{date_str}.csv",
    )


# ── JSON API ──────────────────────────────────────────────────────────────────

def _episode_json_response(date_str: str, filename: str):
    """Shared helper: serve episode JSON from R2 (DB path) or local file (fallback)."""
    ep_row = _lookup_episode(date_str)
    target_file = filename
    if ep_row is not None:
        if filename in ("analysis.json", "highlights.json"):
            base_name, ext = filename.split(".", 1)
            level_filename = f"{base_name}_{ep_row.level}.{ext}"
            if ep_row.r2_prefix and _get_r2():
                try:
                    if _r2_key_exists(f"{ep_row.r2_prefix}{level_filename}"):
                        target_file = level_filename
                except Exception as exc:
                    log.warning("R2 error checking level JSON key for %s, using base: %s", date_str, exc)
            else:
                if (_ep_dir(date_str) / level_filename).exists():
                    target_file = level_filename

        if ep_row.r2_prefix and _get_r2():
            # Redirect to a presigned R2 URL so the browser fetches directly
            # from R2 (APAC) instead of proxying through Railway.
            # Requires CORS on the R2 bucket for https://mimichan.ziwei-chen.com.
            return redirect(_r2_presigned(f"{ep_row.r2_prefix}{target_file}"))
        # Bug #4 fix: fall through to local disk when r2_prefix is empty.
        if ep_row.r2_prefix:
            abort(404)

    return _make_response_cached(jsonify(_read_json(_ep_dir(date_str) / target_file)))


@app.route("/api/episode/<date_str>/meta")
@login_required
def api_meta(date_str: str):
    return _episode_json_response(date_str, "meta.json")


@app.route("/api/episode/<date_str>/transcript")
@login_required
def api_transcript(date_str: str):
    return _episode_json_response(date_str, "transcript.json")


@app.route("/api/episode/<date_str>/analysis")
@login_required
def api_analysis(date_str: str):
    return _episode_json_response(date_str, "analysis.json")


@app.route("/api/episode/<date_str>/resume", methods=["GET"])
@login_required
def api_resume_get(date_str: str):
    """Cross-device playback resume position, keyed by owner + episode."""
    ep_row = _lookup_episode(date_str)
    if ep_row is None:
        return jsonify({"t": None, "updated_at": None})
    return jsonify({
        "t": ep_row.resume_position,
        "max_position": ep_row.max_position,
        "completed": ep_row.completed_at is not None,
        "retention_exempt": ep_row.retention_exempt,
        "delete_after": ep_row.delete_after.isoformat() if ep_row.delete_after else None,
        "deleted_at": ep_row.deleted_at.isoformat() if ep_row.deleted_at else None,
        "updated_at": ep_row.resume_updated_at.isoformat() if ep_row.resume_updated_at else None,
    })


@app.route("/api/episode/<date_str>/resume", methods=["POST"])
@login_required
def api_resume_set(date_str: str):
    data = request.get_json(silent=True) or {}
    t = data.get("t")
    if not isinstance(t, (int, float)) or t < 0:
        abort(400)
    ep_row = _lookup_episode(date_str)
    if ep_row is None:
        return jsonify({"status": "ok"})  # no DB configured — nothing to persist
    now = datetime.now(timezone.utc)
    values = {
        "resume_position": float(t),
        "resume_updated_at": now,
        "max_position": func.greatest(func.coalesce(Episode.max_position, 0.0), float(t)),
    }
    completed = ep_row.completed_at is not None
    delete_after = ep_row.delete_after
    if data.get("manual_completion") is True and data.get("completed") is False:
        values.update(completed_at=None, delete_after=None, deleted_at=None)
        if data.get("reset_progress") is True:
            values.update(resume_position=0.0, max_position=0.0)
        completed = False
        delete_after = None
    elif data.get("completed") is True or (ep_row.duration > 0 and float(t) / ep_row.duration >= 0.9):
        values["completed_at"] = ep_row.completed_at or now
        if not ep_row.retention_exempt and ep_row.delete_after is None:
            delete_after = now + timedelta(days=30)
            values["delete_after"] = delete_after
        completed = True
    with get_db() as db:
        db.execute(
            update(Episode)
            .where(Episode.id == ep_row.id)
            .values(**values)
        )
    return jsonify({
        "status": "ok",
        "completed": completed,
        "retention_exempt": ep_row.retention_exempt,
        "delete_after": delete_after.isoformat() if delete_after else None,
        "deleted_at": None,
    })


@app.route("/api/episode/<date_str>/retention", methods=["POST"])
@login_required
def api_episode_retention(date_str: str):
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("keep"), bool):
        abort(400)
    ep_row = _lookup_episode(date_str)
    if ep_row is None:
        return jsonify({"status": "ok"})

    keep = data["keep"]
    delete_after = None
    if not keep and ep_row.completed_at is not None:
        delete_after = datetime.now(timezone.utc) + timedelta(days=30)
    with get_db() as db:
        db.execute(
            update(Episode)
            .where(Episode.id == ep_row.id)
            .values(
                retention_exempt=keep,
                delete_after=delete_after,
                deleted_at=None,
            )
        )
    return jsonify({
        "status": "ok",
        "completed": ep_row.completed_at is not None,
        "retention_exempt": keep,
        "delete_after": delete_after.isoformat() if delete_after else None,
        "deleted_at": None,
    })


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    from lib.analyzer import LEVELS, DEFAULT_LEVEL

    level = request.form.get("level", DEFAULT_LEVEL).strip()
    if level not in LEVELS:
        level = DEFAULT_LEVEL

    current_user = get_current_user()
    user_id      = current_user.id if current_user else None

    unlimited = bool(current_user and _is_unlimited(current_user))

    source_url = request.form.get("source_url", "").strip()
    job_id     = str(uuid.uuid4())
    base_slug  = date.today().isoformat()
    slug       = _unique_ep_slug(base_slug, user_id=user_id)
    ep_dir     = EPISODES_DIR / slug
    ep_dir.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = None
    meta: dict = {}
    clone_from_id: str | None = None
    clone_from_level: str | None = None

    if source_url:
        own_slug, clone_from_id, clone_from_level = _find_source_clone(source_url, user_id)
        if own_slug:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({"job_id": None, "slug": own_slug})

        title_override = request.form.get("title", "").strip()
        try:
            duration_override = max(0, int(float(request.form.get("duration", "0") or 0)))
        except (TypeError, ValueError):
            duration_override = 0
        meta = {
            "title":       title_override or source_url,
            "channel":     request.form.get("channel", "").strip(),
            "upload_date": date.today().strftime("%Y%m%d"),
            "duration":    duration_override,
            "url":         source_url,
            "thumbnail":   "",
            "description": "",
            "video_id":    "",
            "source":      "url",
            "level":       level,
        }
    else:
        if "audio" not in request.files:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({"error": "No file or URL provided."}), 400
        f = request.files["audio"]
        if not f.filename:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({"error": "No file selected."}), 400
        suffix = Path(f.filename).suffix.lower()
        if suffix not in UPLOAD_EXTENSIONS:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({"error": f"Unsupported format '{suffix}'."}), 400
        title      = request.form.get("title", "").strip() or Path(f.filename).stem
        audio_path = ep_dir / f"audio{suffix}"

        # Bug #2 fix: reject before disk write using Content-Length header.
        if not unlimited:
            cl = request.content_length
            if cl and cl > _TRANSCRIPTION_MAX_BYTES:
                shutil.rmtree(ep_dir, ignore_errors=True)
                return jsonify({
                    "error": "Audio file is too large — only files under 23 MB are supported."
                }), 400

        f.save(audio_path)

        # Definitive size check (catches missing/spoofed Content-Length).
        if not unlimited and audio_path.stat().st_size > _TRANSCRIPTION_MAX_BYTES:
            size_mb = audio_path.stat().st_size // (1024 * 1024)
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({
                "error": f"Audio file is {size_mb} MB — only files under 23 MB are supported."
            }), 400

        meta = {
            "title":             title,
            "channel":           "Upload",
            "upload_date":       date.today().strftime("%Y%m%d"),
            "duration":          0,
            "url":               "",
            "thumbnail":         "",
            "description":       f"Uploaded file: {f.filename}",
            "video_id":          "",
            "source":            "upload",
            "original_filename": f.filename,
            "level":             level,
        }

    # ── Atomic quota check + usage insert ────────────────────────────────────
    usage_id = None
    if not clone_from_id and db_available() and current_user:
        try:
            audio_bytes = audio_path.stat().st_size if audio_path else 0
            usage = _atomic_quota_insert(current_user, audio_bytes=audio_bytes)
            usage_id = str(usage.id)
        except ValueError as qe:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return jsonify({"error": str(qe)}), 429

    if clone_from_id:
        total_steps = 1 if (clone_from_level == level) else 3
    else:
        total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "processing",
            "slug":        slug,
            "user_id":     str(user_id) if user_id else None,
            "step":        "Starting…",
            "step_num":    0,
            "total_steps": total_steps,
            "error":       "",
            "started_at":  time.time(),
            "source_token": _get_source_token(source_url),
        }

    try:
        _persist_processing_job(
            job_id, slug, user_id, source_url, level, total_steps, meta,
            usage_id=usage_id, unlimited=unlimited, clone_from_id=clone_from_id,
        )
    except IntegrityError:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        shutil.rmtree(ep_dir, ignore_errors=True)
        if usage_id:
            with get_db() as db:
                db.execute(update(TranscriptionUsage).where(
                    TranscriptionUsage.id == uuid.UUID(str(usage_id))
                ).values(status="failed"))
        if user_id and _get_source_token(source_url):
            with get_db() as db:
                active = db.execute(select(ProcessingJob).where(
                    ProcessingJob.user_id == user_id,
                    ProcessingJob.source_token == _get_source_token(source_url),
                    ProcessingJob.status.in_(["queued", "running", "retrying"]),
                )).scalar_one_or_none()
            if active:
                return jsonify({"job_id": str(active.id), "slug": active.slug})
        raise

    _prune_old_jobs()
    threading.Thread(
        target=_pipeline_thread,
        args=(job_id, slug, ep_dir, source_url or None, audio_path, meta, level),
        kwargs={
            "user_id": user_id,
            "usage_id": usage_id,
            "unlimited": unlimited,
            "clone_from_id": clone_from_id
        },
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "slug": slug})


@app.route("/api/explain", methods=["POST"])
@login_required
def api_explain():
    from lib.analyzer import explain_sentence, _EXPLAIN_MAX_INPUT_CHARS
    data    = request.get_json(silent=True) or {}
    text    = data.get("text", "").strip()
    # Bug #5 fix: fall back to a per-user global bucket when episode is absent
    # so direct API callers can't bypass the limit by omitting the field.
    episode = data.get("episode", "").strip() or "_global_"
    if not text:
        return jsonify({"error": "No text provided"}), 400
    # Token-burn fix: reject oversized input at the API boundary so the LLM
    # never sees more than a sentence or two regardless of what the client sends.
    if len(text) > _EXPLAIN_MAX_INPUT_CHARS:
        return jsonify({
            "error": f"Text too long ({len(text)} chars). Maximum is {_EXPLAIN_MAX_INPUT_CHARS} characters."
        }), 400

    # Rate-limit: _EXPLAIN_LIMIT calls per user per episode (or per-session for
    # direct API calls).  Resets daily.  Admins / whitelisted users are exempt.
    user = get_current_user()
    if user and db_available() and not _is_unlimited(user):
        uid  = str(user.id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _explain_lock:
            # Bug #5 fix: prune dict if it's grown beyond the user cap.
            if len(_explain_counts) > _EXPLAIN_MAX_USERS:
                _explain_counts.clear()

            bucket = _explain_counts.setdefault(uid, {"day": today, "counts": {}})
            # Daily reset: new calendar day wipes the per-episode counters.
            if bucket["day"] != today:
                bucket["day"]    = today
                bucket["counts"] = {}

            used = bucket["counts"].get(episode, 0)
            if used >= _EXPLAIN_LIMIT:
                return jsonify({
                    "error": f"Explain limit reached ({_EXPLAIN_LIMIT} per episode per day)."
                }), 429
            bucket["counts"][episode] = used + 1

    explanation = explain_sentence(text)
    return jsonify({"explanation": explanation})


@app.route("/api/episodes")
@login_required
def api_episodes():
    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if not user:
            return jsonify([])
        with get_db() as db:
            rows = db.execute(
                select(Episode)
                .where(
                    Episode.owner_user_id == user.id,
                    Episode.deleted_at.is_(None),
                )
                .order_by(Episode.date.desc(), Episode.created_at.desc())
            ).scalars().all()
        return jsonify([
            {
                "date": row.slug,
                "meta": {
                    "title":     row.title,
                    "channel":   row.channel,
                    "url":       row.url,
                    "thumbnail": row.thumbnail,
                    "duration":  row.duration,
                    "level":     row.level,
                    "source":    row.source,
                },
            }
            for row in rows
        ])

    # ── File fallback ─────────────────────────────────────────────────────────
    out = []
    if EPISODES_DIR.exists():
        for ep in sorted(EPISODES_DIR.iterdir(), reverse=True):
            if not ep.is_dir() or not _SLUG_RE.match(ep.name):
                continue
            meta_file = ep / "meta.json"
            meta = (
                json.loads(meta_file.read_text(encoding="utf-8"))
                if meta_file.exists()
                else {}
            )
            out.append({"date": ep.name, "meta": meta})
    return jsonify(out)


@app.route("/api/playback", methods=["POST"])
@login_required
def api_playback_progress():
    """Persist a compact per-user listening signal for recommendation ranking."""
    if not db_available():
        return ("", 204)
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    slug = str(data.get("episode", "")).strip()
    try:
        current = max(0.0, float(data.get("current_time", 0)))
        duration = max(0.0, float(data.get("duration", 0)))
    except (TypeError, ValueError):
        abort(400, "invalid playback values")
    if not user or not slug or duration <= 0:
        abort(400, "episode and positive duration are required")
    percent = min(100, round(current / duration * 100))
    finished = bool(data.get("finished")) or percent >= 95
    with get_db() as db:
        episode_row = db.execute(select(Episode).where(
            Episode.owner_user_id == user.id, Episode.slug == slug
        )).scalar_one_or_none()
        if episode_row is None:
            abort(404)
        row = db.execute(select(PlaybackProgress).where(
            PlaybackProgress.user_id == user.id,
            PlaybackProgress.episode_id == episode_row.id,
        )).scalar_one_or_none()
        if row is None:
            db.add(PlaybackProgress(user_id=user.id, episode_id=episode_row.id,
                                    percent=percent, finished=finished))
        else:
            row.percent = max(row.percent, percent)
            row.finished = row.finished or finished
    return jsonify({"ok": True, "percent": percent, "finished": finished})


# ── Admin ─────────────────────────────────────────────────────────────────────

def _admin_required(f):
    """Decorator: requires the current user to be an admin (is_admin=True)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not db_available():
            abort(503)
        user = get_current_user()
        if user is None:
            return redirect(url_for("login_page"))
        if not user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@_admin_required
def admin_page():
    """Admin dashboard — list all users with quota and cost summary."""
    from web.db import User
    with get_db() as db:
        users = db.execute(
            select(User).order_by(User.created_at)
        ).scalars().all()

        # Jobs used per user (started + completed)
        usage_counts = dict(
            db.execute(
                select(
                    TranscriptionUsage.user_id,
                    func.count().label("cnt"),
                ).where(
                    TranscriptionUsage.status.in_(["started", "completed"])
                ).group_by(TranscriptionUsage.user_id)
            ).all()
        )

        # Total audio bytes processed per user (all statuses)
        audio_totals = dict(
            db.execute(
                select(
                    TranscriptionUsage.user_id,
                    func.sum(TranscriptionUsage.audio_bytes).label("total_bytes"),
                ).group_by(TranscriptionUsage.user_id)
            ).all()
        )

    rows = []
    for u in users:
        used        = usage_counts.get(u.id, 0)
        unlimited   = _is_unlimited(u)
        total_bytes = int(audio_totals.get(u.id) or 0)
        # Whisper cost estimate: assume avg 128 kbps → 16 000 B/s
        # duration_min = bytes / 16000 / 60 ; cost = duration_min * $0.006
        whisper_cost = total_bytes / 16_000 / 60 * 0.006
        rows.append({
            "id":           str(u.id),
            "email":        u.email,
            "is_admin":     u.is_admin,
            "unlimited":    unlimited,
            "limit":        u.transcription_limit,
            "used":         used,
            "joined":       u.created_at.strftime("%Y-%m-%d %H:%M UTC") if u.created_at else "—",
            "audio_mb":     round(total_bytes / 1_048_576, 1),
            "whisper_cost": round(whisper_cost, 4),
        })

    return render_template("admin.html", users=rows)


@app.route("/admin/user/<user_id>/delete", methods=["POST"])
@_admin_required
def admin_delete_user(user_id):
    """Delete a non-admin user and all their data (cascades via FK)."""
    from web.db import User
    with get_db() as db:
        target = db.get(User, user_id)
        if target is None:
            abort(404)
        if target.is_admin:
            # Safety: never delete admin accounts via the UI
            return redirect(url_for("admin_page"))
        db.delete(target)
    log.info("Admin deleted user %s", user_id)
    return redirect(url_for("admin_page"))


@app.route("/admin/user/<user_id>/toggle-unlimited", methods=["POST"])
@_admin_required
def admin_toggle_unlimited(user_id):
    """Toggle a non-admin user between regular (limit=3) and unlimited (limit=-1)."""
    from web.db import User
    _DEFAULT_LIMIT = 3
    with get_db() as db:
        target = db.get(User, user_id)
        if target is None:
            abort(404)
        if target.is_admin:
            return redirect(url_for("admin_page"))
        # If already unlimited (by DB flag or whitelist), demote to regular
        if _is_unlimited(target):
            target.transcription_limit = _DEFAULT_LIMIT
            log.info("Admin set user %s to regular (limit=%d)", target.email, _DEFAULT_LIMIT)
        else:
            target.transcription_limit = -1
            log.info("Admin set user %s to unlimited", target.email)
    return redirect(url_for("admin_page"))


@app.route("/api/admin/user/<user_id>/history")
@_admin_required
def admin_user_history(user_id):
    """Return transcription history for one user (for admin detail modal)."""
    from web.db import User
    with get_db() as db:
        target = db.get(User, user_id)
        if target is None:
            abort(404)

        rows = db.execute(
            select(TranscriptionUsage, Episode)
            .outerjoin(Episode, TranscriptionUsage.episode_id == Episode.id)
            .where(TranscriptionUsage.user_id == user_id)
            .order_by(TranscriptionUsage.created_at.desc())
        ).all()

    history = []
    for usage, ep in rows:
        ab = usage.audio_bytes or 0
        ep_duration = ep.duration if ep else 0   # seconds from Episode row
        # Use real episode duration when available, else estimate from bytes
        if ep_duration and ep_duration > 0:
            duration_min = ep_duration / 60
        else:
            duration_min = ab / 16_000 / 60      # fallback estimate
        cost = round(duration_min * 0.006, 4)
        history.append({
            "id":             str(usage.id),
            "status":         usage.status,
            "created_at":     usage.created_at.strftime("%Y-%m-%d %H:%M UTC") if usage.created_at else "—",
            "audio_mb":       round(ab / 1_048_576, 2),
            "whisper_cost":   cost,
            "duration_min":   round(duration_min, 1),
            "episode_title":  ep.title  if ep else "—",
            "episode_slug":   ep.slug   if ep else "",
            "episode_level":  ep.level  if ep else "",
            "episode_source": ep.source if ep else "",
        })

    return jsonify({"email": target.email, "history": history})


if __name__ == "__main__":
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug="--debug" in sys.argv)
