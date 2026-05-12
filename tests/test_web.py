import json
import pytest
from unittest.mock import patch
from pathlib import Path
from web.app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
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

@patch("lib.analyzer.explain_sentence")
def test_api_explain(mock_explain, client):
    mock_explain.return_value = "Detailed explanation."
    
    rv = client.post('/api/explain', json={'text': 'Hello'})
    assert rv.status_code == 200
    assert rv.get_json()['explanation'] == "Detailed explanation."
    mock_explain.assert_called_once_with("Hello")
