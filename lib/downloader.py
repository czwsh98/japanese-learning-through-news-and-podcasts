"""Download episode audio from a yt-dlp source URL or direct audio link.

Supported inputs
----------------
- YouTube, NHK, SoundCloud, Apple Podcasts, and anything else yt-dlp handles
- RSS/Atom podcast feed URLs (handled by yt-dlp)
- Direct audio file URLs (streamed with requests)
"""
import base64
import json
import logging
import os
import subprocess
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

import requests as _req

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 600))
_DIRECT_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus", ".webm"}

# ── YouTube cookies ───────────────────────────────────────────────────────────

_cookies_file: str | None = None


def _get_cookies_file() -> str | None:
    """Decode YOUTUBE_COOKIES_B64 into a temp file on first call; return path."""
    global _cookies_file
    if _cookies_file:
        return _cookies_file
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        content = base64.b64decode(b64).decode("utf-8")
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        _cookies_file = path
        log.info("YouTube cookies loaded from YOUTUBE_COOKIES_B64")
    except Exception as exc:
        log.error(f"Failed to decode YOUTUBE_COOKIES_B64: {exc}")
        return None
    return _cookies_file


def _is_direct_audio(url: str) -> bool:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext in _DIRECT_AUDIO_EXTS


# ── Direct audio download ─────────────────────────────────────────────────────

def _download_direct(audio_url: str, episode_dir: Path, meta: dict) -> tuple[Path, dict]:
    """Stream a direct audio URL to episode_dir/audio.<ext>."""
    ext = Path(urllib.parse.urlparse(audio_url).path).suffix.lower()
    if ext not in _DIRECT_AUDIO_EXTS:
        ext = ".mp3"
    audio_path = episode_dir / f"audio{ext}"

    if audio_path.exists():
        log.info(f"Audio already present ({audio_path.name}), skipping download")
        return audio_path, meta

    log.info(f"Streaming audio from {audio_url[:80]}…")
    with _req.get(audio_url, stream=True, timeout=_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(audio_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65_536):
                fh.write(chunk)

    size_kb = audio_path.stat().st_size // 1024
    log.info(f"Downloaded {size_kb:,} KB → {audio_path}")
    return audio_path, meta


# ── yt-dlp download ───────────────────────────────────────────────────────────

def _download_ytdlp(url: str, episode_dir: Path) -> tuple[Path, dict]:
    """Use yt-dlp to download the first item from *url*."""
    audio_path = episode_dir / "audio.mp3"

    cookies = _get_cookies_file()
    log.info(f"yt-dlp cookies: {'loaded' if cookies else 'not set'}")

    cookie_args = ["--cookies", cookies] if cookies else []

    # Fetch metadata
    info_cmd = [
        "yt-dlp", "--playlist-items", "1",
        "--dump-json", "--no-warnings", "--quiet",
        *cookie_args,
        url,
    ]
    result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=min(60, _TIMEOUT))
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {result.stderr.strip()}")

    info = json.loads(result.stdout.strip().splitlines()[0])
    meta = _extract_meta(info)

    if audio_path.exists():
        log.info(f"Audio already present for {meta['title']!r}, skipping download")
        return audio_path, meta

    dl_cmd = [
        "yt-dlp", "--playlist-items", "1",
        "-x", "--audio-format", "mp3", "--audio-quality", "192",
        "--no-warnings", "--quiet",
        "-o", str(episode_dir / "audio.%(ext)s"),
        *cookie_args,
        url,
    ]
    log.info(f"Downloading {meta['title']!r} from {url}")
    result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {result.stderr.strip()}")

    if not audio_path.exists():
        raise RuntimeError("Download finished but audio.mp3 not found")

    size_kb = audio_path.stat().st_size // 1024
    log.info(f"Downloaded {size_kb:,} KB → {audio_path}")
    return audio_path, meta


# ── Public API ────────────────────────────────────────────────────────────────

def download_latest(
    urls: list[str], episode_dir: Path, dry_run: bool = False
) -> tuple[Path | None, dict]:
    """Try each URL in order; return first successful (audio_path, meta)."""
    for url in urls:
        try:
            audio_path, meta = _download(url, episode_dir, dry_run)
            if audio_path:
                return audio_path, meta
        except Exception as exc:
            log.error(f"Download failed for {url}: {exc}")
    return None, {}


def _download(url: str, episode_dir: Path, dry_run: bool) -> tuple[Path | None, dict]:
    if dry_run:
        log.info(f"[DRY RUN] Would download from {url}")
        p = episode_dir / "audio.mp3"
        p.touch()
        return p, _stub_meta()

    # ── Direct audio file URL ────────────────────────────────────────────────
    if _is_direct_audio(url):
        meta = {"title": Path(urllib.parse.urlparse(url).path).stem,
                "channel": "", "upload_date": date.today().strftime("%Y%m%d"),
                "duration": 0, "url": url, "thumbnail": "", "description": "",
                "video_id": ""}
        return _download_direct(url, episode_dir, meta)

    # ── yt-dlp (YouTube, RSS feeds, SoundCloud, NHK, …) ─────────────────────
    return _download_ytdlp(url, episode_dir)


# ── Meta helpers ──────────────────────────────────────────────────────────────

def _extract_meta(info: dict) -> dict:
    return {
        "title":       info.get("title", ""),
        "channel":     info.get("channel") or info.get("uploader", ""),
        "upload_date": info.get("upload_date", ""),
        "duration":    info.get("duration", 0),
        "url":         info.get("webpage_url", ""),
        "thumbnail":   info.get("thumbnail", ""),
        "description": (info.get("description") or "")[:500],
        "video_id":    info.get("id", ""),
    }


def _stub_meta() -> dict:
    return {
        "title":       "Dry-run episode",
        "channel":     "Test Channel",
        "upload_date": date.today().strftime("%Y%m%d"),
        "duration":    120,
        "url":         "",
        "thumbnail":   "",
        "description": "",
        "video_id":    "dry-run",
    }
