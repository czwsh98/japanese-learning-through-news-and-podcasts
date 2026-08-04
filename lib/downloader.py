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
import re
import subprocess
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

import requests as _req

from lib.url_safety import DownloadTooLargeError, safe_download, validate_public_url

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 600))
_MAX_REMOTE_BYTES = 512 * 1024 * 1024
_DIRECT_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus", ".webm"}

# ── VPS download proxy ────────────────────────────────────────────────────────

_VPS_URL   = os.environ.get("VPS_DOWNLOAD_URL", "").rstrip("/")
_VPS_TOKEN = os.environ.get("VPS_DOWNLOAD_TOKEN", "")
_YT_RE     = re.compile(r'(?:watch\?.*v=|youtu\.be/)([a-zA-Z0-9_-]{11})')

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


# ── Apple Podcasts episode resolution ─────────────────────────────────────────
# yt-dlp's ApplePodcasts extractor scrapes the podcasts.apple.com webpage,
# which intermittently 500s (an Apple-side issue, observed across locales and
# networks — not something we can fix). The iTunes Lookup API is a separate,
# more reliable backend that resolves the same episode straight to its real
# audio file URL, so we prefer it whenever the source URL is an Apple
# Podcasts page.

_APPLE_SHOW_ID_RE = re.compile(r"podcasts\.apple\.com/.*/id(\d+)")


def _resolve_apple_podcast_episode(url: str) -> tuple[str, dict] | None:
    """For an Apple Podcasts episode page URL (…/id<show>?i=<episode>),
    look up the episode via the iTunes Lookup API and return
    (direct_audio_url, meta). Returns None if the URL isn't an Apple
    Podcasts episode link or the episode can't be found."""
    show_match = _APPLE_SHOW_ID_RE.search(url)
    if not show_match:
        return None
    episode_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("i", [None])[0]
    if not episode_id:
        return None
    try:
        resp = _req.get(
            "https://itunes.apple.com/lookup",
            params={"id": show_match.group(1), "entity": "podcastEpisode", "limit": 200},
            timeout=10,
        )
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            if str(r.get("trackId")) != episode_id:
                continue
            audio_url = r.get("episodeUrl") or r.get("previewUrl")
            if not audio_url:
                return None
            meta = {
                "title":       r.get("trackName", ""),
                "channel":     r.get("collectionName", ""),
                "upload_date": (r.get("releaseDate") or "")[:10].replace("-", "")
                               or date.today().strftime("%Y%m%d"),
                "duration":    int(r.get("trackTimeMillis", 0) / 1000),
                "url":         url,
                "thumbnail":   r.get("artworkUrl600", ""),
                "description": (r.get("description") or "")[:500],
                "video_id":    episode_id,
            }
            return audio_url, meta
    except Exception as exc:
        log.warning(f"Apple Podcasts episode lookup failed for {url}: {exc}")
    return None


# ── Direct audio download ─────────────────────────────────────────────────────

def _download_direct(audio_url: str, episode_dir: Path, meta: dict,
                     max_bytes: int) -> tuple[Path, dict]:
    """Stream a direct audio URL to episode_dir/audio.<ext>."""
    ext = Path(urllib.parse.urlparse(audio_url).path).suffix.lower()
    if ext not in _DIRECT_AUDIO_EXTS:
        ext = ".mp3"
    audio_path = episode_dir / f"audio{ext}"

    if audio_path.exists():
        log.info(f"Audio already present ({audio_path.name}), skipping download")
        return audio_path, meta

    log.info(f"Streaming audio from {audio_url[:80]}…")
    safe_download(audio_url, audio_path, max_bytes=max_bytes, timeout=_TIMEOUT)

    size_kb = audio_path.stat().st_size // 1024
    log.info(f"Downloaded {size_kb:,} KB → {audio_path}")
    return audio_path, meta


# ── VPS proxy download (YouTube) ─────────────────────────────────────────────

