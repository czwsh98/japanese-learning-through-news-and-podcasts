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
    assert body.index("Recommended for you") < body.index("Your sources") < body.index("http://test.com")
    assert 'class="recommendation-rail"' in body

@patch("lib.analyzer.explain_sentence")
def test_api_explain(mock_explain, client):
    mock_explain.return_value = "Detailed explanation."
    
    rv = client.post('/api/explain', json={'text': 'Hello'})
    assert rv.status_code == 200
    assert rv.get_json()['explanation'] == "Detailed explanation."
    mock_explain.assert_called_once_with("Hello")
