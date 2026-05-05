"""Download latest episode audio from a yt-dlp source URL."""
import json
import logging
import subprocess
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)


def download_latest(
    urls: list[str], episode_dir: Path, dry_run: bool = False
) -> tuple[Path | None, dict]:
    """Try each URL in order, return first successful (audio_path, meta)."""
    for url in urls:
        try:
            audio_path, meta = _download(url, episode_dir, dry_run)
            if audio_path:
                return audio_path, meta
        except Exception as e:
            log.error(f"Download failed for {url}: {e}")
    return None, {}


def _download(url: str, episode_dir: Path, dry_run: bool) -> tuple[Path | None, dict]:
    audio_path = episode_dir / "audio.mp3"

    if dry_run:
        log.info(f"[DRY RUN] Would download from {url}")
        audio_path.touch()
        return audio_path, _stub_meta()

    # Fetch metadata for the latest video without downloading
    info_cmd = [
        "yt-dlp",
        "--playlist-items", "1",
        "--dump-json",
        "--no-warnings",
        "--quiet",
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

    # Download and extract audio
    dl_cmd = [
        "yt-dlp",
        "--playlist-items", "1",
        "-x", "--audio-format", "mp3",
        "--audio-quality", "192",
        "--no-warnings",
        "--quiet",
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


def _extract_meta(info: dict) -> dict:
    return {
        "title": info.get("title", ""),
        "channel": info.get("channel") or info.get("uploader", ""),
        "upload_date": info.get("upload_date", ""),
        "duration": info.get("duration", 0),
        "url": info.get("webpage_url", ""),
        "thumbnail": info.get("thumbnail", ""),
        "description": (info.get("description") or "")[:500],
        "video_id": info.get("id", ""),
    }


def _stub_meta() -> dict:
    return {
        "title": "Dry-run episode",
        "channel": "Test Channel",
        "upload_date": date.today().strftime("%Y%m%d"),
        "duration": 120,
        "url": "",
        "thumbnail": "",
        "description": "",
        "video_id": "dry-run",
    }
