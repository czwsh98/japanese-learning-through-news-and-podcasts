import json
from datetime import date
from types import SimpleNamespace

from web.recommendations import (
    build_interest_profile,
    load_catalog,
    normalize_url,
    rank_recommendations,
)


def _candidate(candidate_id, kind, tags, url=None, related=None):
    return {
        "id": candidate_id,
        "name": candidate_id,
        "type": kind,
        "url": url or f"https://example.com/{candidate_id}",
        "rss_url": f"https://feeds.example.com/{candidate_id}" if kind == "podcast" else None,
        "description": "A sufficiently useful description.",
        "tags": tags,
        "related_sources": related or [],
    }


def test_catalog_is_versioned_valid_and_balanced():
    catalog = load_catalog()
    assert len(catalog) == 24
    assert sum(c["type"] == "podcast" for c in catalog) == 12
    assert sum(c["type"] == "youtube" for c in catalog) == 12
    assert len({c["id"] for c in catalog}) == len(catalog)
    assert len({normalize_url(c["url"]) for c in catalog}) == len(catalog)
    by_id = {candidate["id"]: candidate for candidate in catalog}
    for candidate_id in ("pod-nikkei", "pod-news-connect", "yt-nikkei", "yt-tvtokyo-biz"):
        assert by_id[candidate_id]["image_url"].startswith("https://")
    assert by_id["pod-nikkei"]["url"].endswith("id1627014612")
    assert by_id["pod-nikkei"]["rss_url"] == "https://feeds.megaphone.fm/nagara"
    assert by_id["pod-news-connect"]["rss_url"] == "https://anchor.fm/s/81fb5eec/podcast/rss"


def test_loader_skips_malformed_and_duplicate_records(tmp_path):
    good = _candidate("one", "youtube", ["news", "society"])
    duplicate = {**good, "id": "two"}
    malformed = {"id": "bad"}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "sources": [good, duplicate, malformed]}))
    assert [item["id"] for item in load_catalog(path)] == ["one"]


def test_url_normalization_removes_cosmetic_differences():
    assert normalize_url("HTTPS://WWW.YouTube.com/@Example/?utm_source=x#top") == \
           normalize_url("https://youtube.com/@Example")


def test_profile_weights_processed_playback_and_completion():
    catalog = [_candidate("pivot", "youtube", ["economics", "interviews"],
                          url="https://youtube.com/@pivot")]
    source = {"name": "pivot", "url": "https://youtube.com/@pivot", "enabled": True}
    episode = SimpleNamespace(id="ep-1", channel="pivot", url="https://youtube.com/watch?v=abcdefghijk")
    weights, evidence = build_interest_profile(
        catalog, [source], [episode], {"ep-1": {"percent": .8, "finished": True}}
    )
    # Active source (2) + processed episode (1) + 80% playback (2.4) + completion (6).
    assert weights["economics"] == 11.4
    assert evidence["pivot"] == {"active": 1, "episodes": 1, "playback": .8, "finished": 1}


def test_ranking_is_weekly_stable_balanced_diverse_and_excludes_existing():
    catalog = [
        _candidate("p1", "podcast", ["news", "society"]),
        _candidate("p2", "podcast", ["news", "economics"]),
        _candidate("p3", "podcast", ["science", "technology"]),
        _candidate("y1", "youtube", ["news", "society"]),
        _candidate("y2", "youtube", ["history", "culture"]),
        _candidate("y3", "youtube", ["language", "culture"]),
    ]
    sources = [{"name": "p1", "url": catalog[0]["url"]}]
    args = (catalog, sources, [], {}, set(), "user-a", date(2026, 8, 2))
    first = rank_recommendations(*args)
    second = rank_recommendations(*args)
    assert [c["id"] for c in first] == [c["id"] for c in second]
    assert "p1" not in {c["id"] for c in first}
    assert sum(c["type"] == "podcast" for c in first) == 2
    assert sum(c["type"] == "youtube" for c in first) == 2
    assert len({tag for c in first for tag in c["tags"]}) >= 5


def test_sparse_catalog_fills_from_available_type_and_dismisses_id():
    catalog = [_candidate(f"y{i}", "youtube", [f"topic{i}", "culture"]) for i in range(5)]
    result = rank_recommendations(catalog, [], [], {}, {"y0"}, "user-a", date(2026, 8, 2))
    assert len(result) == 4
    assert all(c["type"] == "youtube" for c in result)
    assert "y0" not in {c["id"] for c in result}