def _download_vps(url: str, episode_dir: Path, max_bytes: int) -> tuple[Path, dict]:
    """Stream YouTube audio from the Japan VPS proxy, then fetch meta via oEmbed."""
    audio_path = episode_dir / "audio.mp3"

    meta = fetch_youtube_meta_oembed(url)

    if audio_path.exists():
        log.info("Audio already present, skipping VPS download")
        return audio_path, meta

    log.info(f"Downloading via VPS proxy: {url[:80]}…")
    with _req.post(
        f"{_VPS_URL}/download",
        json={"url": url},
        headers={"X-Token": _VPS_TOKEN},
        timeout=_TIMEOUT,
        stream=True,
    ) as resp:
        resp.raise_for_status()
        try:
            expected = int(resp.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            expected = 0
        if expected > max_bytes:
            raise DownloadTooLargeError(f"Remote audio exceeds {max_bytes} bytes")
        total = 0
        with open(audio_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65_536):
                total += len(chunk)
                if total > max_bytes:
                    audio_path.unlink(missing_ok=True)
                    raise DownloadTooLargeError(f"Remote audio exceeds {max_bytes} bytes")
                fh.write(chunk)

    size_kb = audio_path.stat().st_size // 1024
    log.info(f"VPS download complete: {size_kb:,} KB → {audio_path}")
    return audio_path, meta


# ── yt-dlp download ───────────────────────────────────────────────────────────

def _download_ytdlp(url: str, episode_dir: Path, max_bytes: int) -> tuple[Path, dict]:
    """Use yt-dlp to download the first item from *url*."""
    audio_path = episode_dir / "audio.mp3"

    cookies = _get_cookies_file()
    log.info(f"yt-dlp cookies: {'loaded' if cookies else 'not set'}")

    cookie_args = ["--cookies", cookies] if cookies else []
    # YouTube periodically 403s the media download from data-center IPs even
    # when the player API succeeds. --extractor-retries forces a fresh signed
    # URL on each retry (a plain --retries would re-hit the stale 403 URL).
    retry_args = ["--retries", "5", "--fragment-retries", "5", "--extractor-retries", "3"]
    yt_args = [*retry_args, *cookie_args]

    # Fetch metadata
    info_cmd = [
        "yt-dlp", "--playlist-items", "1",
        "--dump-json", "--no-warnings", "--quiet",
        *yt_args,
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
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--max-filesize", str(max_bytes),
        "--no-warnings", "--quiet",
        "-o", str(episode_dir / "audio.%(ext)s"),
        *yt_args,
        url,
    ]
    log.info(f"Downloading {meta['title']!r} from {url}")
    result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {result.stderr.strip()}")

    if not audio_path.exists():
        raise RuntimeError("Download finished but audio.mp3 not found")
    if audio_path.stat().st_size > max_bytes:
        audio_path.unlink(missing_ok=True)
        raise DownloadTooLargeError(f"Remote audio exceeds {max_bytes} bytes")

    size_kb = audio_path.stat().st_size // 1024
    log.info(f"Downloaded {size_kb:,} KB → {audio_path}")
    return audio_path, meta


# ── YouTube oEmbed metadata ───────────────────────────────────────────────────

_YT_ID_RE = re.compile(r'(?:watch\?.*v=|youtu\.be/)([a-zA-Z0-9_-]{11})')


def fetch_youtube_meta_oembed(url: str) -> dict:
    """Fetch YouTube title/channel/thumbnail via oEmbed (no auth, works from any IP)."""
    m = _YT_ID_RE.search(url)
    video_id = m.group(1) if m else ""
    meta = {
        "title": url, "channel": "", "upload_date": date.today().strftime("%Y%m%d"),
        "duration": 0, "url": url, "thumbnail": "", "description": "",
        "video_id": video_id, "source": "url",
    }
    try:
        resp = _req.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        meta.update({
            "title":     data.get("title", url),
            "channel":   data.get("author_name", ""),
            "thumbnail": data.get("thumbnail_url", ""),
        })
    except Exception as exc:
        log.warning(f"oEmbed fetch failed: {exc}")
    return meta


# ── Public API ────────────────────────────────────────────────────────────────

def download_latest(
    urls: list[str], episode_dir: Path, dry_run: bool = False,
    max_bytes: int = _MAX_REMOTE_BYTES,
) -> tuple[Path | None, dict]:
    """Try each URL in order; return first successful (audio_path, meta)."""
    for url in urls:
        try:
            audio_path, meta = _download(url, episode_dir, dry_run, max_bytes)
            if audio_path:
                return audio_path, meta
        except Exception as exc:
            log.error(f"Download failed for {url}: {exc}")
    return None, {}


def _download(url: str, episode_dir: Path, dry_run: bool,
              max_bytes: int) -> tuple[Path | None, dict]:
    if dry_run:
        log.info(f"[DRY RUN] Would download from {url}")
        p = episode_dir / "audio.mp3"
        p.touch()
        return p, _stub_meta()

    url = validate_public_url(url)

    # ── Direct audio file URL ────────────────────────────────────────────────
    if _is_direct_audio(url):
        meta = {"title": Path(urllib.parse.urlparse(url).path).stem,
                "channel": "", "upload_date": date.today().strftime("%Y%m%d"),
                "duration": 0, "url": url, "thumbnail": "", "description": "",
                "video_id": ""}
        return _download_direct(url, episode_dir, meta, max_bytes)

    # ── Apple Podcasts: resolve via iTunes Lookup API instead of scraping
    #    the (unreliable) podcasts.apple.com webpage ──────────────────────────
    apple_result = _resolve_apple_podcast_episode(url)
    if apple_result:
        audio_url, meta = apple_result
        log.info(f"Resolved Apple Podcasts episode via iTunes Lookup: {meta['title']!r}")
        return _download_direct(audio_url, episode_dir, meta, max_bytes)

    # ── YouTube: use VPS proxy if configured ─────────────────────────────────
    if _VPS_URL and _VPS_TOKEN and _YT_RE.search(url):
        return _download_vps(url, episode_dir, max_bytes)

    # ── yt-dlp (RSS feeds, SoundCloud, NHK, …) ──────────────────────────────
    return _download_ytdlp(url, episode_dir, max_bytes)


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
