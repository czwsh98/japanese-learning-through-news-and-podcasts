#!/usr/bin/env python3
"""
mimichan_bot.py — Telegram bot for the mimichan Japanese podcast pipeline.
Runs on dmit-hk, directly calls docker/local files — no SSH needed.

Processing goes through the web app's authenticated API so episodes land in
the DB + R2 under the owner's account (visible on the website) AND expose live
step status (download → transcribe → translate → analyse → write → cloud).

Commands:
  /check    — scan subscriptions for new episodes, push notifications
  /status   — show currently-processing episodes and their step
  /ep       — list 10 most recent processed episodes
  /help     — show help

Inline buttons:
  📖 Process  — run the pipeline for that episode URL (live status)
"""

import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ── Config ────────────────────────────────────────────────────────────────────

SOURCES_FILE = "/root/mimichan/sources.json"
CONFIG_FILE  = "/root/mimichan/bot_config.json"
LOG_FILE     = "/root/mimichan/mimichan_bot.log"
PID_FILE     = "/tmp/mimichan_bot.pid"
TOKEN_FILE   = "/root/mimichan/.web_token"

DB_CONTAINER  = "mimichan-db-1"
WEB_CONTAINER = "mimichan-web-1"
DB_CMD        = ["docker", "exec", DB_CONTAINER, "psql", "-U", "app", "-d", "japanese", "-At", "-c"]

WEB_BASE   = "http://localhost:8000"        # reachable inside the web container
SITE_BASE  = "https://mimichan.ziwei-chen.com"

# Friendly labels for the web app's 6 processing steps (keyed by step_num).
STEP_LABELS = {
    1: "⬇️ Downloading audio",
    2: "🎧 Transcribing (Whisper)",
    3: "🌐 Translating (EN + ZH)",
    4: "📝 Analysing vocab & grammar",
    5: "💾 Writing episode files",
    6: "☁️ Saving to cloud",
}

# md5-key → url  (for the 64-byte callback_data limit)
_PENDING      = {}
_PENDING_LOCK = threading.Lock()

# job_id → {url, title, slug, step, step_num, total_steps, status, chat_id, started_at}
_JOBS      = {}
_JOBS_LOCK = threading.Lock()

# Cached web-API bearer token (owner session)
_WEB_TOKEN      = None
_WEB_TOKEN_LOCK = threading.Lock()


# ── Single-instance lock ──────────────────────────────────────────────────────

def acquire_lock():
    if os.path.exists(PID_FILE):
        try:
            old = int(open(PID_FILE).read().strip())
            os.kill(old, 0)
            print(f"[mimichan_bot] another instance running (pid={old}), exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    open(PID_FILE, "w").write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.unlink(PID_FILE) if os.path.exists(PID_FILE) else None)


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Telegram API ──────────────────────────────────────────────────────────────

def tg(token, method, _timeout=15, **params):
    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"tg {method} error: {e}")
        return {}


def send(token, chat_id, text, **kwargs):
    return tg(token, "sendMessage", chat_id=chat_id, text=text, **kwargs)


def edit(token, chat_id, msg_id, text, **kwargs):
    return tg(token, "editMessageText", chat_id=chat_id, message_id=msg_id, text=text, **kwargs)


def answer_callback(token, callback_query_id, text=""):
    tg(token, "answerCallbackQuery", callback_query_id=callback_query_id, text=text)


# ── Pending URL store ─────────────────────────────────────────────────────────

