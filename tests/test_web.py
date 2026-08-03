import json
import pytest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from web.app import app, _extract_page_image, _extract_rss_image

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
    assert 'id="btn-shadow-mobile"' in body
    assert 'id="shadow-sheet"' in body
    assert 'id="shadow-bar"' in body
    assert "No microphone or recording" in body


def test_today_and_library_have_distinct_navigation(client):
    today = client.get("/").get_data(as_text=True)
    library = client.get("/episodes").get_data(as_text=True)

    assert "Today — Mimichan" in today
    assert "Your learning queue" in today
    assert "Library — Mimichan" in library
    assert ">Library</h1>" in library
    assert 'id="tab-home"' in today


def test_review_page_has_mobile_safe_rating_controls(client):
    body = client.get("/review").get_data(as_text=True)

    assert '<title>Daily Review — Mimichan</title>' in body
    assert 'class="review-actions' in body
    assert 'data-rating="again"' in body
    assert 'data-rating="hard"' in body
    assert 'data-rating="good"' in body
    assert 'id="tab-vocab"' in body
    assert 'mobile-nav-active' in body


def test_activity_page_uses_upload_mobile_tab(client):
    body = client.get("/activity").get_data(as_text=True)

    assert '<title>Processing Activity — Mimichan</title>' in body
    assert 'id="activity-list"' in body
    assert 'id="tab-upload"' in body
    assert 'mobile-nav-active' in body

def test_subscriptions_add(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": []}))
    
    with patch("web.app.SOURCES_FILE", mock_sources), \
         patch("web.app._resolve_source_metadata", return_value={
             "rss_url": "https://feeds.example/new.xml",
             "image_url": "https://images.example/new.jpg",
         }):
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
        assert data['sources'][0]['rss_url'] == 'https://feeds.example/new.xml'
        assert data['sources'][0]['image_url'] == 'https://images.example/new.jpg'

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
    with patch("web.app.SOURCES_FILE", mock_sources), \
         patch("web.app._resolve_source_metadata", return_value={
             "image_url": "https://images.example/yuru.jpg",
         }):
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
        assert source["image_url"] == "https://images.example/yuru.jpg"


def test_source_artwork_extractors_accept_page_and_rss_metadata():
    assert _extract_page_image(
        '<meta property="og:image" content="https://images.example/channel.jpg">'
    ) == "https://images.example/channel.jpg"
    rss = b'''<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel><itunes:image href="https://images.example/show.jpg" /></channel>
    </rss>'''
    assert _extract_rss_image(rss) == "https://images.example/show.jpg"


def test_recommendation_card_renders_cover_with_icon_fallback(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": []}))
    recommendation = {
        "id": "cover-test", "name": "Cover Test", "type": "podcast",
        "url": "https://example.com/show", "description": "Description",
        "tags": ["news", "culture"], "reason": "A useful source",
        "initials": "CT", "image_url": "https://images.example/cover.jpg",
    }
    with patch("web.app.SOURCES_FILE", mock_sources), \
         patch("web.app._recommendations_for_user", return_value=([recommendation], True)):
        body = client.get("/subscriptions").get_data(as_text=True)
    assert 'src="https://images.example/cover.jpg"' in body
    assert 'class="source-artwork ' in body
    assert 'onerror="this.remove()"' in body


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


def test_inbox_and_source_management_have_distinct_routes(client, tmp_path):
    mock_sources = tmp_path / "sources.json"
    mock_sources.write_text(json.dumps({"sources": [{"name": "Test", "url": "http://test.com"}]}))
    with patch("web.app.SOURCES_FILE", mock_sources):
        inbox = client.get("/subscriptions").get_data(as_text=True)
        sources = client.get("/sources").get_data(as_text=True)
    assert "Recommended for you" in inbox
    assert "Your sources" not in inbox
    assert "Manage sources" in inbox
    assert "Manage listening sources" in sources
    assert "Your sources" in sources
    assert "http://test.com" in sources
    assert "Recommended for you" not in sources
    assert 'class="recommendation-rail"' in inbox

@patch("lib.analyzer.explain_sentence")
def test_api_explain(mock_explain, client):
    mock_explain.return_value = "Detailed explanation."
    
    rv = client.post('/api/explain', json={'text': 'Hello'})
    assert rv.status_code == 200
    assert rv.get_json()['explanation'] == "Detailed explanation."
    assert rv.get_json()['cached'] is False
    mock_explain.assert_called_once_with("Hello")


@patch("lib.analyzer.explain_sentence")
def test_api_explain_returns_persistent_cache_hit_without_api_call(mock_explain, client):
    cached = SimpleNamespace(
        explanation="Cached explanation.", hit_count=2, last_used_at=None,
    )

    class FakeSession:
        def get(self, model, key):
            return cached

    @contextmanager
    def fake_get_db():
        yield FakeSession()

    with patch("web.app.db_available", return_value=True), \
         patch("web.app.get_db", fake_get_db):
        rv = client.post('/api/explain', json={'text': 'これはテストです。'})

    assert rv.status_code == 200
    assert rv.get_json() == {"explanation": "Cached explanation.", "cached": True}
    assert cached.hit_count == 3
    assert cached.last_used_at is not None
    mock_explain.assert_not_called()
