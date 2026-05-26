# Mimichan — Japanese Learning Through Listening

A multi-user web app that turns Japanese YouTube videos, news broadcasts, podcast episodes, and audio files into interactive study material — automatically transcribed, translated, and analysed for vocabulary and grammar at your chosen JLPT level.

## What it does

1. **Downloads** audio from YouTube, Apple Podcasts, RSS feeds, or any yt-dlp-supported source — or accepts a direct file upload
2. **Transcribes** the audio to Japanese text using the OpenAI Whisper API (or local mlx-whisper on Apple Silicon)
3. **Translates** each segment into English and Simplified Chinese via **Google Gemini Flash**
4. **Tokenises** the Japanese transcript with **janome** morphological analysis to attach hiragana readings (furigana) to every kanji-bearing word — client-side, no extra API cost
5. **Analyses** the transcript for JLPT vocabulary, grammar patterns, set phrases, and idioms at your chosen level (N5 through N1) via **OpenAI gpt-4o-mini**
6. **Stores** episode files in **Cloudflare R2** and metadata in **Postgres** — fully multi-user, each user sees only their own content; duplicate URLs are detected and shared across users automatically (zero re-transcription cost)
6. **Saves** words and phrases you care about to a persistent **Vocab Bank** across all episodes
7. **Exports** your saved vocab bank or per-episode flashcards as CSV for Anki import

---

## Web UI

### Episode player
- **Embedded YouTube player** (YouTube sources) or **HTML5 audio player** (uploads/podcasts), both synced to the transcript
- **Click any transcript line** to seek to that moment
- **Auto-scrolling transcript** — follows playback; scroll away and a **↓ Now playing** pill snaps you back
- **Playback speed** — 0.5× to 2×
- **Inline JLPT highlights** — underlined in the transcript, colour-coded by level:
  - N5 green · N4 teal · N3 blue · N2 amber · N1 rose · context-specific violet
  - Solid underline = vocab · dashed = grammar
- **Hover / tap tooltips** — word, reading, level badge, English and Chinese gloss
- **Sentence explain** — click any segment for a full grammatical breakdown (gpt-4o-mini), rate-limited to 5 per episode per day
- **Furigana toggle** — show/hide hiragana readings above kanji inline; keyboard shortcut `f`; toggles all segments at once
- **EN / ZH translation toggles** — desktop header buttons; mobile floating CC button
- **Vocab / Grammar / Phrases / Ctx side panel** — flashcard-style cards; save any card to your Vocab Bank with one click

### Vocab Bank (`/vocab`)
Browse, search, filter by JLPT level and type, delete, and export your saved words as CSV for Anki.

### Upload page (`/upload`)
Paste a YouTube/podcast URL or drag-and-drop an audio file, choose your JLPT level, and track progress on a live step-by-step job page.

If the same URL has already been processed by any user, the episode is shared instantly (no API calls, no quota charge). If the URL exists at a different JLPT level, only the analysis step is re-run (steps 1–3 instead of 6).

### Subscriptions (`/subscriptions`)
Manage the list of sources used by the scheduled CLI pipeline.

### Admin (`/admin`)
Admin-only dashboard showing all registered users with:
- Role badges (admin / unlimited / user)
- Jobs used vs. lifetime limit
- Total audio processed (MB) and estimated Whisper cost
- Click any row to expand full transcription history per user
- Delete non-admin users and all their data

---

## Authentication & Multi-user

- Email + password accounts; registration gated by `TRANSCRIPTION_WHITELIST` env var
- HttpOnly session cookie (90-day default); Bearer token supported for API / mobile clients
- Each user's episodes, vocab, and usage are fully isolated
- First registered account becomes admin automatically; set `BOOTSTRAP_ADMIN_EMAIL` to pin the admin email regardless of registration order

### Roles

| Role | How assigned | Transcription cap | Audio duration cap |
|---|---|---|---|
| **Admin** | `is_admin = true` in DB | None | None |
| **Unlimited** | Email in `TRANSCRIPTION_WHITELIST` | None | None |
| **Regular user** | Everyone else | 3 lifetime jobs (default) | 30 min per job |

---

## JLPT levels

| Level key | Targets |
|---|---|
| `beginner` | N5 |
| `beginner-intermediate` | N4 |
| `intermediate` | N3 |
| `intermediate-advanced` | N2 |
| `advanced` | N1 |

A sixth **context-specific** (violet) tier captures domain terminology, advanced literary expressions, and topical jargon beyond standard JLPT — always visible regardless of chosen level.

---

## Setup

### Requirements

- Python 3.11+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — `brew install yt-dlp`
- ffmpeg — `brew install ffmpeg`
- OpenAI API key (Whisper + gpt-4o-mini)
- Gemini API key (translation)
- *(Production)* PostgreSQL database and Cloudflare R2 bucket

### Install

```bash
git clone https://github.com/czwsh98/japanese-learning-through-news-and-podcasts.git
cd japanese-learning-through-news-and-podcasts
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env and fill in your keys
```

#### Core keys

```env
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
SECRET_KEY=<random 32+ char string>       # required in production
DATABASE_URL=postgresql://...             # Postgres connection string
```

#### Auth & quotas

```env
TRANSCRIPTION_WHITELIST=alice@example.com,bob@example.com   # unlimited + can register
BOOTSTRAP_ADMIN_EMAIL=you@example.com     # always admin, even if not first to register
SESSION_DAYS=90                           # session cookie lifetime (default 90)
MAX_AUDIO_MINUTES=30                      # cap for regular users (default 30)
```

