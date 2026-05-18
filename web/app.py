"""Flask web UI for the Japanese Learning Pipeline — localhost:5000."""
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
import uuid
from datetime import date
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

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

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

UPLOAD_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".aac", ".opus"}

# Slug pattern: YYYY-MM-DD or YYYY-MM-DD-N
_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")

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
from web.db import Episode, VocabItem, db_available, get_db, init_db
from sqlalchemy import select

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
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "dev-insecure-change-me-before-deploy"
)

# Connect to Postgres and ensure all tables exist.
# Silently skips if DATABASE_URL is not set (local dev without Postgres).
init_db()


@app.context_processor
def _inject_auth():
    """Make current_user available in every Jinja template automatically."""
    return {"current_user": get_current_user(), "db_available": db_available()}


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
        _, token = register_user(email, password)
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
        user, token = register_user(email, password)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"token": token, "user": {"id": str(user.id), "email": user.email, "is_admin": user.is_admin}})


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
    return jsonify({"token": token, "user": {"id": str(user.id), "email": user.email, "is_admin": user.is_admin}})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    token = _extract_bearer()
    logout_token(token)
    return jsonify({"ok": True})


@app.route("/api/auth/me")
@login_required
def api_auth_me():
    user = get_current_user()
    return jsonify({
        "id":       str(user.id),
        "email":    user.email,
        "is_admin": user.is_admin,
        "transcription_limit": user.transcription_limit,
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


def _r2_find_audio(r2_prefix: str) -> "tuple[str, str] | None":
    """
    Locate the audio file under r2_prefix.
    Returns (key, mimetype) or None.
    Uses list_objects_v2 so we don't have to guess the extension.
    """
    resp = _get_r2().list_objects_v2(
        Bucket=_r2_bucket(), Prefix=r2_prefix + "audio"
    )
    for obj in resp.get("Contents", []):
        name = Path(obj["Key"]).name
        if name.startswith("audio."):
            mt = mimetypes.guess_type(name)[0] or "audio/mpeg"
            return obj["Key"], mt
    return None


def _r2_upload_episode(ep_dir: Path, r2_prefix: str) -> None:
    """Upload all known episode files from ep_dir to R2 at r2_prefix."""
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
        try:
            s3.upload_file(str(fpath), bucket, key, ExtraArgs={"ContentType": mt})
            log.info(f"R2 uploaded: {key}")
        except Exception as exc:
            log.error(f"R2 upload failed for {key}: {exc}")


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
) -> None:
    """Runs the full pipeline in a background thread."""
    from lib.transcriber import transcribe_audio
    from lib.translator import translate_segments
    from lib.analyzer import analyze_transcript
    from lib.writer import write_episode_files

    total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    try:
        # ── Step 1: Download ────────────────────────────────────────────────
        if source_url and audio_path is None:
            from lib.downloader import download_latest
            _set_step(job_id, "Downloading audio…", 1)
            audio_path, meta = download_latest([source_url], ep_dir)
            if not audio_path:
                raise RuntimeError("Could not download audio — check the URL")

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        _set_step(job_id, "Transcribing with Whisper…", 2)
        whisper_result = transcribe_audio(audio_path)

        # ── Step 3: Translate ────────────────────────────────────────────────
        _set_step(job_id, "Translating EN + ZH with Gemini…", 3)
        segments = translate_segments(whisper_result["segments"])

        # ── Step 4: Analyse ──────────────────────────────────────────────────
        _set_step(job_id, "Analysing vocabulary and grammar…", 4)
        analysis = analyze_transcript(segments, level=level)

        # ── Step 5: Write files ──────────────────────────────────────────────
        _set_step(job_id, "Writing episode files…", 5)
        write_episode_files(ep_dir, meta, segments, analysis, whisper_result)

        # ── Step 6: Upload to R2 + persist Episode row ───────────────────────
        if user_id and db_available() and _get_r2():
            _set_step(job_id, "Saving to cloud storage…", 6)
            r2_prefix = f"episodes/{user_id}/{slug}/"
            _r2_upload_episode(ep_dir, r2_prefix)

            with get_db() as db:
                existing = db.execute(
                    select(Episode).where(
                        Episode.owner_user_id == user_id,
                        Episode.slug == slug,
                    )
                ).scalar_one_or_none()
                if not existing:
                    # re-read meta (download may have enriched it)
                    meta_path = ep_dir / "meta.json"
                    if meta_path.exists():
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
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
                        r2_prefix     = r2_prefix,
                    )
                    db.add(ep_row)
                else:
                    existing.r2_prefix = r2_prefix

        with _jobs_lock:
            _jobs[job_id]["status"]   = "done"
            _jobs[job_id]["step"]     = "Complete"
            _jobs[job_id]["step_num"] = total_steps

    except Exception as exc:
        tb = traceback.format_exc()
        log.error(f"Job {job_id} failed:\n{tb}")
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


def _make_response_cached(response):
    """Add cache headers to a response for episode data that may be re-generated."""
    response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    return response