def store_ep(url, title="", channel=""):
    import hashlib
    key = hashlib.md5(url.encode()).hexdigest()[:8]
    with _PENDING_LOCK:
        _PENDING[key] = {"url": url, "title": title, "channel": channel}
    return key


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_query(sql):
    r = subprocess.run(DB_CMD + [sql], capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def get_known_urls():
    raw = db_query("SELECT url FROM episodes")
    return {u.strip() for u in raw.splitlines() if u.strip()}


# ── Web-API auth (owner session token) ────────────────────────────────────────

def _mint_web_token(owner_email):
    """Mint a fresh owner session token inside the web container. Never logged."""
    # Ensure the helper exists in the container (self-heal after a rebuild).
    subprocess.run(
        ["docker", "cp", "/root/mimichan/_mint_token.py",
         f"{WEB_CONTAINER}:/app/_mint_token.py"],
        capture_output=True, text=True, timeout=30,
    )
    r = subprocess.run(
        ["docker", "exec", WEB_CONTAINER, "python", "/app/_mint_token.py", owner_email],
        capture_output=True, text=True, timeout=30,
    )
    tok = r.stdout.strip()
    if not tok:
        log(f"token mint failed: {r.stderr[:200]}")
        return None
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(tok)
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return tok


def _get_web_token(owner_email, force=False):
    global _WEB_TOKEN
    with _WEB_TOKEN_LOCK:
        if force:
            _WEB_TOKEN = None
        if _WEB_TOKEN:
            return _WEB_TOKEN
        # try cached file first
        if not force and os.path.exists(TOKEN_FILE):
            try:
                cached = open(TOKEN_FILE).read().strip()
                if cached:
                    _WEB_TOKEN = cached
                    return _WEB_TOKEN
            except Exception:
                pass
        _WEB_TOKEN = _mint_web_token(owner_email)
        return _WEB_TOKEN


def _web_curl(method, path, token, form=None, timeout=60):
    """Call the web API via curl inside the container. Returns (status, body_text)."""
    args = ["docker", "exec", WEB_CONTAINER, "curl", "-s",
            "-w", "\n%{http_code}", "-X", method, WEB_BASE + path,
            "-H", f"Authorization: Bearer {token}"]
    for k, v in (form or {}).items():
        args += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out    = r.stdout
    nl     = out.rfind("\n")
    body   = out[:nl] if nl >= 0 else out
    status = out[nl + 1:].strip() if nl >= 0 else ""
    try:
        status = int(status)
    except ValueError:
        status = 0
    return status, body


def web_api(owner_email, method, path, form=None, timeout=60):
    """Authenticated web-API call with one automatic token refresh on 401."""
    token = _get_web_token(owner_email)
    if not token:
        return None, "no token"
    status, body = _web_curl(method, path, token, form, timeout)
    if status == 401:
        token = _get_web_token(owner_email, force=True)
        if not token:
            return None, "no token"
        status, body = _web_curl(method, path, token, form, timeout)
    try:
        return json.loads(body), None
    except Exception:
        return None, f"HTTP {status}: {body[:150]}"


# ── Subscription checking ─────────────────────────────────────────────────────

def fetch_latest(source):
    """Return (ep_url, title, channel) or None."""
    rss_url = source.get("rss_url")
    url     = source["url"]
    name    = source.get("name", url)

    if rss_url:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                xml_bytes = r.read()
            root = ET.fromstring(xml_bytes)
            item = root.find("./channel/item")
            if item is None:
                return None
            enc     = item.find("enclosure")
            ep_url  = (enc.get("url") if enc is not None else None) or item.findtext("link") or url
            title   = item.findtext("title") or "(unknown)"
            channel = root.findtext("./channel/title") or name
            return ep_url, title, channel
        except Exception as e:
            log(f"RSS fetch failed for {name}: {e}")
            return None
    else:
        try:
            r = subprocess.run(
                ["docker", "exec", WEB_CONTAINER, "yt-dlp",
                 "--socket-timeout", "10", "--playlist-items", "1",
                 "--dump-json", "--no-warnings", "--quiet", url],
                capture_output=True, text=True, timeout=40
            )
            if not r.stdout.strip():
                return None
            info    = json.loads(r.stdout.strip().splitlines()[0])
            ep_url  = info.get("webpage_url") or info.get("url") or url
            title   = info.get("title", "(unknown)")
            channel = info.get("channel") or info.get("uploader") or name
            return ep_url, title, channel
        except Exception as e:
            log(f"yt-dlp failed for {name}: {e}")
            return None


def do_check(token, chat_id):
    try:
        with open(SOURCES_FILE) as f:
            sources = json.load(f).get("sources", [])
    except Exception as e:
        return f"Could not read sources: {e}"

    known = get_known_urls()
    found = []
    for source in sources:
        result = fetch_latest(source)
        if result is None:
            continue
        ep_url, title, channel = result
        if ep_url not in known:
            found.append({"url": ep_url, "title": title, "channel": channel})

    if not found:
        return "No new episodes."

    for ep in found:
        key      = store_ep(ep["url"], ep["title"], ep["channel"])
        keyboard = {"inline_keyboard": [[
            {"text": "📖 Process", "callback_data": f"mc:process:{key}"},
        ]]}
        tg(token, "sendMessage", chat_id=chat_id,
           text=f"🎙 New episode\n*{ep['title']}*\n_{ep['channel']}_",
           parse_mode="Markdown", reply_markup=json.dumps(keyboard))

    return f"Found {len(found)} new episode(s)."


# ── Recent-episodes cache (for the /subscriptions web page) ────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def _one_line(text, max_len=200):
    """Collapse an RSS/description blob to a single trimmed line, tags stripped."""
    if not text:
        return ""
    line = html.unescape(_TAG_RE.sub(" ", text))
    line = " ".join(line.split())
    return line if len(line) <= max_len else line[:max_len].rstrip() + "…"


_YT_TABS = ("videos", "streams", "shorts", "featured", "playlists", "community", "about")


def _youtube_uploads_url(url):
    """Point a YouTube channel/handle URL at its /videos tab so a flat-playlist
    lists recent uploads instead of the channel's tab list (Videos/Live/Shorts).
    URLs that already target a tab — or aren't YouTube channel roots — are left
    unchanged."""
    if "youtube.com" not in url:
        return url
    base  = url.rstrip("/")
    parts = [p for p in urllib.parse.urlparse(base).path.split("/") if p]
    if not parts or parts[-1] in _YT_TABS:
        return url  # not a channel path, or already targets a tab
    is_channel_root = (len(parts) == 1 and parts[0].startswith("@")) or \
                      (len(parts) == 2 and parts[0] in ("channel", "c", "user"))
    return base + "/videos" if is_channel_root else url


def fetch_recent(source, limit=5):
    """Return up to `limit` recent items [{title, description}] for a source.

    Mirrors fetch_latest's transport choice: parse rss_url directly (fast, no
    YouTube throttling), else fall back to yt-dlp --flat-playlist for channels.
    Descriptions only come from RSS feeds; YouTube flat listings have none.
    """
    rss_url = source.get("rss_url")
    url     = source["url"]
    name    = source.get("name", url)

    if rss_url:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read())
            out = []
            for item in root.findall("./channel/item")[:limit]:
                out.append({
                    "title":       (item.findtext("title") or "(untitled)").strip(),
                    "description": _one_line(item.findtext("description")),
                })
            return out
        except Exception as e:
            log(f"recent RSS fetch failed for {name}: {e}")
            return []
    else:
        try:
            r = subprocess.run(
                ["docker", "exec", WEB_CONTAINER, "yt-dlp",
                 "--flat-playlist", "--playlist-end", str(limit),
                 "--dump-single-json", "--no-warnings", "--quiet",
                 _youtube_uploads_url(url)],
                capture_output=True, text=True, timeout=60,
            )
            if not r.stdout.strip():
                return []
            data    = json.loads(r.stdout.strip())
            entries = (data.get("entries") or [])[:limit]
            return [{"title": (e.get("title") or "(untitled)"),
                     "description": _one_line(e.get("description"))}
                    for e in entries]
        except Exception as e:
            log(f"recent yt-dlp failed for {name}: {e}")
            return []


