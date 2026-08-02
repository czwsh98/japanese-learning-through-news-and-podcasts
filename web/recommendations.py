"""Curated, local-only source recommendations and deterministic ranking."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)
CATALOG_FILE = Path(__file__).with_name("recommendation_catalog.v1.json")
REQUIRED = {"id", "name", "type", "url", "description", "tags", "related_sources"}


def normalize_url(value: str) -> str:
    """Canonical form used only for source identity and duplicate prevention."""
    value = (value or "").strip()
    try:
        parts = urlsplit(value if "://" in value.lower() else "https://" + value)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = f":{parts.port}" if parts.port else ""
        path = parts.path.rstrip("/") or ""
        # Tracking parameters do not make a distinct subscription.
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                           if not k.lower().startswith("utm_") and k.lower() not in {"ref", "feature"}])
        return urlunsplit(((parts.scheme or "https").lower(), host + port, path, query, ""))
    except (TypeError, ValueError):
        return value.rstrip("/").lower()


def load_catalog(path: Path = CATALOG_FILE) -> list[dict]:
    """Load valid records; a bad record never prevents the page from rendering."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("sources"), list):
            raise ValueError("unsupported or missing catalog version")
    except Exception as exc:
        log.error("Recommendation catalog unavailable: %s", exc)
        return []

    valid, ids, urls = [], set(), set()
    for index, item in enumerate(payload["sources"]):
        try:
            if not isinstance(item, dict) or not REQUIRED.issubset(item):
                raise ValueError("missing required fields")
            if item["type"] not in {"podcast", "youtube"}:
                raise ValueError("invalid type")
            if not all(isinstance(item[k], str) and item[k].strip()
                       for k in ("id", "name", "url", "description")):
                raise ValueError("blank or non-string field")
            if not isinstance(item["tags"], list) or len(item["tags"]) < 2:
                raise ValueError("at least two tags required")
            if not isinstance(item["related_sources"], list):
                raise ValueError("related_sources must be a list")
            if item["type"] == "podcast" and not item.get("rss_url"):
                raise ValueError("podcast is missing resolved RSS URL")
            normalized = normalize_url(item["url"])
            if item["id"] in ids or normalized in urls:
                raise ValueError("duplicate id or URL")
            ids.add(item["id"]); urls.add(normalized)
            valid.append(item)
        except Exception as exc:
            log.error("Ignoring recommendation catalog record %s: %s", index, exc)
    return valid


def build_interest_profile(catalog: list[dict], subscriptions: list[dict],
                           episodes: list, playback: dict) -> tuple[dict, dict]:
    """Return topic weights and evidence keyed by catalog id."""
    weights = defaultdict(float)
    evidence = defaultdict(lambda: {"active": 0, "episodes": 0, "playback": 0.0, "finished": 0})
    active = [s for s in subscriptions if s.get("enabled", True)]

    def match(url: str, name: str):
        nu, nn = normalize_url(url), (name or "").casefold().strip()
        for item in catalog:
            names = [item["name"], *item.get("related_sources", [])]
            if nu and nu in {normalize_url(item["url"]), normalize_url(item.get("rss_url", ""))}:
                yield item
            elif nn and any(nn == n.casefold().strip() for n in names):
                yield item

    for source in active:
        for item in match(source.get("url", ""), source.get("name", "")):
            evidence[item["id"]]["active"] += 1
            for tag in item["tags"]:
                weights[tag] += 2.0
    for episode in episodes:
        matches = list(match(getattr(episode, "url", ""), getattr(episode, "channel", "")))
        state = playback.get(str(getattr(episode, "id", "")), {})
        pct = max(0.0, min(1.0, float(state.get("percent", 0) or 0)))
        finished = bool(state.get("finished"))
        for item in matches:
            ev = evidence[item["id"]]
            ev["episodes"] += 1; ev["playback"] += pct; ev["finished"] += int(finished)
            for tag in item["tags"]:
                weights[tag] += 1.0 + pct * 3.0 + (6.0 if finished else 0.0)
    return dict(weights), dict(evidence)


def rank_recommendations(catalog: list[dict], subscriptions: list[dict], episodes: list,
                         playback: dict, dismissed_ids: set[str], user_id: str,
                         today: date | None = None, limit: int = 4) -> list[dict]:
    """Rank locally with weekly stable jitter, type balance, and topic diversity."""
    subscribed = {normalize_url(s.get("url", "")) for s in subscriptions}
    eligible = [c for c in catalog if c["id"] not in dismissed_ids
                and normalize_url(c["url"]) not in subscribed
                and normalize_url(c.get("rss_url", "")) not in subscribed]
    if not eligible:
        return []
    weights, evidence = build_interest_profile(catalog, subscriptions, episodes, playback)
    iso = (today or date.today()).isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    active_names = {s.get("name", "").casefold() for s in subscriptions if s.get("enabled", True)}
    for item in eligible:
        relevance = sum(weights.get(tag, 0.0) for tag in item["tags"])
        related = next((name for name in item.get("related_sources", [])
                        if name.casefold() in active_names), None)
        jitter_bytes = hashlib.sha256(f"{user_id}:{week_key}:{item['id']}".encode()).digest()[:4]
        item = item.copy()
        item["_score"] = relevance + int.from_bytes(jitter_bytes, "big") / 2**32
        if related:
            item["reason"] = f"Because you listen to {related}"
        elif relevance and item["tags"]:
            item["reason"] = f"More {item['tags'][0]} for your listening mix"
        else:
            item["reason"] = f"Adds {item['tags'][0]} to your listening mix"
        item["initials"] = "".join(part[0] for part in item["name"].split()[:2]).upper() or item["name"][:1]
        item["evidence"] = evidence.get(item["id"], {})
        item["week"] = week_key
        eligible[eligible.index(next(c for c in eligible if c["id"] == item["id"]))] = item

    selected: list[dict] = []
    def pick(kind: str | None):
        choices = [c for c in eligible if c not in selected and (kind is None or c["type"] == kind)]
        if not choices:
            return False
        def adjusted(c):
            overlap = max((len(set(c["tags"]) & set(s["tags"])) for s in selected), default=0)
            return c["_score"] - overlap * 2.5
        selected.append(max(choices, key=adjusted))
        return True
    for kind in ("podcast", "youtube", "podcast", "youtube"):
        if len(selected) < limit:
            pick(kind)
    while len(selected) < limit and pick(None):
        pass
    for item in selected:
        item.pop("_score", None)
    return selected
