"""Flask web UI for the Japanese Learning Pipeline — localhost:5000."""
import json
import logging
import mimetypes
import os
import shutil
import sys
import threading
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

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

# Make lib/ importable when running web/app.py directly
sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_ROOT = Path(__file__).parent.parent
_episodes_env = os.environ.get("EPISODES_DIR", "")
EPISODES_DIR = (Path(_episodes_env) if Path(_episodes_env).is_absolute()
                else _PROJECT_ROOT / (_episodes_env or "episodes"))

UPLOAD_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".aac", ".opus"}

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# ── Background job tracking ───────────────────────────────────────────────────

# job_id → {status, slug, step, error}
# status: "processing" | "done" | "error"
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_step(job_id: str, step: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["step"] = step


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

    try:
        # ── Step 1: Download (URL path only) ────────────────────────────────
        if source_url and audio_path is None:
            from lib.downloader import download_latest
            _set_step(job_id, "Downloading audio…")
            audio_path, meta = download_latest([source_url], ep_dir)
            if not audio_path:
                raise RuntimeError("Could not download audio — check the URL")

        # ── Step 2: Transcribe ───────────────────────────────────────────────
        _set_step(job_id, "Transcribing with Whisper…")
        whisper_result = transcribe_audio(audio_path)

        # ── Step 3: Translate ────────────────────────────────────────────────
        _set_step(job_id, "Translating EN + ZH with Gemini…")
        segments = translate_segments(whisper_result["segments"])

        # ── Step 4: Analyse ──────────────────────────────────────────────────
        _set_step(job_id, "Analysing vocabulary and grammar…")
        analysis = analyze_transcript(segments, level=level)

        # ── Step 5: Write files ──────────────────────────────────────────────
        _set_step(job_id, "Writing episode files…")
        write_episode_files(ep_dir, meta, segments, analysis, whisper_result)

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["step"]   = "Complete"

    except Exception as exc:
        tb = traceback.format_exc()
        log.error(f"Job {job_id} failed:\n{tb}")
        shutil.rmtree(ep_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"]  = str(exc)
            _jobs[job_id]["step"]   = "Failed"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ep_dir(date_str: str) -> Path:
    ep = EPISODES_DIR / date_str
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


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    episodes = []
    if EPISODES_DIR.exists():
        for ep in sorted(EPISODES_DIR.iterdir(), reverse=True):
            if not ep.is_dir():
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


@app.route("/episode/<date_str>/retranslate", methods=["POST"])
def episode_retranslate(date_str: str):
    ep = _ep_dir(date_str)
    transcript_file = ep / "transcript.json"
    if not transcript_file.exists():
        return jsonify({"error": "No transcript.json found — run the pipeline first"}), 404

    try:
        from lib.translator import translate_segments
        from lib.writer import _write_vtt

        data = json.loads(transcript_file.read_text(encoding="utf-8"))
        new_segments = translate_segments(data["segments"])

        updated = {"segments": new_segments}
        transcript_file.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_vtt(ep / "subtitles.vtt", new_segments)

        log.info(f"Re-translated {len(new_segments)} segments for {date_str}")
        return jsonify({"ok": True, "segments": new_segments})
    except Exception as exc:
        tb = traceback.format_exc()
        log.error(f"Re-translation failed for {date_str}:\n{tb}")
        return jsonify({"error": str(exc), "traceback": tb}), 500


@app.route("/episode/<date_str>/delete", methods=["POST"])
def episode_delete(date_str: str):
    ep = _ep_dir(date_str)
    shutil.rmtree(ep)
    log.info(f"Deleted episode {date_str}")
    return redirect(url_for("index"))


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
            "error":  "",
        }

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
        "status": job["status"],
        "step":   job.get("step", ""),
        "slug":   job["slug"],
        "error":  job.get("error", ""),
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
    return send_file(vtt, mimetype="text/vtt")


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
    return jsonify(_read_json(_ep_dir(date_str) / "meta.json"))


@app.route("/api/episode/<date_str>/transcript")
def api_transcript(date_str: str):
    return jsonify(_read_json(_ep_dir(date_str) / "transcript.json"))


@app.route("/api/episode/<date_str>/analysis")
def api_analysis(date_str: str):
    return jsonify(_read_json(_ep_dir(date_str) / "analysis.json"))


@app.route("/api/episodes")
def api_episodes():
    out = []
    if EPISODES_DIR.exists():
        for ep in sorted(EPISODES_DIR.iterdir(), reverse=True):
            if ep.is_dir():
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
