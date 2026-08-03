# Mimichan

Mimichan turns Japanese podcasts, YouTube videos, news programs, and uploaded audio into synchronized study material. It combines transcription, English and Simplified Chinese translation, furigana, JLPT-aware vocabulary and grammar, playback progress, shadowing, and spaced review in a multi-user web app.

## What it does

- Accepts YouTube URLs, Apple Podcasts, RSS feeds, direct media URLs, and audio uploads.
- Maintains a personal listening inbox and source subscriptions. New subscriptions automatically resolve and save cover artwork from Apple Podcasts, RSS metadata, or page social metadata when available.
- Uses Japanese YouTube captions when available; otherwise transcribes with OpenAI Whisper or optional local `mlx-whisper`.
- Translates every transcript segment into English and Simplified Chinese with DeepSeek.
- Adds furigana with Janome and finds vocabulary through a bundled JLPT word bank.
- Uses DeepSeek by default to curate useful vocabulary and identify grammar, expressions, and context-specific terminology.
- Presents a timestamp-synchronized player with clickable transcript lines, speed controls, translation/furigana toggles, and audio-only shadowing mode.
- Saves vocabulary with episode and sentence context, backlinks to every occurrence, CSV export, and a daily review queue.
- Persists listening position, completion state, episode retention, trash/restore state, processing jobs, and retry checkpoints.
- Shares already-processed source URLs across users to avoid duplicate transcription work while keeping user libraries, progress, and vocabulary isolated.
- Supports PostgreSQL metadata, Cloudflare R2 episode storage, authentication, quotas, an admin dashboard, and Telegram subscription digests.

## Processing pipeline

```text
URL or upload
  -> download/extract audio and source metadata
  -> use YouTube Japanese captions when available
     otherwise prepare speech-optimized audio and transcribe with Whisper
  -> translate segments to English and Chinese
  -> attach readings and JLPT vocabulary
  -> analyze grammar, expressions, and context vocabulary
  -> write episode artifacts and persist/upload them
```

The long-audio path is optimized for podcasts:

- Large playback files are converted to a temporary 64 kbps, mono, 16 kHz transcription copy. The original remains unchanged for listening.
- Audio still above the Whisper upload limit is split and transcribed in parallel; timestamps are merged back into one transcript.
- Translation runs in large parallel batches and retries only missing segment indices when a provider returns a partial response.
- Transcript cleanup removes only high-confidence character or consecutive phrase loops. Short utterances and legitimate repeated sentences are retained.
- Web jobs emit searchable timing logs for each pipeline stage, making bottlenecks visible in production.

The concurrency and bitrate defaults can be changed with `WHISPER_CHUNK_WORKERS`, `WHISPER_AUDIO_BITRATE`, `TRANSLATION_BATCH_SIZE`, and `TRANSLATION_WORKERS`.

## Main web features

### Today, inbox, and library

The home experience groups current listening, newly available subscription episodes, and recommendations. The full episode library supports resume state, completion tracking, retention controls, and trash/restore.

### Subscriptions and recommendations

Users can subscribe to podcast, YouTube, RSS, and other supported sources. Mimichan resolves source metadata and cover images when a subscription is created, and uses those images in subscription and recommendation cards. A scheduled digest can discover recent episodes and send them through Telegram.

### Episode player

- Embedded YouTube or HTML5 audio playback synchronized to transcript timestamps
- Click-to-seek transcript and automatic following with a return-to-current-line control
- 0.5x–2x playback speed
- English, Chinese, and furigana toggles
- JLPT-colored vocabulary and grammar highlights with readings and glosses
- Per-sentence grammar explanations
- Audio-only shadowing practice
- Save-to-vocabulary actions with the original sentence and timestamp

### Vocabulary and review

The vocabulary bank supports search, level/type filters, contextual occurrences, episode backlinks, deletion, and CSV export. Daily review uses persisted scheduling state and supports undoing the latest answer.

### Durable processing jobs

Uploads and URL submissions run as checkpointed jobs. Completed stages are reused after a failure, active and historical jobs are visible in the UI, and failed jobs can be retried without restarting successful work.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe`
- `yt-dlp`
- OpenAI API key for Whisper transcription
- DeepSeek API key for translation and the default analysis provider
- PostgreSQL and Cloudflare R2 for the production multi-user deployment

Install system tools on macOS with:

```bash
brew install ffmpeg yt-dlp
```

## Local setup

```bash
git clone https://github.com/czwsh98/japanese-learning-through-news-and-podcasts.git
cd japanese-learning-through-news-and-podcasts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least these values in `.env`:

```dotenv
OPENAI_API_KEY=your-openai-key
DEEPSEEK_API_KEY=your-deepseek-key
SECRET_KEY=generate-a-long-random-value
DATABASE_URL=postgresql://user:password@localhost:5432/japanese
```

Generate a session secret with `python -c "import secrets; print(secrets.token_hex(32))"`.

Then run:

```bash
python web/app.py
```

Open `http://localhost:5000`. `DATABASE_URL` is optional for basic local browsing and pipeline development; database-backed authentication, user isolation, quotas, progress, vocabulary, review, and job history require PostgreSQL.

## Configuration

