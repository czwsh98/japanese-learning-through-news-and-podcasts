# Japanese Learning Through News & Podcasts

A personal pipeline that turns Japanese YouTube videos, news broadcasts, podcast episodes, and audio files into interactive study material — automatically transcribed, translated, and analysed for vocabulary and grammar at your chosen JLPT level.

## What it does

1. **Downloads** audio from YouTube, Apple Podcasts, RSS feeds, or any yt-dlp-supported source — or accepts a direct file upload
2. **Transcribes** the audio to Japanese text using the OpenAI Whisper API (default) or local mlx-whisper on Apple Silicon
3. **Translates** each segment into English and Simplified Chinese via **Google Gemini Flash**
4. **Analyses** the transcript for JLPT vocabulary, grammar patterns, set phrases, and idioms at your chosen level (N5 through N1) via **OpenAI gpt-4o-mini**
5. **Writes** per-episode flat files: `transcript.json`, `subtitles.vtt`, `analysis.json`, `cards.csv`, `meta.json`
6. **Pushes** flashcards to Anki via AnkiConnect (optional, non-blocking)

## Web UI

A local Flask app at `localhost:5000` lets you browse every processed episode:

- **Audio player** synced to the transcript — click any line to seek
- **Inline highlights** — vocabulary and grammar items underlined in the transcript, colour-coded by JLPT level (N5 green → N4 teal → N3 blue → N2 amber → N1 rose), solid underline for vocab, dashed for grammar
- **Hover tooltips** — word, reading, JLPT level, English and Chinese gloss
- **EN / ZH translation toggles** per segment
- **Vocab / Grammar / Phrases side panel** — flashcard-style cards filtered to the episode's chosen JLPT level
- **Upload page** — paste a URL or drag-and-drop an audio file, choose your JLPT level
- **Re-translate button** — re-run only the translation step on an existing episode

## Supported input sources

| Source type | Example |
|---|---|
| YouTube video or channel | `https://www.youtube.com/watch?v=...` |
| Apple Podcasts episode | `https://podcasts.apple.com/us/podcast/.../id...?i=...` |
| RSS / Atom podcast feed | `https://feeds.megaphone.fm/...` |
| SoundCloud, NHK, and more | anything yt-dlp supports |
| Direct audio URL | `.mp3`, `.m4a`, `.wav`, `.ogg`, `.flac`, etc. |
| File upload | drag-and-drop via web UI |

**Apple Podcasts** links are resolved via the iTunes Lookup API, which extracts the direct audio URL and full episode metadata (title, show, duration, artwork) automatically.

## JLPT levels

Choose the level that matches your current Japanese when uploading or running the pipeline:

| Level key | JLPT tiers highlighted |
|---|---|
| `beginner` | N5 |
| `beginner-intermediate` | N4–N3 |
| `intermediate` | N3 |
| `intermediate-advanced` | N2 |
| `advanced` | N2–N1 |

Highlights, tooltips, and side-panel cards are filtered to show only items at the chosen level.

## Setup

### Requirements

- Python 3.11+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — `brew install yt-dlp`
- ffmpeg — `brew install ffmpeg` (required for chunking large audio files)
- An [OpenAI API key](https://platform.openai.com/) — for Whisper transcription and gpt-4o-mini analysis
- A [Gemini API key](https://aistudio.google.com/) — for translation
- *(Optional)* [Anki](https://apps.ankiweb.net/) with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on

### Install

```bash
git clone https://github.com/czwsh98/japanese-learning-through-news-and-podcasts.git
cd japanese-learning-through-news-and-podcasts
pip install -r requirements.txt
pip install mlx-whisper   # Apple Silicon only — skip if using the OpenAI Whisper API
```

### Configure

```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

`.env` keys:

```
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...

# Optional overrides
GEMINI_MODEL=gemini-2.5-flash          # default translation model
OPENAI_ANALYSIS_MODEL=gpt-4o-mini      # default analysis model
USE_LOCAL_WHISPER=1                    # set to use local mlx-whisper (Apple Silicon)
MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-mlx
ANKI_CONNECT_URL=http://localhost:8765
EPISODES_DIR=episodes
```

Edit `sources.json` to point to your preferred sources for the scheduled CLI pipeline:

```json
{
  "sources": [
    { "name": "NHK Web Easy", "url": "https://www.youtube.com/@nhkwebeasynews" }
  ]
}
```

### Run the web UI

```bash
python web/app.py
# Open http://localhost:5000
```

### Run the pipeline from the CLI

```bash
# Process today's episode from sources.json
python pipeline.py

# Process a specific date
python pipeline.py --date 2026-05-04

# Override source URL for a single run
python pipeline.py --url https://www.youtube.com/watch?v=...

# Choose JLPT level (default: advanced)
python pipeline.py --url <URL> --level intermediate

# Dry run — no API calls, writes stub files to verify file layout
python pipeline.py --dry-run
```

### Automate with launchd (macOS)

```bash
# Run at 03:00 daily
cp com.japanese.pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.japanese.pipeline.plist
```

## Project structure

```
├── pipeline.py          # CLI orchestrator
├── sources.json         # Sources for the scheduled CLI pipeline
├── lib/
│   ├── downloader.py    # yt-dlp + Apple Podcasts + direct audio download
│   ├── transcriber.py   # Whisper (OpenAI API or local mlx-whisper)
│   ├── translator.py    # Gemini Flash — EN + ZH translation
│   ├── analyzer.py      # gpt-4o-mini — JLPT vocabulary & grammar analysis
│   ├── writer.py        # Writes flat files per episode
│   └── anki.py          # AnkiConnect push
├── web/
│   ├── app.py           # Flask web UI
│   ├── templates/       # Jinja2 HTML templates
│   └── static/          # CSS, JS, player
└── episodes/            # Per-episode output (gitignored)
    └── 2026-05-04/
        ├── audio.mp3
        ├── meta.json
        ├── transcript.json
        ├── subtitles.vtt
        ├── analysis.json
        └── cards.csv
```

## Episode output files

| File | Contents |
|---|---|
| `meta.json` | Title, channel, date, duration, source URL, JLPT level |
| `transcript.json` | Segments with timestamps, Japanese text, EN + ZH translations |
| `subtitles.vtt` | WebVTT subtitle file for the audio player |
| `analysis.json` | Highlights, vocab flashcards, grammar patterns, expressions |
| `cards.csv` | Anki-importable CSV (type / front / back / reading / en / zh / level / example) |

## API usage and cost

Each episode processed makes the following API calls:

| Step | API | Approx. cost (30-min episode) |
|---|---|---|
| Transcription | OpenAI Whisper | ~$0.18 |
| Translation (EN + ZH) | Gemini 2.5 Flash | ~$0.02 |
| Analysis | OpenAI gpt-4o-mini | ~$0.05 |
| **Total** | | **~$0.25** |

Audio longer than 23 MB is automatically split into 15-minute chunks by ffmpeg, transcribed in sequence, and merged — the split is transparent in the output.
