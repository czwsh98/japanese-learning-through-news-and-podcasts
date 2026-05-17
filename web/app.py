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

from web.db import db_available, init_db

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
) -> None:
    """Runs the full pipeline in a background thread."""
    from lib.transcriber import transcribe_audio
    from lib.translator import translate_segments
    from lib.analyzer import analyze_transcript
    from lib.writer import write_episode_files

    total_steps = 5

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

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["step"]   = "Complete"
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


def _unique_ep_slug(base: str) -> str:
    """Return a slug that doesn't already have a processed transcript."""
    slug = base
    counter = 2
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
def index():
    episodes = []
    if EPISODES_DIR.exists():
        for ep in sorted(EPISODES_DIR.iterdir(), reverse=True):
            if not ep.is_dir() or not _SLUG_RE.match(ep.name):
                continue
            meta_file = ep / "meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            has_audio = _find_audio(ep) is not None
            has_transcript = (ep / "transcript.json").exists()
            episodes.append(
                {
                    "date": ep.name,
                    "meta": meta,
                    "has_audio": has_audio,
                    "has_transcript": has_transcript,
                }
            )
    return render_template("index.html", episodes=episodes)


@app.route("/episode/<date_str>")
def episode(date_str: str):
    ep = _ep_dir(date_str)
    meta_file = ep / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    return render_template("episode.html", date=date_str, meta=meta)



@app.route("/episode/<date_str>/delete", methods=["POST"])
def episode_delete(date_str: str):
    ep = _ep_dir(date_str)
    shutil.rmtree(ep)
    log.info(f"Deleted episode {date_str}")
    return redirect(url_for("index"))


@app.route("/subscriptions", methods=["GET"])
def subscriptions_page():
    sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {"sources": []}
    return render_template("subscriptions.html", sources=sources_data.get("sources", []))


@app.route("/subscriptions/add", methods=["POST"])
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
def vocab_page():
    return render_template("vocab.html")


@app.route("/api/vocab", methods=["GET"])
def api_vocab_get():
    with _vocab_lock:
        data = json.loads(VOCAB_FILE.read_text(encoding="utf-8")) if VOCAB_FILE.exists() else {"items": []}
    items = data.get("items", [])
    
    q = request.args.get("q", "").lower().strip()
    level = request.args.get("level", "").lower().strip()
    vtype = request.args.get("type", "").lower().strip()
    
    if q:
        items = [i for i in items if q in i.get("word", "").lower() or q in i.get("reading", "").lower() or q in i.get("en", "").lower() or q in i.get("zh", "").lower()]
    if level and level != "all":
        items = [i for i in items if i.get("level", "").lower() == level]
    if vtype and vtype != "all":
        items = [i for i in items if i.get("type", "").lower() == vtype]
        
    return jsonify(items)


@app.route("/api/vocab", methods=["POST"])
def api_vocab_add():
    new_item = request.json
    if not new_item or not new_item.get("front"):
        return jsonify({"error": "Invalid data"}), 400

    word = new_item["front"]

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
def api_vocab_delete(item_id):
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
def vocab_export():
    if not VOCAB_FILE.exists():
        return "No vocab found", 404
        
    data = json.loads(VOCAB_FILE.read_text(encoding="utf-8"))
    items = data.get("items", [])
    
    import csv
    import io
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
            i.get("type", "")
        ])
        
    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)
    output.close()
    
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"vocab-export-{time.strftime('%Y%m%d')}.csv"
    )


@app.route("/upload", methods=["GET"])
def upload_page():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
def upload_process():
    from lib.analyzer import LEVELS, DEFAULT_LEVEL

    level = request.form.get("level", DEFAULT_LEVEL).strip()
    if level not in LEVELS:
        level = DEFAULT_LEVEL

    source_url  = request.form.get("source_url", "").strip()
    job_id      = str(uuid.uuid4())
    base_slug   = date.today().isoformat()
    slug        = _unique_ep_slug(base_slug)
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

    # Register job and start background thread
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "processing",
            "slug":   slug,
            "step":   "Starting…",
            "step_num": 0,
            "total_steps": 5,
            "error":  "",
            "started_at": time.time(),
        }

    _prune_old_jobs()

    t = threading.Thread(
        target=_pipeline_thread,
        args=(job_id, slug, ep_dir, source_url or None, audio_path, meta, level),
        daemon=True,
    )
    t.start()
    log.info(f"Started job {job_id} for slug {slug!r}")

    return redirect(url_for("job_page", job_id=job_id))


# ── Job status ────────────────────────────────────────────────────────────────

@app.route("/job/<job_id>")
def job_page(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return render_template("job.html", job_id=job_id, slug=job["slug"])


@app.route("/api/job/<job_id>/status")
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
def episode_audio(date_str: str):
    audio = _find_audio(_ep_dir(date_str))
    if not audio:
        abort(404)
    mimetype = mimetypes.guess_type(audio.name)[0] or "audio/mpeg"
    return send_file(audio, mimetype=mimetype, conditional=True)


@app.route("/episode/<date_str>/subtitles.vtt")
def episode_vtt(date_str: str):
    vtt = _ep_dir(date_str) / "subtitles.vtt"
    if not vtt.exists():
        abort(404)
    return _make_response_cached(send_file(vtt, mimetype="text/vtt"))


@app.route("/episode/<date_str>/cards.csv")
def episode_cards(date_str: str):
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

@app.route("/api/episode/<date_str>/meta")
def api_meta(date_str: str):
    resp = jsonify(_read_json(_ep_dir(date_str) / "meta.json"))
    return _make_response_cached(resp)


@app.route("/api/episode/<date_str>/transcript")
def api_transcript(date_str: str):
    resp = jsonify(_read_json(_ep_dir(date_str) / "transcript.json"))
    return _make_response_cached(resp)


@app.route("/api/episode/<date_str>/analysis")
def api_analysis(date_str: str):
    resp = jsonify(_read_json(_ep_dir(date_str) / "analysis.json"))
    return _make_response_cached(resp)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    from lib.analyzer import LEVELS, DEFAULT_LEVEL

    level = request.form.get("level", DEFAULT_LEVEL).strip()
    if level not in LEVELS:
        level = DEFAULT_LEVEL

    source_url = request.form.get("source_url", "").strip()
    job_id     = str(uuid.uuid4())
    base_slug  = date.today().isoformat()
    slug       = _unique_ep_slug(base_slug)
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

    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "processing",
            "slug":        slug,
            "step":        "Starting…",
            "step_num":    0,
            "total_steps": 5,
            "error":       "",
            "started_at":  time.time(),
        }

    _prune_old_jobs()
    threading.Thread(
        target=_pipeline_thread,
        args=(job_id, slug, ep_dir, source_url or None, audio_path, meta, level),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "slug": slug})


@app.route("/api/explain", methods=["POST"])
def api_explain():
    from lib.analyzer import explain_sentence
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400
    explanation = explain_sentence(text)
    return jsonify({"explanation": explanation})


@app.route("/api/episodes")
def api_episodes():
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
