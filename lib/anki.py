"""Push cards to Anki via AnkiConnect. Non-blocking — logs success/failure only."""
import logging
import os

import requests

log = logging.getLogger(__name__)

_URL = os.environ.get("ANKI_CONNECT_URL", "http://localhost:8765")
_DECK = "Japanese::Pipeline"
_NOTE_TYPE = "Basic"
_TIMEOUT = 10


def push_to_anki(analysis: dict, episode_date: str) -> None:
    try:
        _ensure_deck()
        notes = _build_notes(analysis, episode_date)
        if not notes:
            log.info("AnkiConnect: no cards to push")
            return
        results = _invoke("addNotes", {"notes": notes})
        added = sum(1 for r in results if r is not None)
        skipped = len(results) - added
        log.info(f"AnkiConnect: {added} added, {skipped} skipped (duplicates)")
    except requests.exceptions.ConnectionError:
        log.warning("AnkiConnect: Anki not running — skipping push (cards saved to cards.csv)")
    except Exception as exc:
        log.warning(f"AnkiConnect: push failed — {exc}")


def _ensure_deck() -> None:
    decks = _invoke("deckNames")
    if _DECK not in decks:
        _invoke("createDeck", {"deck": _DECK})
        log.info(f"AnkiConnect: created deck {_DECK!r}")


def _build_notes(analysis: dict, episode_date: str) -> list[dict]:
    tags = ["japanese", "pipeline", f"episode::{episode_date}"]
    notes = []

    for v in analysis.get("vocab", []):
        notes.append({
            "deckName": _DECK,
            "modelName": _NOTE_TYPE,
            "fields": {
                "Front": f"{v['word']}（{v['reading']}）",
                "Back": (
                    f"<b>{v['en']}</b><br>{v['zh']}"
                    + (f"<br><i>{v['example']}</i>" if v.get("example") else "")
                ),
            },
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
            "tags": tags + [v.get("level", ""), "vocab"],
        })

    for g in analysis.get("grammar", []):
        notes.append({
            "deckName": _DECK,
            "modelName": _NOTE_TYPE,
            "fields": {
                "Front": g["pattern"],
                "Back": (
                    f"<b>{g['meaning_en']}</b><br>{g['meaning_zh']}"
                    + (f"<br><small>{g['construction']}</small>" if g.get("construction") else "")
                    + (f"<br><i>{g['example']}</i>" if g.get("example") else "")
                ),
            },
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
            "tags": tags + [g.get("level", ""), "grammar"],
        })

    return notes


def _invoke(action: str, params: dict | None = None):
    payload: dict = {"action": action, "version": 6}
    if params:
        payload["params"] = params
    resp = requests.post(_URL, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data["result"]
