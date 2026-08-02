# CLAUDE.md

This file provides guidance to Claude when working with code in this repository.

## What this project is

A personal automation pipeline that downloads Japanese YouTube videos/podcasts, transcribes them with Whisper, translates segments with Gemini Flash, grades vocabulary against a real JLPT word bank and extracts grammar/expressions with an LLM, and serves the results through a Flask web UI with an audio player synced to the transcript. Users can save individual words and phrases to a persistent cross-episode Vocab Bank.

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

No build step. Tests are in `tests/` (pytest). Dependencies are in `requirements.txt`; install with `pip install -r requirements.txt`. Requires a `.env` file with `OPENAI_API_KEY` and `GEMINI_API_KEY` (see `.env.example`).

## Architecture

### Pipeline flow

```
Input (URL or sources.json)
  → lib/downloader.py   — yt-dlp / Apple Podcasts / RSS / direct audio
  → lib/transcriber.py  — Whisper API (auto-chunks files >23 MB via ffmpeg)
  → lib/translator.py   — Gemini Flash (50 segments/call, EN + ZH)
  → lib/analyzer.py     — vocabulary graded via lib/jlpt_bank.py (real JLPT word list);
                          LLM curates which graded words to keep + extracts grammar/expressions
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

### Vocab Bank (`vocab.json`)

A single JSON file at the project root (configurable via `VOCAB_FILE` env var) that stores words saved by the user across all episodes. Schema per item: `id`, `word`, `reading`, `en`, `zh`, `example`, `level`, `type`, `source_episode`, `saved_at`.

- Deduplicated by surface form (`word`) on write
- All reads and writes go through `_vocab_lock` (threading.Lock) to prevent concurrent corruption
- Exposed via REST API in `web/app.py`: `GET/POST /api/vocab`, `DELETE /api/vocab/<id>`, `GET /vocab/export.csv`
- The episode player fetches the saved-word set on load and marks already-saved cards immediately

### JLPT levels

The `--level` flag (or `jlpt_level` in meta.json) controls what the analyzer targets. The `tiers` list for each level MUST match `LEVEL_TIERS` in `web/static/js/player.js` — the UI filters vocab/grammar to those tiers, so a mismatch makes a tier invisible even if the analyzer found it:
- `beginner`: N5
- `beginner-intermediate`: N4, N3
- `intermediate`: N3
- `intermediate-advanced`: N2
- `advanced`: N2, N1
- Plus `context-specific` for domain/literary/specialized terms

The web UI color-codes highlights: N5 green → N4 teal → N3 blue → N2 amber → N1 rose → context-specific violet.

**Vocabulary levels come from `lib/jlpt_bank.py`** (a compiled JLPT word list, see `data/README.md`), not from LLM grading — the LLM was asked to grade vocabulary in two earlier attempts and never did it reliably (it graded almost everything at whichever tier was requested, regardless of true difficulty). The bank is a deterministic lookup; the LLM only curates which bank-graded candidates are worth a flashcard and writes their Chinese gloss. **Grammar pattern levels are still LLM-graded** — no equivalent open grammar-level dataset is vendored yet, so grammar levels carry the same reliability caveat future work should address.

### Web UI (`web/`)

- `web/app.py` — Flask routes + background job threading + vocab bank API
- `web/templates/` — Jinja2: `base.html`, `index.html` (episode browser), `episode.html` (player + transcript), `vocab.html` (vocab bank), `upload.html`, `subscriptions.html`, `job.html` (pipeline status)
- `web/static/` — CSS and the audio player JS (transcript sync, JLPT highlight tooltips, translation panel, mobile FAB, vocab save buttons)

The episode player JS handles: click-to-seek on transcript lines, auto-follow during playback, inline JLPT hover tooltips, translation/grammar side panel, vocab save buttons (POST to `/api/vocab`), and a mobile floating action button for toggling translations.

### Key implementation details

- **Chunking**: `transcriber.py` splits audio >23 MB into 15-minute chunks with ffmpeg, transcribes each, then merges segments while preserving timestamps.
- **Hallucination filtering**: 4-layer filter in `transcriber.py` (empty/short, character loops, phrase loops, cross-segment loops).
- **Structured output**: Both Gemini and gpt-4o-mini are called with explicit JSON schemas to avoid free-form output.
- **Defensive card writing**: `writer.py` uses `.get()` for all LLM-returned fields and skips cards missing required fields (word + translation) with a warning rather than crashing.
- **Cache headers**: Episode JSON endpoints (`transcript`, `analysis`, `meta`) use `Cache-Control: max-age=300, must-revalidate` — not `immutable` — so re-analyzed episodes serve fresh data within 5 minutes.
- **Vocab locking**: `_vocab_lock` (threading.Lock) guards all reads and writes to `vocab.json` in `web/app.py`.
- **macOS scheduling**: `com.japanese.pipeline.plist` is a launchd agent for daily 3:00 AM runs.

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
