# Japanese Learning Through News & Podcasts

A personal pipeline that turns Japanese YouTube videos, news broadcasts, and podcast audio into interactive study material — automatically transcribed, translated, and analysed for vocabulary and grammar at your chosen JLPT level.

## What it does

1. **Downloads** audio from YouTube (or accepts your own upload) via yt-dlp
2. **Transcribes** the audio to Japanese text using local mlx-whisper (Apple Silicon) or the OpenAI Whisper API
3. **Translates** each segment into English and Simplified Chinese via Claude (Claude Sonnet, batch tool-use)
4. **Analyses** the transcript for JLPT vocabulary, grammar patterns, set phrases, and idioms at your chosen level (N5 through N1) via Claude
5. **Writes** per-episode flat files: `transcript.json`, `subtitles.vtt`, `analysis.json`, `cards.csv`, `meta.json`
6. **Pushes** flashcards to Anki via AnkiConnect (optional, non-blocking)

## Web UI

A local Flask app at `localhost:5000` lets you browse every processed episode:

- **Audio player** synced to the transcript — click any line to seek
- **Inline highlights** — vocabulary and grammar items are underlined in the transcript, colour-coded by JLPT level (N5 green → N4 teal → N3 blue → N2 amber → N1 rose), solid for vocab, dashed for grammar
- **Hover tooltips** — word, reading, JLPT level, English and Chinese gloss
- **EN / ZH translation toggles** per segment
- **Vocab / Grammar / Phrases side panel** — flashcard-style cards filtered to the episode's level
- **Upload page** — drag-and-drop any audio file (mp3, m4a, wav, ogg, flac, webm, aac, opus, mp4) and choose your JLPT level before processing
- **Re-translate button** — re-run only the translation step on an existing episode without reprocessing audio

## JLPT levels

When uploading or running the pipeline, choose the level that matches your current Japanese:

| Level | JLPT tiers |
|---|---|
| Beginner | N5 |
| Beginner–Intermediate | N4–N3 |
| Intermediate | N3 |
| Intermediate–Advanced | N2 |
| Advanced | N2–N1 |

Highlights, tooltips, and side-panel cards are filtered to show only items at the chosen level.

## Setup

### Requirements

- Python 3.11+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp`)
- ffmpeg (`brew install ffmpeg`)
- An [Anthropic API key](https://console.anthropic.com/)
- *(Optional)* An [OpenAI API key](https://platform.openai.com/) if using the Whisper API instead of local transcription
- *(Optional)* [Anki](https://apps.ankiweb.net/) with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on

### Install

```bash
git clone https://github.com/zchen/japanese-learning-through-news-and-podcasts.git
cd japanese-learning-through-news-and-podcasts
pip install -r requirements.txt
pip install mlx-whisper   # Apple Silicon only — skip if using OpenAI Whisper API
```

### Configure

```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

`.env` keys:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...          # only needed if USE_OPENAI_WHISPER=1

# Optional
CLAUDE_MODEL=claude-sonnet-4-6
USE_OPENAI_WHISPER=1           # set to use OpenAI API instead of local mlx-whisper
MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-mlx
ANKI_CONNECT_URL=http://localhost:8765
EPISODES_DIR=episodes
```

Edit `sources.json` to point to your preferred YouTube channels:

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

### Run the pipeline manually

```bash
# Process today's episode from sources.json
python pipeline.py

# Process a specific date
python pipeline.py --date 2026-05-04

# Override source URL
python pipeline.py --url https://www.youtube.com/watch?v=...

# Choose JLPT level (default: advanced)
python pipeline.py --url <URL> --level intermediate

# Dry run (no API calls, writes stub files)
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
├── sources.json         # YouTube channels to follow
├── lib/
│   ├── downloader.py    # yt-dlp wrapper
│   ├── transcriber.py   # Whisper (local mlx or OpenAI API)
│   ├── translator.py    # Claude — EN + ZH translation (tool use)
│   ├── analyzer.py      # Claude — JLPT analysis (tool use)
│   ├── writer.py        # Writes flat files per episode
│   └── anki.py          # AnkiConnect push
├── web/
│   ├── app.py           # Flask web UI
│   ├── templates/
│   └── static/
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

## API usage

Each episode makes two Claude API calls:

1. **Translation** — batched 40 segments at a time, ~4k output tokens per batch
2. **Analysis** — full transcript in one call, ~8k output tokens

Prompt caching is enabled on system prompts to reduce cost on repeated runs.