#### Storage (optional — local filesystem fallback if unset)

```env
R2_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=your-bucket-name
```

#### Model overrides (optional)

```env
GEMINI_MODEL=gemini-2.5-flash
OPENAI_ANALYSIS_MODEL=gpt-4o-mini
USE_LOCAL_WHISPER=1                       # use local mlx-whisper (Apple Silicon)
MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-mlx
```

### Run locally

```bash
python web/app.py
# Open http://localhost:5000
```

> **Note:** `DATABASE_URL` is optional for local dev. Without it the app runs in no-database mode — no login required, files are served from the local `episodes/` directory, and all DB-backed features (auth, quotas, per-user vocab) are disabled.

### Run the CLI pipeline

```bash
# Process today's episodes from sources.json
python pipeline.py

# Specific date
python pipeline.py --date 2026-05-04

# Single URL
python pipeline.py --url https://www.youtube.com/watch?v=... --level intermediate

# Dry run (no API calls, writes stub files)
python pipeline.py --dry-run
```

### Deploy to Railway

1. Push to GitHub and connect the repo in Railway
2. Add a Postgres plugin — `DATABASE_URL` is injected automatically
3. Set all required env vars in Railway's Variables tab
4. The app starts, creates all DB tables automatically, and is ready

---

## Project structure

```
├── pipeline.py              # CLI orchestrator
├── sources.json             # Sources for the scheduled CLI pipeline
├── lib/
│   ├── downloader.py        # yt-dlp + Apple Podcasts + direct audio
│   ├── transcriber.py       # Whisper API or local mlx-whisper; ffprobe duration cap
│   ├── translator.py        # Gemini Flash — EN + ZH translation
│   ├── analyzer.py          # gpt-4o-mini — JLPT vocab & grammar analysis
│   ├── tokenizer.py         # janome morphological analysis — furigana readings
│   └── writer.py            # Writes flat files per episode
├── web/
│   ├── app.py               # Flask routes, background jobs, quota logic, R2, admin
│   ├── auth.py              # Registration, login, session tokens
│   ├── db.py                # SQLAlchemy models: User, Episode, VocabItem, TranscriptionUsage
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html       # Episode browser
│   │   ├── episode.html     # Player + transcript + side panel
│   │   ├── vocab.html       # Vocab Bank
│   │   ├── upload.html      # Upload / URL submit
│   │   ├── subscriptions.html
│   │   ├── job.html         # Live pipeline progress
│   │   ├── login.html
│   │   ├── register.html
│   │   └── admin.html       # Admin user dashboard
│   └── static/              # CSS, player JS
├── scripts/
│   ├── migrate_episodes_to_r2.py   # One-time migration of existing episodes to R2
│   └── migrate_existing_data.py    # Seed DB from local episode files
└── episodes/                # Local episode cache (gitignored); canonical copy is R2
```

---

## Episode output files

| File | Contents |
|---|---|
| `meta.json` | Title, channel, date, duration, source URL, JLPT level |
| `transcript.json` | Segments: timestamps, Japanese text, EN + ZH translations, per-token furigana readings |
| `subtitles.vtt` | WebVTT for the audio player |
| `analysis.json` | Highlights, vocab flashcards, grammar patterns, expressions (default level) |
| `analysis_<level>.json` | Level-specific analysis (written alongside `analysis.json` from pipeline v2+) |
| `cards.csv` | Anki-importable CSV (default level) |
| `cards_<level>.csv` | Level-specific Anki CSV |

---

## API surface

### Auth

| Route | Method | Purpose |
|---|---|---|
| `/login` `/register` `/logout` | GET/POST | Web UI auth |
| `/api/auth/login` | POST | SPA/mobile login → returns Bearer token |
| `/api/auth/register` | POST | SPA/mobile register |
| `/api/auth/me` | GET | Current user info |
| `/api/quota` | GET | Transcription quota for current user |

### Episodes & vocab

| Route | Method | Purpose |
|---|---|---|
| `/upload` | POST | Submit URL or audio file |
| `/api/upload` | POST | Same, JSON response for SPA |
| `/api/job/<id>/status` | GET | Pipeline job progress |
| `/api/episode/<slug>/meta` | GET | Episode metadata |
| `/api/episode/<slug>/transcript` | GET | Full transcript JSON |
| `/api/episode/<slug>/analysis` | GET | JLPT analysis JSON |
| `/api/vocab` | GET/POST | List / save vocab items |
| `/api/vocab/<id>` | DELETE | Remove a saved word |
| `/vocab/export.csv` | GET | Download full Vocab Bank as CSV |
| `/api/explain` | POST | Grammatical breakdown of a sentence |

### Admin

| Route | Method | Purpose |
|---|---|---|
| `/admin` | GET | User dashboard |
| `/admin/user/<id>/delete` | POST | Delete a user and all their data |
| `/api/admin/user/<id>/history` | GET | Transcription history for one user |

---

## Cost reference

Approximate cost per 30-minute episode:

| Step | API | Cost |
|---|---|---|
| Transcription | OpenAI Whisper (~$0.006/min) | ~$0.18 |
| Translation | Gemini 2.5 Flash | ~$0.02 |
| Analysis | gpt-4o-mini | ~$0.05 |
| **Total** | | **~$0.25** |

Built-in safeguards cap per-user API spend:
- 30-minute audio duration limit for regular users (ffprobe-based, not byte-size)
- 40-chunk ceiling on gpt-4o-mini analysis calls per job
- 5 sentence-explain calls per episode per day (500-char input cap)
- Atomic Postgres advisory lock prevents quota races under concurrent requests