The checked-in `.env.example` contains safe placeholders. Important settings are:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI Whisper transcription; also used if analysis is switched to OpenAI |
| `DEEPSEEK_API_KEY` | English/Chinese translation and default transcript analysis |
| `DEEPSEEK_MODEL` | DeepSeek model; defaults to `deepseek-v4-flash` |
| `ANALYSIS_PROVIDER` | Analysis backend; defaults to `deepseek`, or set to `openai` |
| `EXPLAIN_PROVIDER` | Sentence-explanation backend; defaults to `deepseek` |
| `EXPLAIN_MODEL` | Sentence-explanation model; defaults to `deepseek-v4-flash` |
| `OPENAI_ANALYSIS_MODEL` | OpenAI model used only when an OpenAI provider override is selected |
| `USE_LOCAL_WHISPER` | Set to `1` to use local `mlx-whisper` instead of the API |
| `MLX_WHISPER_MODEL` | Local Whisper model override |
| `MAX_AUDIO_MINUTES` | Per-job duration cap for regular users; defaults to 30 minutes |
| `DATABASE_URL` | PostgreSQL connection URL |
| `SECRET_KEY` | Flask session signing key; required in production |
| `R2_ENDPOINT_URL` | Cloudflare R2/S3 endpoint |
| `R2_ACCESS_KEY_ID` | R2 access key ID |
| `R2_SECRET_ACCESS_KEY` | R2 secret access key |
| `R2_BUCKET` | Episode artifact bucket |
| `HTTPS_PROXY` | Optional proxy, including for OpenAI in restricted regions |
| `NO_PROXY` | Hosts that should bypass the proxy; production routes DeepSeek directly |
| `VPS_DOWNLOAD_URL` | Optional remote download service for difficult media sources |
| `VPS_DOWNLOAD_TOKEN` | Authentication token for that download service |

Authentication and quota deployments can additionally set `TRANSCRIPTION_WHITELIST`, `REGISTRATION_WHITELIST`, `BOOTSTRAP_ADMIN_EMAIL`, and `SESSION_DAYS`.

Never commit `.env`, exported cookies, database dumps, tokens, or private keys. They are runtime configuration and are intentionally ignored by Git.

## CLI pipeline

Process the configured source for today:

```bash
python pipeline.py
```

Common examples:

```bash
python pipeline.py --date 2026-08-03
python pipeline.py --url 'https://www.youtube.com/watch?v=VIDEO_ID' --level intermediate
python pipeline.py --dry-run
python pipeline.py --force
```

Available study levels are `beginner`, `beginner-intermediate`, `intermediate`, `intermediate-advanced`, and `advanced`.

## Docker deployment

The included Compose stack is:

```text
Internet -> Caddy (TLS) -> Gunicorn/Flask -> PostgreSQL
                                  |
                                  +-> Cloudflare R2
                                  +-> OpenAI / DeepSeek
```

Create `.env`, including `POSTGRES_PASSWORD` and the application/API settings, then deploy:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

The Compose file supplies `DATABASE_URL` to the web container and mounts `./state` for mutable subscription and recent-episode state. Caddy terminates HTTPS using the checked-in `Caddyfile`.

For production upgrades:

```bash
git pull --ff-only
docker compose up -d --build web
```

Back up PostgreSQL and verify R2 recovery before relying on the service. `scripts/backup_db.sh`, `scripts/restore_from_r2.py`, and the migration scripts provide the repository's operational building blocks.

## Testing

Install the test runner in your development environment and run the complete suite:

```bash
pip install pytest
pytest -q
```

The tests cover the analyzer and JLPT bank, sharing, subscription recommendations and artwork, digest behavior, web workflows, transcription performance paths, and transcript/translation completeness regressions.

## Project structure

```text
pipeline.py                     CLI pipeline orchestration
lib/downloader.py               yt-dlp, podcast, RSS, and direct-media handling
lib/transcriber.py              captions/local Whisper/OpenAI Whisper paths
lib/translator.py               batched DeepSeek EN/ZH translation
lib/tokenizer.py                Janome tokenization and furigana readings
lib/jlpt_bank.py                bundled JLPT vocabulary lookup
lib/analyzer.py                 vocabulary curation and grammar analysis
lib/writer.py                   episode artifact writer
web/app.py                      Flask routes, jobs, storage, quotas, admin
web/auth.py                     sessions and authentication
web/db.py                       SQLAlchemy models and database helpers
web/recommendations.py          source recommendations and metadata/artwork lookup
web/templates/                  server-rendered web UI
web/static/                     player and application assets
frontend/                       Capacitor/mobile frontend and iOS project
data/jlpt_bank.json.gz          bundled JLPT vocabulary data
scripts/                        migrations, backup/restore, retention, backfills
tests/                          pytest regression suite
state/                          mutable deployment state (mounted by Compose)
```

## Episode artifacts

An episode may contain:

| File | Contents |
|---|---|
| `meta.json` | Source, title, channel, date, duration, thumbnail, and study level |
| `transcript.json` | Timestamped Japanese segments, EN/ZH translations, and token readings |
| `subtitles.vtt` | WebVTT subtitles |
| `analysis.json` | Vocabulary, grammar, phrases, and context-specific items |
| `analysis_<level>.json` | Level-specific analysis |
| `cards.csv` | Anki-compatible flashcards |
| `cards_<level>.csv` | Level-specific flashcards |

In production these artifacts are stored in R2 while PostgreSQL holds user, episode, job, progress, vocabulary, and usage metadata.

## Operational notes

- YouTube extraction can require refreshed cookies or the optional remote downloader when a hosting provider's IP is challenged.
- Rebuild the web image when `yt-dlp` needs upgrading.
- Search web logs for `Pipeline stage timing` to compare download, transcription, translation, tokenization, analysis, write, and upload durations.
- Search web logs for `API usage` to aggregate provider/model token counts, retries, cache hits, and estimated text-model cost without logging prompts or responses.
- Sentence explanations are cached by a privacy-safe content hash; repeat requests for the same sentence, prompt version, provider, and model do not make another API call.
- Long-audio jobs need enough temporary disk for the original audio, compact transcription audio, and chunks.
