import json

import mimichan_bot
from mimichan_bot import _normalized_published_at, _parse_duration, _rank_candidates


def test_parse_duration_formats():
    assert _parse_duration("01:02:03") == 3723
    assert _parse_duration("42:15") == 2535
    assert _parse_duration("123.4") == 123
    assert _parse_duration(90) == 90
    assert _parse_duration("bad") == 0


def test_normalized_publication_dates_and_fallbacks():
    assert _normalized_published_at("Wed, 30 Jul 2026 12:34:56 +0000") == "2026-07-30T12:34:56Z"
    assert _normalized_published_at("20260730") == "2026-07-30T00:00:00Z"
    assert _normalized_published_at(1785414896).endswith("Z")
    assert _normalized_published_at("not-a-date") == ""


def test_rank_candidates_rotates_sources_without_back_to_back_repeats():
    candidates = [
        {
            "url": f"episode-{source}",
            "source_url": f"source-{source}",
            "source_order": source,
            "item_order": 0,
        }
        for source in range(4)
    ]
    state = {}
    previous_sources = set()
    for day in range(3):
        picks = _rank_candidates(candidates, state)
        picked_sources = {pick["source_url"] for pick in picks}
        assert len(picks) == 2
        assert len(picked_sources) == 2
        assert picked_sources.isdisjoint(previous_sources)
        for source_url in picked_sources:
            state[source_url] = f"2026-08-0{day + 1}T10:00:00+00:00"
        previous_sources = picked_sources


def test_rank_candidates_prefers_newest_item_within_source():
    candidates = [
        {"url": "new", "source_url": "a", "source_order": 0, "item_order": 0},
        {"url": "old", "source_url": "a", "source_order": 0, "item_order": 1},
        {"url": "other", "source_url": "b", "source_order": 1, "item_order": 0},
    ]
    assert [item["url"] for item in _rank_candidates(candidates, {}, limit=2)] == ["new", "other"]


def test_fetch_all_recent_skips_paused_sources(tmp_path, monkeypatch):
    source_file = tmp_path / "sources.json"
    source_file.write_text(json.dumps({"sources": [
        {"name": "legacy", "url": "legacy"},
        {"name": "paused", "url": "paused", "enabled": False},
    ]}))
    fetched = []
    monkeypatch.setattr(mimichan_bot, "SOURCES_FILE", str(source_file))
    monkeypatch.setattr(mimichan_bot, "fetch_recent", lambda source, limit=5: fetched.append(source["url"]) or [{"url": source["url"] + "/ep"}])
    sources, recent = mimichan_bot.fetch_all_recent()
    assert [source["url"] for source in sources] == ["legacy", "paused"]
    assert fetched == ["legacy"]
    assert list(recent) == ["legacy"]


def test_digest_excludes_digest_disabled_but_keeps_legacy_sources(monkeypatch):
    sent = []
    monkeypatch.setattr(mimichan_bot, "get_known_urls", lambda: set())
    monkeypatch.setattr(mimichan_bot, "_unfinished_episodes", lambda owner: [])
    monkeypatch.setattr(mimichan_bot, "_expiring_episodes", lambda owner: [])
    monkeypatch.setattr(mimichan_bot, "_load_digest_state", lambda: {})
    monkeypatch.setattr(mimichan_bot, "store_ep", lambda *args: "key")
    monkeypatch.setattr(mimichan_bot, "tg", lambda *args, **kwargs: sent.append(kwargs.get("text", "")) or {"ok": False})
    sources = [
        {"name": "legacy", "url": "legacy"},
        {"name": "inbox-only", "url": "inbox", "digest_enabled": False},
        {"name": "paused", "url": "paused", "enabled": False},
    ]
    recent = {source["url"]: [{"url": source["url"] + "/ep", "title": source["name"], "channel": source["name"]}] for source in sources}
    result = mimichan_bot.do_digest("token", "chat", "owner@example.com", sources, recent)
    assert result.startswith("Sent 1 pick")
    assert "legacy" in sent[0]
    assert "inbox-only" not in sent[0]
    assert "paused" not in sent[0]