def refresh_recent_cache(owner_email):
    """Fetch recent episodes for every source and push them to the web app so
    the /subscriptions page can display them. Called by the daily cron check."""
    try:
        with open(SOURCES_FILE) as f:
            sources = json.load(f).get("sources", [])
    except Exception as e:
        log(f"recent cache: cannot read sources: {e}")
        return

    cache = {}
    for source in sources:
        items = fetch_recent(source, limit=5)
        if items:
            cache[source["url"]] = {
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "episodes":   items,
            }

    if not cache:
        log("recent cache: nothing fetched, skipping push")
        return

    resp, err = web_api(owner_email, "POST", "/api/subscriptions/recent",
                        form={"payload": json.dumps(cache, ensure_ascii=False)})
    if err:
        log(f"recent cache push failed: {err}")
    else:
        log(f"recent cache pushed: {resp.get('sources', len(cache))} sources")


def do_episodes(limit=10):
    rows = db_query(f"SELECT title, slug FROM episodes ORDER BY created_at DESC LIMIT {limit}")
    if not rows:
        return "No processed episodes yet."
    lines = ["🎙 Recent episodes:"]
    for row in rows.splitlines():
        parts = row.split("|")
        title = parts[0][:55] if parts else "(untitled)"
        slug  = parts[1] if len(parts) > 1 else ""
        lines.append(f"• {title}\n  {SITE_BASE}/episode/{slug}")
    return "\n".join(lines)


