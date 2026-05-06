"""Download episode audio from a yt-dlp source URL or podcast directory link.

Supported inputs
----------------
- YouTube, NHK, SoundCloud, and anything else yt-dlp handles
- RSS/Atom podcast feed URLs (handled by yt-dlp)
- Apple Podcasts episode URLs  (resolved via iTunes Lookup API → direct audio)
- Direct audio file URLs       (streamed with requests)
"""
import json
import logging
import re
import subprocess
import urllib.parse
from datetime import date
from pathlib import Path

import requests as _req

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

_DIRECT_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus", ".webm"}


def _is_apple_podcasts(url: str) -> bool:
    return "podcasts.apple.com" in url


def _is_direct_audio(url: str) -> bool:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext in _DIRECT_AUDIO_EXTS


# ── iTunes Lookup API ─────────────────────────────────────────────────────────

def _resolve_apple_podcasts(url: str) -> tuple[str, dict] | None:
    """Return (direct_audio_url, meta) for an Apple Podcasts episode URL, or None."""
    match = re.search(r"[?&]i=(\d+)", url)
    if not match:
        log.warning("Apple Podcasts URL has no ?i=episode_id — cannot resolve via iTunes API")
        return None

    episode_id = match.group(1)
    log.info(f"Resolving Apple Podcasts episode {episode_id} via iTunes Lookup API …")

    try:
        resp = _req.get(
            "https://itunes.apple.com/lookup",
            params={"id": episode_id},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        log.warning(f"iTunes Lookup API failed: {exc}")
        return None

    if not results:
        log.warning("iTunes Lookup returned no results")
        return None

    r = results[0]
    audio_url = r.get("episodeUrl", "")
    if not audio_url:
        log.warning("iTunes result has no episodeUrl")
        return None

    # Normalise upload_date to YYYYMMDD
    raw_date = (r.get("releaseDate") or "")[:10].replace("-", "")

    meta = {
        "title":       r.get("trackName", ""),
        "channel":     r.get("collectionName") or r.get("artistName", ""),
        "upload_date": raw_date,
        "duration":    int((r.get("trackTimeMillis") or 0) // 1000),
        "url":         url,
        "thumbnail":   r.get("artworkUrl600") or r.get("artworkUrl160", ""),
        "description": (r.get("description") or "")[:500],
        "video_id":    episode_id,
    }

    log.info(f"Resolved: {meta['title']!r} — {audio_url[:80]}…")
    return audio_url, meta


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
    with _req.get(audio_url, stream=True, timeout=600) as resp:
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

    # Fetch metadata
    info_cmd = [
        "yt-dlp", "--playlist-items", "1",
        "--dump-json", "--no-warnings", "--quiet",
        url,
    ]
    result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)
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
        url,
    ]
    log.info(f"Downloading {meta['title']!r} from {url}")
    result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
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

    # ── Apple Podcasts directory link ────────────────────────────────────────
    if _is_apple_podcasts(url):
        resolved = _resolve_apple_podcasts(url)
        if resolved:
            direct_url, meta = resolved
            return _download_direct(direct_url, episode_dir, meta)
        log.warning("Apple Podcasts resolution failed — falling through to yt-dlp")

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
