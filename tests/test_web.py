import json
import pytest
from unittest.mock import patch
from pathlib import Path
from web.app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with patch("web.app.db_available", return_value=False), \
         patch("web.auth.db_available", return_value=False):
        with app.test_client() as client:
            yield client

def test_subscriptions_page(client, tmp_path):
    # Mock SOURCES_FILE
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": [{"name": "Test", "url": "http://test.com"}]}))
    
    with patch("web.app.SOURCES_FILE", mock_sources):
        rv = client.get('/subscriptions')
        assert rv.status_code == 200
        assert b"Test" in rv.data
        assert b"http://test.com" in rv.data


def test_inbox_navigation_is_active_and_mobile_friendly(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": []}))

    with patch("web.app.SOURCES_FILE", mock_sources):
        body = client.get("/subscriptions").get_data(as_text=True)

    assert '<title>Listening inbox — Mimichan</title>' in body
    assert '>Inbox</a>' in body
    assert 'id="tab-subs"' in body
    assert 'mobile-nav-active' in body
    assert 'aria-current="page"' in body
    assert 'Learning Japanese through Listening Practices' not in body


def test_episode_page_exposes_shortcut_help(client, tmp_path):
    (tmp_path / "2026-01-01").mkdir()
    with patch("web.app.EPISODES_DIR", tmp_path):
        body = client.get("/episode/2026-01-01").get_data(as_text=True)

    assert '<title>2026-01-01 — Mimichan</title>' in body
    assert 'id="btn-shortcuts"' in body
    assert 'id="shortcuts-modal"' in body
    assert 'aria-modal="true"' in body


def test_review_page_has_mobile_safe_rating_controls(client):
    body = client.get("/review").get_data(as_text=True)

    assert '<title>Daily Review — Mimichan</title>' in body
    assert 'class="review-actions' in body
    assert 'data-rating="again"' in body
    assert 'data-rating="hard"' in body
    assert 'data-rating="good"' in body
    assert 'id="tab-vocab"' in body
    assert 'mobile-nav-active' in body

def test_subscriptions_add(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": []}))
    
    with patch("web.app.SOURCES_FILE", mock_sources):
        rv = client.post('/subscriptions/add', data={
            'name': 'New Source',
            'url': 'http://new.com',
            'description': 'Description'
        }, follow_redirects=True)
        
        assert rv.status_code == 200
        assert b"New Source" in rv.data
        
        data = json.loads(mock_sources.read_text())
        assert len(data['sources']) == 1
        assert data['sources'][0]['name'] == 'New Source'

def test_subscriptions_delete(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": [{"name": "To Delete", "url": "http://delete.com"}]}))
    
    with patch("web.app.SOURCES_FILE", mock_sources):
        rv = client.post('/subscriptions/delete', data={
            'url': 'http://delete.com'
        }, follow_redirects=True)
        
        assert rv.status_code == 200
        assert b"To Delete" not in rv.data
        
        data = json.loads(mock_sources.read_text())
        assert len(data['sources']) == 0


def test_recommendation_subscribe_uses_catalog_values_and_defaults(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": []}))
    with patch("web.app.SOURCES_FILE", mock_sources):
        rv = client.post("/subscriptions/recommendations/subscribe", data={
            "candidate_id": "yt-yuru-language",
            "name": "Injected name",
            "url": "https://evil.example",
        })
        assert rv.status_code == 302
        source = json.loads(mock_sources.read_text())["sources"][0]
        assert source["name"] == "ゆる言語学ラジオ"
        assert source["url"] == "https://www.youtube.com/@yurugengogaku"
        assert source["enabled"] is True
        assert source["digest_enabled"] is True


def test_recommendation_subscribe_rejects_duplicate_and_unknown(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": [{
        "name": "Existing", "url": "https://www.youtube.com/@yurugengogaku/"
    }]}))
    with patch("web.app.SOURCES_FILE", mock_sources):
        duplicate = client.post("/subscriptions/recommendations/subscribe",
                                data={"candidate_id": "yt-yuru-language"})
        unknown = client.post("/subscriptions/recommendations/subscribe",
                              data={"candidate_id": "does-not-exist"})
    assert duplicate.status_code == 409
    assert unknown.status_code == 404


def test_recommendation_cards_render_before_source_grid(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": [{"name": "Test", "url": "http://test.com"}]}))
    with patch("web.app.SOURCES_FILE", mock_sources):
        body = client.get("/subscriptions").get_data(as_text=True)
    # "http://test.com" also appears earlier, as the value of the inbox's
    # source-filter <option> — that's the listening-inbox UI (merged in from
    # dmit-hk's production redesign), not the source grid this test targets,
    # so only the recommendations-before-source-grid ordering is asserted here.
    assert body.index("Recommended for you") < body.rindex("Your sources") < body.rindex("http://test.com")
    assert 'class="recommendation-rail"' in body

@patch("lib.analyzer.explain_sentence")
def test_api_explain(mock_explain, client):
    mock_explain.return_value = "Detailed explanation."
    
    rv = client.post('/api/explain', json={'text': 'Hello'})
    assert rv.status_code == 200
    assert rv.get_json()['explanation'] == "Detailed explanation."
    mock_explain.assert_called_once_with("Hello")
