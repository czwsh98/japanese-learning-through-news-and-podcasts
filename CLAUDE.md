# CLAUDE.md

This file provides guidance to Gemini when working with code in this repository.

## What this project is

A personal automation pipeline that downloads Japanese YouTube videos/podcasts, transcribes them with Whisper, translates segments with Gemini Flash, analyzes vocabulary/grammar for JLPT study with gpt-4o-mini, and serves the results through a Flask web UI with an audio player synced to the transcript.

## Commands

```bash
# Run the pipeline (processes today's episodes from sources.json)
python pipeline.py

# Common flags
python pipeline.py --date 2026-05-04
python pipeline.py --url https://youtube.com/... --level intermediate
python pipeline.py --dry-run   # generates stub files without API calls

# Start the web UI (localhost:5000)
python web/app.py
```

No build step. No test suite. Dependencies are in `requirements.txt`; install with `pip install -r requirements.txt`. Requires a `.env` file with `OPENAI_API_KEY` and `GEMINI_API_KEY` (see `.env.example`).

## Architecture

### Pipeline flow

```
Input (URL or sources.json)
  → lib/downloader.py   — yt-dlp / Apple Podcasts / RSS / direct audio
  → lib/transcriber.py  — Whisper API (auto-chunks files >23 MB via ffmpeg)
  → lib/translator.py   — Gemini Flash (50 segments/call, EN + ZH)
  → lib/analyzer.py     — gpt-4o-mini function calling (JLPT vocab/grammar/expressions)
  → lib/writer.py       — writes all output files to episodes/YYYY-MM-DD/
```

`pipeline.py` is the CLI orchestrator. `web/app.py` runs the same pipeline steps in a background thread and streams status to the browser.

### Per-episode output (`episodes/YYYY-MM-DD/<slug>/`)

| File | Contents |
|------|----------|
| `meta.json` | Title, channel, URL, duration, JLPT level, thumbnail |
| `transcript.json` | Segments: timestamps + Japanese text + EN/ZH translations |
| `subtitles.vtt` | WebVTT for the HTML5 audio player |
| `analysis.json` | Vocabulary, grammar patterns, expressions with JLPT level tags |
| `highlights.json` | Subset of analysis.json — only JLPT items found |
| `cards.csv` | Anki-importable CSV (type/front/back/reading/en/zh/register/level/example/tags) |
| `audio.mp3` | Downloaded audio |

### JLPT levels

The `--level` flag (or `jlpt_level` in meta.json) controls what the analyzer targets:
- `beginner`: N5
- `beginner-intermediate`: N4
- `intermediate`: N3
- `intermediate-advanced`: N2
- `advanced`: N1
- Plus `context-specific` for domain/literary/specialized terms

The web UI color-codes highlights: N5 green → N4 teal → N3 blue → N2 amber → N1 rose → context-specific violet.

### Web UI (`web/`)

- `web/app.py` — Flask routes + background job threading
- `web/templates/` — Jinja2: `base.html`, `index.html` (episode browser), `upload.html`, `episode.html` (player + transcript), `job.html` (pipeline status)
- `web/static/` — CSS and the audio player JS (transcript sync, JLPT highlight tooltips, translation panel, mobile FAB)

The episode player JS handles: click-to-seek on transcript lines, auto-follow during playback, inline JLPT hover tooltips, translation/grammar side panel, and a mobile floating action button for toggling translations.

### Key implementation details

- **Chunking**: `transcriber.py` splits audio >23 MB into 15-minute chunks with ffmpeg, transcribes each, then merges segments while preserving timestamps.
- **Hallucination filtering**: 4-layer filter in `transcriber.py` (empty/short, character loops, phrase loops, cross-segment loops).
- **Structured output**: Both Gemini and gpt-4o-mini are called with explicit JSON schemas to avoid free-form output.
- **macOS scheduling**: `com.japanese.pipeline.plist` is a launchd agent for daily 3:00 AM runs.