# ── Processing via web API (with live status) ─────────────────────────────────

def _progress_text(title, step_num, total_steps, label):
    filled = "▰" * step_num + "▱" * max(0, total_steps - step_num)
    return (f"🎙 Processing\n*{title[:60]}*\n\n"
            f"{label}\n{filled}  {step_num}/{total_steps}")


def do_process(bot_token, owner_email, chat_id, url, title="(episode)", channel="", msg_id=None):
    """Kick off processing through the web API and stream live status."""
    form = {"source_url": url, "level": "advanced"}
    # Pass the RSS-derived title/channel so podcast episodes get real titles
    # instead of the audio file's id (enclosures often lack ID3 tags).
    if title and title != "(episode)":
        form["title"] = title
    if channel:
        form["channel"] = channel
    resp, err = web_api(owner_email, "POST", "/api/upload", form=form)
    if err or resp is None:
        if msg_id:
            edit(bot_token, chat_id, msg_id, f"❌ Could not start: {err}")
        else:
            send(bot_token, chat_id, f"❌ Could not start: {err}")
        return

    job_id = resp.get("job_id")
    slug   = resp.get("slug")

    # Dedup: episode already exists → no job, just link it.
    if not job_id and slug:
        link = f"{SITE_BASE}/episode/{slug}"
        txt  = f"✅ Already processed\n{link}"
        if msg_id:
            edit(bot_token, chat_id, msg_id, txt)
        else:
            send(bot_token, chat_id, txt)
        return

    if not job_id:
        msg = "❌ Upload returned no job id."
        edit(bot_token, chat_id, msg_id, msg) if msg_id else send(bot_token, chat_id, msg)
        return

    with _JOBS_LOCK:
        _JOBS[job_id] = {"url": url, "title": title, "slug": slug,
                         "step": "", "step_num": 0, "total_steps": 6,
                         "status": "processing", "chat_id": chat_id,
                         "started_at": time.time()}

    if not msg_id:
        r = send(bot_token, chat_id, f"⏳ Starting…\n{title[:60]}")
        msg_id = r.get("result", {}).get("message_id")
    else:
        edit(bot_token, chat_id, msg_id, f"⏳ Starting…\n{title[:60]}")

    threading.Thread(
        target=_poll_job,
        args=(bot_token, owner_email, chat_id, msg_id, job_id, title, slug),
        daemon=True,
    ).start()


def _poll_job(bot_token, owner_email, chat_id, msg_id, job_id, title, slug):
    last_label = None
    log(f"Processing start job={job_id} url={title[:50]}")
    deadline = time.time() + 3600
    while time.time() < deadline:
        resp, err = web_api(owner_email, "GET", f"/api/job/{job_id}/status", timeout=20)
        if err or resp is None:
            time.sleep(5)
            continue

        status      = resp.get("status", "")
        step_num    = resp.get("step_num", 0)
        total_steps = resp.get("total_steps", 6)
        step_text   = resp.get("step", "")
        final_slug  = resp.get("slug") or slug

        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id].update(status=status, step=step_text,
                                     step_num=step_num, total_steps=total_steps,
                                     slug=final_slug)

        if status == "done":
            link = f"{SITE_BASE}/episode/{final_slug}"
            edit(bot_token, chat_id, msg_id, f"✅ Done\n*{title[:60]}*\n{link}",
                 parse_mode="Markdown")
            log(f"Processing done job={job_id} slug={final_slug}")
            break
        if status == "error":
            edit(bot_token, chat_id, msg_id,
                 f"❌ Failed\n*{title[:60]}*\n{resp.get('error','')[:150]}",
                 parse_mode="Markdown")
            log(f"Processing error job={job_id}: {resp.get('error','')[:150]}")
            break

        label = STEP_LABELS.get(step_num, step_text or "Working…")
        if label != last_label:
            edit(bot_token, chat_id, msg_id,
                 _progress_text(title, step_num, total_steps, label),
                 parse_mode="Markdown")
            last_label = label

        time.sleep(5)
    else:
        edit(bot_token, chat_id, msg_id, f"⏱ Timed out watching\n{title[:60]}")

    # prune from active jobs after a short grace period
    time.sleep(30)
    with _JOBS_LOCK:
        _JOBS.pop(job_id, None)