# ── Pages ─────────────────────────────────────────────────────────────────────

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
        return render_template("index.html", episodes=episodes)

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
    return render_template("index.html", episodes=episodes)


@app.route("/episode/<date_str>")
@login_required
def episode(date_str: str):
    # ── DB path ───────────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        meta = {
            "title":     ep_row.title,
            "channel":   ep_row.channel,
            "url":       ep_row.url,
            "thumbnail": ep_row.thumbnail,
            "duration":  ep_row.duration,
            "level":     ep_row.level,
            "source":    ep_row.source,
        }
        return render_template("episode.html", date=date_str, meta=meta)

    # ── File fallback ─────────────────────────────────────────────────────────
    ep = _ep_dir(date_str)
    meta_file = ep / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    return render_template("episode.html", date=date_str, meta=meta)


@app.route("/episode/<date_str>/delete", methods=["POST"])
@login_required
def episode_delete(date_str: str):
    # ── DB path ───────────────────────────────────────────────────────────────
    ep_row = _lookup_episode(date_str)
    if ep_row is not None:
        if ep_row.r2_prefix and _get_r2():
            # Delete all objects under the prefix
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


@app.route("/subscriptions", methods=["GET"])
@login_required
def subscriptions_page():
    sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
    return render_template("subscriptions.html", sources=sources_data.get("sources", []))


@app.route("/subscriptions/add", methods=["POST"])
@login_required
def subscriptions_add():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    desc = request.form.get("description", "").strip()

    if not name or not url:
        return render_template("subscriptions.html", error="Name and URL are required.",
                               sources=json.loads(SOURCES_FILE.read_text(encoding="utf-8")).get("sources", []))

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


@app.route("/upload", methods=["GET"])
@login_required
def upload_page():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
@login_required
def upload_process():
    from lib.analyzer import LEVELS, DEFAULT_LEVEL

    level = request.form.get("level", DEFAULT_LEVEL).strip()
    if level not in LEVELS:
        level = DEFAULT_LEVEL

    current_user = get_current_user()
    user_id      = current_user.id if current_user else None

    source_url  = request.form.get("source_url", "").strip()
    job_id      = str(uuid.uuid4())
    base_slug   = date.today().isoformat()
    slug        = _unique_ep_slug(base_slug, user_id=user_id)
    ep_dir      = EPISODES_DIR / slug
    ep_dir.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = None
    meta: dict = {}

    if source_url:
        # URL path — download happens inside the thread
        title_override = request.form.get("title", "").strip()
        meta = {
            "title":       title_override or source_url,
            "channel":     "",
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
        # File upload — save synchronously, process in thread
        if "audio" not in request.files:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template("upload.html", error="No file or URL provided."), 400

        f = request.files["audio"]
        if not f.filename:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template("upload.html", error="No file selected."), 400

        suffix = Path(f.filename).suffix.lower()
        if suffix not in UPLOAD_EXTENSIONS:
            shutil.rmtree(ep_dir, ignore_errors=True)
            return render_template(
                "upload.html",
                error=f"Unsupported format '{suffix}'. Accepted: {', '.join(sorted(UPLOAD_EXTENSIONS))}",
            ), 400

        title      = request.form.get("title", "").strip() or Path(f.filename).stem
        audio_path = ep_dir / f"audio{suffix}"
        f.save(audio_path)

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

    total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    # Register job and start background thread
    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "processing",
            "slug":        slug,
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
        kwargs={"user_id": user_id},
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
        if ep_row.r2_prefix and _get_r2():
            return redirect(_r2_presigned(f"{ep_row.r2_prefix}cards.csv"))
        abort(404)

    # ── File fallback ─────────────────────────────────────────────────────────
    csv_file = _ep_dir(date_str) / "cards.csv"
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
    if ep_row is not None:
        if ep_row.r2_prefix and _get_r2():
            try:
                data = _r2_get_json(f"{ep_row.r2_prefix}{filename}")
                return _make_response_cached(jsonify(data))
            except Exception as exc:
                log.error(f"R2 fetch failed for {ep_row.r2_prefix}{filename}: {exc}")
                abort(404)
        abort(404)
    # File fallback
    return _make_response_cached(jsonify(_read_json(_ep_dir(date_str) / filename)))


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

    source_url = request.form.get("source_url", "").strip()
    job_id     = str(uuid.uuid4())
    base_slug  = date.today().isoformat()
    slug       = _unique_ep_slug(base_slug, user_id=user_id)
    ep_dir     = EPISODES_DIR / slug
    ep_dir.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = None
    meta: dict = {}

    if source_url:
        title_override = request.form.get("title", "").strip()
        meta = {
            "title":       title_override or source_url,
            "channel":     "",
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
        f.save(audio_path)
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

    total_steps = 6 if (user_id and db_available() and _get_r2()) else 5

    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "processing",
            "slug":        slug,
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
        kwargs={"user_id": user_id},
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "slug": slug})


@app.route("/api/explain", methods=["POST"])
@login_required
def api_explain():
    from lib.analyzer import explain_sentence
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
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


if __name__ == "__main__":
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug="--debug" in sys.argv)
