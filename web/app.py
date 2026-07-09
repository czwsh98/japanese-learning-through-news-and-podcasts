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
from datetime import date, datetime, timezone
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
RECENT_EPISODES_CACHE_FILE = _PROJECT_ROOT / "recent_episodes_cache.json"

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
from web.db import Episode, TranscriptionUsage, VocabItem, db_available, get_db, init_db
from sqlalchemy import func, select, text as sa_text

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
            uid_hash = hash(str(user.id)) & 0x7FFFFFFFFFFFFFFF  # positive int64
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
                _jobs[job_id]["status"]      = "done"
                _jobs[job_id]["step"]        = "Complete"
                _jobs[job_id]["step_num"]    = done_step
                _jobs[job_id]["total_steps"] = done_step
            return

        except Exception as exc:
            tb = traceback.format_exc()
            log.error(f"Job {job_id} fast-path failed:\n{tb}")
            shutil.rmtree(ep_dir, ignore_errors=True)
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"]  = str(exc)
                _jobs[job_id]["step"]   = "Failed"
            return

    total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    def _mark_usage(status: str) -> None:
        if usage_id and db_available():
            try:
                with get_db() as db:
                    row = db.get(TranscriptionUsage, usage_id)
                    if row:
                        row.status = status
                        if audio_path and audio_path.exists():
                            row.audio_bytes = audio_path.stat().st_size
            except Exception as e:
                log.warning(f"Could not update TranscriptionUsage {usage_id}: {e}")

    try:
        # ── Step 1: Download ────────────────────────────────────────────────
        if source_url and audio_path is None:
            from lib.downloader import download_latest
            _set_step(job_id, "Downloading audio…", 1)
            _ovr_title   = (meta or {}).get("title", "")
            _ovr_channel = (meta or {}).get("channel", "")
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

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        _set_step(job_id, "Transcribing with Whisper…", 2)
        whisper_result = transcribe_audio(audio_path)

        # ── Step 3: Translate ────────────────────────────────────────────────
        _set_step(job_id, "Translating EN + ZH with Gemini…", 3)
        segments = translate_segments(whisper_result["segments"])
        segments = tokenize_segments(segments)

        # ── Step 4: Analyse ──────────────────────────────────────────────────
        _set_step(job_id, "Analysing vocabulary and grammar…", 4)
        analysis = analyze_transcript(segments, level=level)

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

        _mark_usage("completed")

        # Bug #4 fix: after a successful R2 upload the local ep_dir is no longer
        # needed (canonical copy is in R2).  Remove it to prevent unbounded disk
        # growth on long-lived workers.  When R2 is not configured we keep the
        # local files (they are the only copy).
        if r2_prefix:
            shutil.rmtree(ep_dir, ignore_errors=True)
            log.info(f"Cleaned up local ep_dir after R2 upload: {ep_dir}")

        with _jobs_lock:
            _jobs[job_id]["status"]   = "done"
            _jobs[job_id]["step"]     = "Complete"
            _jobs[job_id]["step_num"] = total_steps

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
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"]  = str(exc)
            _jobs[job_id]["step"]   = "Failed"


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
        normalized_url = urllib.parse.urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            "", "", ""
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
            while db.execute(
                select(Episode).where(
                    Episode.owner_user_id == user_id,
                    Episode.slug == slug,
                )
            ).scalar_one_or_none() is not None:
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
            )
        ).scalars().first()
        if own:
            return own.slug, None, None
        other = db.execute(
            select(Episode).where(
                Episode.source_token == source_token,
                Episode.r2_prefix    != "",
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


@app.route("/")
@login_required
def index():
    episodes = []

    # ── DB path ───────────────────────────────────────────────────────────────
    if db_available():
        user = get_current_user()
        if user:
            with get_db() as db:
                rows = db.execute(
                    select(Episode)
                    .where(Episode.owner_user_id == user.id)
                    .order_by(Episode.date.desc(), Episode.created_at.desc())
                ).scalars().all()
            for row in rows:
                episodes.append({
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
                    "has_audio":      bool(row.r2_prefix),
                    "has_transcript": bool(row.r2_prefix),
                })
        groups, group_mode = _grouped(episodes)
        return render_template("index.html", episodes=episodes, groups=groups, group_mode=group_mode)

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
    return render_template("index.html", episodes=episodes, groups=groups, group_mode=group_mode)


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


@app.route("/episode/<date_str>/delete", methods=["POST"])
@login_required
def episode_delete(date_str: str):
    # ── DB path ───────────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        if ep_row.r2_prefix and _get_r2():
            # Only delete R2 objects if no other Episode row shares this prefix
            # (cross-user clones point at the same r2_prefix — deleting would break them).
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
        log.info(f"Deleted episode {date_str} from DB/R2")
        return redirect(url_for("index"))

    # ── File fallback ─────────────────────────────────────────────────────────
    ep = _ep_dir(date_str)
    shutil.rmtree(ep)
    log.info(f"Deleted episode {date_str} from filesystem")
    return redirect(url_for("index"))


def _load_recent_cache() -> dict:
    """Recent-episodes-per-source map written by the bot; {} if absent/broken."""
    try:
        if RECENT_EPISODES_CACHE_FILE.exists():
            return json.loads(RECENT_EPISODES_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


@app.route("/subscriptions", methods=["GET"])
@login_required
def subscriptions_page():
    sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
    return render_template("subscriptions.html", sources=sources_data.get("sources", []),
                           recent_cache=_load_recent_cache())


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
            episodes.append({
                "title":       str(ep.get("title", ""))[:300],
                "description": str(ep.get("description", ""))[:400],
            })
        clean[str(url)] = {
            "fetched_at": str(entry.get("fetched_at", ""))[:32],
            "episodes":   episodes,
        }

    with _recent_lock:
        RECENT_EPISODES_CACHE_FILE.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "sources": len(clean)})


@app.route("/subscriptions/add", methods=["POST"])
@login_required
def subscriptions_add():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    desc = request.form.get("description", "").strip()

    if not name or not url:
        return render_template("subscriptions.html", error="Name and URL are required.",
                               sources=json.loads(SOURCES_FILE.read_text(encoding="utf-8")).get("sources", []),
                               recent_cache=_load_recent_cache())

    with _sources_lock:
        sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
        if "sources" not in sources_data: sources_data["sources"] = []
        sources_data["sources"].append({"name": name, "url": url, "description": desc})
        SOURCES_FILE.write_text(json.dumps(sources_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return redirect(url_for("subscriptions_page"))


@app.route("/subscriptions/delete", methods=["POST"])
@login_required
def subscriptions_delete():
    url = request.form.get("url", "").strip()
    if not url:
        abort(400)

    with _sources_lock:
        sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
        sources_data["sources"] = [s for s in sources_data.get("sources", []) if s["url"] != url]
        SOURCES_FILE.write_text(json.dumps(sources_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return redirect(url_for("subscriptions_page"))


@app.route("/vocab")
@login_required
def vocab_page():
    return render_template("vocab.html")


def _vocab_item_to_dict(row: VocabItem) -> dict:
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
    }


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
        items = [_vocab_item_to_dict(r) for r in rows]
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
            if existing:
                return jsonify({"status": "exists", "id": str(existing.id)}), 200

            row = VocabItem(
                user_id        = user.id,
                word           = word,
                reading        = new_item.get("reading", ""),
                en             = new_item.get("en", ""),
                zh             = new_item.get("zh", ""),
                example        = new_item.get("example", ""),
                level          = new_item.get("level", ""),
                type           = new_item.get("type", "vocab"),
                source_episode = new_item.get("source_episode", ""),
            )
            db.add(row)
        return jsonify({"status": "success", "id": str(row.id)}), 201

    # ── File fallback ─────────────────────────────────────────────────────────
    with _vocab_lock:
        data = json.loads(VOCAB_FILE.read_text(encoding="utf-8")) if VOCAB_FILE.exists() else {"items": []}
        items = data.get("items", [])

        if any(i.get("word") == word for i in items):
            return jsonify({"status": "exists"}), 200

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
        }

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

@app.route("/job/<job_id>")
@login_required
def job_page(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return render_template("job.html", job_id=job_id, slug=job["slug"])


@app.route("/api/job/<job_id>/status")
@login_required
def api_job_status(job_id: str):
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
        }

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
                .where(Episode.owner_user_id == user.id)
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