def do_status():
    with _JOBS_LOCK:
        active = [j for j in _JOBS.values() if j["status"] == "processing"]
    if not active:
        return "💤 No episodes processing right now."
    lines = ["⚙️ Processing now:"]
    for j in active:
        label = STEP_LABELS.get(j["step_num"], j.get("step") or "…")
        el    = int(time.time() - j["started_at"])
        lines.append(f"• *{j['title'][:45]}*\n  {label} ({j['step_num']}/{j['total_steps']}) · {el}s")
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_message(bot_token, owner_email, chat_id, text, authorized_id):
    if chat_id != authorized_id:
        return
    text = (text or "").strip()
    low  = text.lower()
    if low in ("/check", "/check@mimichanjp_bot"):
        send(bot_token, chat_id, "🔍 Checking subscriptions…")
        send(bot_token, chat_id, do_check(bot_token, chat_id))
    elif low in ("/status", "/status@mimichanjp_bot"):
        send(bot_token, chat_id, do_status(), parse_mode="Markdown")
    elif low in ("/ep", "/episodes"):
        send(bot_token, chat_id, do_episodes())
    elif low in ("/help", "/start"):
        send(bot_token, chat_id,
             "🎙 *mimichan bot*\n\n"
             "/check — scan subscriptions for new episodes\n"
             "/status — show what's processing now\n"
             "/ep — recent processed episodes (with links)\n\n"
             "Tap 📖 Process on any notification to run the pipeline; "
             "the message updates live through transcribe → translate → done.",
             parse_mode="Markdown")


def handle_callback(bot_token, owner_email, callback_query, authorized_id):
    cq_id   = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    msg_id  = callback_query["message"]["message_id"]
    data    = callback_query.get("data", "")

    answer_callback(bot_token, cq_id)
    if chat_id != authorized_id:
        return

    if data.startswith("mc:process:"):
        key = data.split(":", 2)[2]
        with _PENDING_LOCK:
            ep = _PENDING.get(key)
        if not ep:
            edit(bot_token, chat_id, msg_id, "⚠️ Button expired — run /check again.")
            return
        # Fall back to the message text for the title if the store was lost.
        title = ep.get("title", "")
        if not title:
            orig = callback_query["message"].get("text", "")
            parts = orig.split("\n") if orig else []
            title = parts[1].strip("*_ ") if len(parts) >= 2 else "(episode)"
        do_process(bot_token, owner_email, chat_id, ep["url"],
                   title=title or "(episode)", channel=ep.get("channel", ""), msg_id=msg_id)


# ── Long-polling loop ─────────────────────────────────────────────────────────

def poll(bot_token, owner_email, authorized_id):
    offset = 0
    log(f"Polling started, authorized_id={authorized_id}")
    while True:
        try:
            resp = tg(bot_token, "getUpdates", _timeout=40, offset=offset,
                      timeout=30, allowed_updates=["message", "callback_query"])
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    m = update["message"]
                    handle_message(bot_token, owner_email, m["chat"]["id"],
                                   m.get("text", ""), authorized_id)
                elif "callback_query" in update:
                    handle_callback(bot_token, owner_email,
                                    update["callback_query"], authorized_id)
        except KeyboardInterrupt:
            log("Shutting down.")
            break
        except Exception as e:
            log(f"Poll error: {e}")
            time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    acquire_lock()
    cfg           = load_config()
    bot_token     = cfg["token"]
    authorized_id = cfg["chat_id"]
    owner_email   = cfg.get("owner_email", "czwsimon@gmail.com")
    log(f"mimichan_bot started, authorized={authorized_id}, owner={owner_email}")
    poll(bot_token, owner_email, authorized_id)


if __name__ == "__main__":
    main()
