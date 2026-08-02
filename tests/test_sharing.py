import os
import json
import uuid
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from sqlalchemy import select

# Setup environment variables before imports to initialize the app in SQLite and dummy R2 mode
DB_PATH = "test_sharing.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["R2_ENDPOINT_URL"] = "https://mock-r2.com"
os.environ["R2_ACCESS_KEY_ID"] = "mock-key"
os.environ["R2_SECRET_ACCESS_KEY"] = "mock-secret"
os.environ["R2_BUCKET"] = "mock-bucket"
os.environ["SECRET_KEY"] = "test-secret-key-12345"

# Mock dotenv.load_dotenv to prevent it from reading the actual .env file and overwriting sqlite configuration
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: None

from web.app import app, _get_source_token, _pipeline_thread, _jobs, _jobs_lock
from web.db import (
    get_db, User, Episode, PlaybackProgress, RecommendationDismissal,
    TranscriptionUsage, VocabItem, VocabOccurrence,
)

@pytest.fixture(autouse=True)
def setup_db():
    # Force database recreation for each test case using the file-based SQLite
    db_file = Path(DB_PATH)
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass
            
    # Force fresh engine / connection initialization
    from web.db import init_db, Base, _engine
    init_db()
    Base.metadata.create_all(_engine)
    
    yield
    
    # Cleanup DB file after test runs
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass

@pytest.fixture
def test_users():
    # Create two dummy users in SQLite
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    with get_db() as db:
        user_a = User(
            id=user_a_id,
            email="usera@example.com",
            password_hash="pbkdf2:sha256:...",
            is_admin=False,
            transcription_limit=3
        )
        user_b = User(
            id=user_b_id,
            email="userb@example.com",
            password_hash="pbkdf2:sha256:...",
            is_admin=False,
            transcription_limit=3
        )
        db.add(user_a)
        db.add(user_b)
    return user_a_id, user_b_id

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_get_source_token_youtube():
    # Test different YouTube formats
    assert _get_source_token("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert _get_source_token("https://youtu.be/dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert _get_source_token("https://youtube.com/watch?v=dQw4w9WgXcQ&feature=share") == "youtube:dQw4w9WgXcQ"
    assert _get_source_token("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert _get_source_token(None) is None
    assert _get_source_token("   ") is None

def test_get_source_token_general_url():
    # Test standard URL normalization (ignores query params/fragments, lowercases host/scheme)
    url1 = "HTTPS://Example.Com/podcast/Ep1.mp3?token=abc#sec1"
    url2 = "https://example.com/podcast/Ep1.mp3"
    token1 = _get_source_token(url1)
    token2 = _get_source_token(url2)
    assert token1 is not None
    assert token1.startswith("url:")
    assert token1 == token2


@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
def test_recommendation_dismissal_is_idempotent_and_per_user(mock_auth_user, mock_app_user,
                                                              client, test_users):
    user_a_id, user_b_id = test_users
    for user_id in (user_a_id, user_a_id, user_b_id):
        with get_db() as db:
            user = db.get(User, user_id)
        mock_auth_user.return_value = user
        mock_app_user.return_value = user
        response = client.post("/subscriptions/recommendations/dismiss",
                               data={"candidate_id": "yt-yuru-language"})
        assert response.status_code == 302
    with get_db() as db:
        rows = db.execute(select(RecommendationDismissal)).scalars().all()
    assert {(row.user_id, row.candidate_id) for row in rows} == {
        (user_a_id, "yt-yuru-language"), (user_b_id, "yt-yuru-language")
    }


@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
def test_playback_progress_keeps_high_water_mark_and_completion(mock_auth_user, mock_app_user,
                                                                client, test_users):
    user_a_id, _ = test_users
    with get_db() as db:
        user = db.get(User, user_a_id)
        episode = Episode(owner_user_id=user_a_id, slug="2026-08-02", date="2026-08-02",
                          title="Test", channel="PIVOT 公式チャンネル")
        db.add(episode)
    mock_auth_user.return_value = user
    mock_app_user.return_value = user
    assert client.post("/api/playback", json={
        "episode": "2026-08-02", "current_time": 50, "duration": 100
    }).status_code == 200
    assert client.post("/api/playback", json={
        "episode": "2026-08-02", "current_time": 100, "duration": 100, "finished": True
    }).status_code == 200
    assert client.post("/api/playback", json={
        "episode": "2026-08-02", "current_time": 10, "duration": 100
    }).status_code == 200
    with get_db() as db:
        row = db.execute(select(PlaybackProgress).where(
            PlaybackProgress.user_id == user_a_id
        )).scalar_one()
    assert row.percent == 100
    assert row.finished is True


@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
def test_vocab_keeps_multiple_exact_source_occurrences(mock_auth_user, mock_app_user,
                                                        client, test_users):
    user_a_id, _ = test_users
    with get_db() as db:
        user = db.get(User, user_a_id)
        db.add(Episode(
            owner_user_id=user_a_id,
            slug="2026-08-02",
            date="2026-08-02",
            title="Source title from the server",
            r2_prefix="episodes/test/",
        ))
    mock_auth_user.return_value = user
    mock_app_user.return_value = user

    payload = {
        "front": "経済",
        "reading": "けいざい",
        "en": "economy",
        "type": "vocab",
        "source_episode": "2026-08-02",
        "source_segment_index": 3,
        "source_start": 42.5,
        "source_end": 48.0,
        "source_text": "日本の経済について話します。",
        "source_en": "We will discuss Japan's economy.",
    }
    first = client.post("/api/vocab", json=payload)
    duplicate = client.post("/api/vocab", json=payload)
    second_context = client.post("/api/vocab", json={**payload, "source_start": 90.0})

    assert first.status_code == 201
    assert first.get_json()["status"] == "success"
    assert duplicate.get_json()["status"] == "exists"
    assert second_context.get_json()["status"] == "occurrence_added"

    items = client.get("/api/vocab").get_json()
    assert len(items) == 1
    assert len(items[0]["occurrences"]) == 2
    assert items[0]["occurrences"][0]["episode_title"] == "Source title from the server"
    assert {o["start_time"] for o in items[0]["occurrences"]} == {42.5, 90.0}

    with get_db() as db:
        assert len(db.execute(select(VocabItem)).scalars().all()) == 1
        assert len(db.execute(select(VocabOccurrence)).scalars().all()) == 2


@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
def test_daily_review_schedules_and_undoes_latest_answer(mock_auth_user, mock_app_user,
                                                         client, test_users):
    user_a_id, _ = test_users
    with get_db() as db:
        user = db.get(User, user_a_id)
        item = VocabItem(
            user_id=user_a_id,
            word="〜わけではない",
            en="it does not mean that",
            type="grammar",
        )
        db.add(item)
        db.flush()
        item_id = str(item.id)
    mock_auth_user.return_value = user
    mock_app_user.return_value = user

    with get_db() as db:
        stored = db.execute(select(VocabItem).where(VocabItem.user_id == user_a_id)).scalars().all()
        assert len(stored) == 1
        assert stored[0].suspended is False
        assert stored[0].due_at is None

    due = client.get("/api/review/due?limit=10")
    assert due.status_code == 200
    assert due.get_json()["total_due"] == 1
    assert due.get_json()["items"][0]["type"] == "grammar"

    answer = client.post(f"/api/review/{item_id}/answer", json={"rating": "good"})
    assert answer.status_code == 200
    answer_data = answer.get_json()
    assert answer_data["status"] == "scheduled"
    assert client.get("/api/review/due").get_json()["total_due"] == 0

    undo = client.post("/api/review/undo", json={"undo_id": answer_data["undo_id"]})
    assert undo.status_code == 200
    assert client.get("/api/review/due").get_json()["total_due"] == 1
    with get_db() as db:
        restored = db.get(VocabItem, uuid.UUID(item_id))
        assert restored.due_at is None
        assert restored.repetitions == 0

@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
@patch("web.app._get_r2")
def test_upload_duplicate_same_user(mock_r2, mock_auth_get_user, mock_app_get_user):
    # Decorators bottom-to-top:
    # 1. web.app._get_r2 -> mock_r2
    # 2. web.auth.get_current_user -> mock_auth_get_user
    # 3. web.app.get_current_user -> mock_app_get_user
    
    user_a_id = uuid.uuid4()
    with get_db() as db:
        user_a = User(
            id=user_a_id,
            email="usera@example.com",
            password_hash="pbkdf2:sha256:...",
            is_admin=False
        )
        db.add(user_a)
    
    mock_auth_get_user.return_value = user_a
    mock_app_get_user.return_value = user_a
    mock_r2.return_value = MagicMock()
    
    # Pre-populate an episode for user_a
    ep_id = uuid.uuid4()
    source_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    token = "youtube:dQw4w9WgXcQ"
    
    with get_db() as db:
        ep = Episode(
            id=ep_id,
            owner_user_id=user_a_id,
            slug="2026-05-21",
            date="2026-05-21",
            title="First Upload",
            source_token=token,
            r2_prefix="episodes/2026-05-21/"
        )
        db.add(ep)
    
    app.config['TESTING'] = True
    with app.test_client() as client:
        rv = client.post("/api/upload", data={
            "source_url": source_url,
            "level": "intermediate"
        })
        
        assert rv.status_code == 200
        res = rv.get_json()
        assert res["job_id"] is None
        assert res["slug"] == "2026-05-21"

@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
@patch("web.app._get_r2")
@patch("web.app._atomic_quota_insert")
@patch("web.app.threading.Thread")
def test_upload_duplicate_different_user_same_level(
    mock_thread, mock_quota_insert, mock_r2, mock_auth_get_user, mock_app_get_user, test_users, tmp_path
):
    # Decorators bottom-to-top:
    # 1. web.app.threading.Thread -> mock_thread
    # 2. web.app._atomic_quota_insert -> mock_quota_insert
    # 3. web.app._get_r2 -> mock_r2
    # 4. web.auth.get_current_user -> mock_auth_get_user
    # 5. web.app.get_current_user -> mock_app_get_user
    
    user_a_id, user_b_id = test_users
    
    with get_db() as db:
        user_b = db.get(User, user_b_id)
    
    mock_auth_get_user.return_value = user_b
    mock_app_get_user.return_value = user_b
    mock_r2.return_value = MagicMock()
    
    # Pre-populate user A's completed Episode
    source_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    token = "youtube:dQw4w9WgXcQ"
    cloned_prefix = "episodes/2026-05-21-cloned/"
    
    user_a_ep_id = uuid.uuid4()
    with get_db() as db:
        ep = Episode(
            id=user_a_ep_id,
            owner_user_id=user_a_id,
            slug="2026-05-21",
            date="2026-05-21",
            title="User A Upload",
            level="intermediate",
            source_token=token,
            r2_prefix=cloned_prefix,
            source="youtube"
        )
        db.add(ep)
        
    app.config['TESTING'] = True
    with app.test_client() as client:
        rv = client.post("/api/upload", data={
            "source_url": source_url,
            "level": "intermediate"
        })
        
        assert rv.status_code == 200
        res = rv.get_json()
        job_id = res["job_id"]
        slug = res["slug"]
        
        # Verify _atomic_quota_insert was NOT called
        mock_quota_insert.assert_not_called()
        
        # Verify background thread setup but intercepted
        mock_thread.assert_called_once()
        thread_kwargs = mock_thread.call_args[1]
        assert thread_kwargs["kwargs"]["clone_from_id"] == str(user_a_ep_id)
        assert thread_kwargs["kwargs"]["user_id"] == user_b_id
        
        # Run the thread target function synchronously using the captured arguments
        ep_dir = tmp_path / slug
        ep_dir.mkdir()
        
        thread_target = thread_kwargs["target"]
        thread_args = thread_kwargs["args"]
        # Override the ep_dir with tmp_path one
        thread_args_list = list(thread_args)
        thread_args_list[2] = ep_dir
        
        thread_target(*thread_args_list, **thread_kwargs["kwargs"])
        
        # Verify the new Episode row is successfully created for user B pointing to the same R2 prefix
        from sqlalchemy import select
        with get_db() as db:
            user_b_eps = db.execute(
                select(Episode).where(Episode.owner_user_id == user_b_id)
            ).scalars().all()
            
            assert len(user_b_eps) == 1
            b_ep = user_b_eps[0]
            assert b_ep.slug == slug
            assert b_ep.level == "intermediate"
            assert b_ep.r2_prefix == cloned_prefix
            assert b_ep.source_token == token
            
        # Local directory should be cleaned up
        assert not ep_dir.exists()
        
        # Job status should be done
        with _jobs_lock:
            job = _jobs.get(job_id)
            assert job["status"] == "done"
            assert job["step_num"] == 1

@patch("web.app.get_current_user")
@patch("web.auth.get_current_user")
@patch("web.app._get_r2")
@patch("web.app._r2_get_json")
@patch("lib.analyzer.analyze_transcript")
@patch("web.app._atomic_quota_insert")
@patch("web.app.threading.Thread")
def test_upload_duplicate_different_user_different_level(
    mock_thread, mock_quota_insert, mock_analyze, mock_r2_get_json, mock_r2, mock_auth_get_user, mock_app_get_user, test_users, tmp_path
):
    # Decorators bottom-to-top:
    # 1. web.app.threading.Thread -> mock_thread
    # 2. web.app._atomic_quota_insert -> mock_quota_insert
    # 3. lib.analyzer.analyze_transcript -> mock_analyze
    # 4. web.app._r2_get_json -> mock_r2_get_json
    # 5. web.app._get_r2 -> mock_r2
    # 6. web.auth.get_current_user -> mock_auth_get_user
    # 7. web.app.get_current_user -> mock_app_get_user
    
    user_a_id, user_b_id = test_users
    
    with get_db() as db:
        user_b = db.get(User, user_b_id)
        
    mock_auth_get_user.return_value = user_b
    mock_app_get_user.return_value = user_b
    
    # Mock R2 client
    mock_s3 = MagicMock()
    mock_r2.return_value = mock_s3
    
    # Mock transcript JSON from R2
    mock_r2_get_json.return_value = {
        "segments": [{"text": "テスト", "start": 0.0, "end": 2.0}]
    }
    
    # Mock analyzer output
    mock_analyze.return_value = {
        "highlights": ["テスト"],
        "words": [{"word": "テスト", "reading": "てすと"}]
    }
    
    # Pre-populate user A's completed Episode
    source_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    token = "youtube:dQw4w9WgXcQ"
    cloned_prefix = "episodes/2026-05-21-cloned/"
    
    user_a_ep_id = uuid.uuid4()
    with get_db() as db:
        ep = Episode(
            id=user_a_ep_id,
            owner_user_id=user_a_id,
            slug="2026-05-21",
            date="2026-05-21",
            title="User A Upload",
            level="intermediate",
            source_token=token,
            r2_prefix=cloned_prefix,
            source="youtube"
        )
        db.add(ep)
        
    app.config['TESTING'] = True
    with app.test_client() as client:
        # POST to upload the duplicate URL for user B (at a different level: advanced)
        rv = client.post("/api/upload", data={
            "source_url": source_url,
            "level": "advanced"
        })
        
        assert rv.status_code == 200
        res = rv.get_json()
        job_id = res["job_id"]
        slug = res["slug"]
        
        # Verify quota check is bypassed
        mock_quota_insert.assert_not_called()
        
        # Verify background thread was intercepted
        mock_thread.assert_called_once()
        thread_kwargs = mock_thread.call_args[1]
        
        # Run pipeline thread target synchronously
        ep_dir = tmp_path / slug
        ep_dir.mkdir()
        
        thread_target = thread_kwargs["target"]
        thread_args = thread_kwargs["args"]
        thread_args_list = list(thread_args)
        thread_args_list[2] = ep_dir
        
        thread_target(*thread_args_list, **thread_kwargs["kwargs"])
        
        with _jobs_lock:
            job_state = _jobs.get(job_id)
        assert job_state is not None
        assert job_state.get("error") == "", f"Job failed with error: {job_state.get('error')}"
        assert job_state.get("status") == "done"
        
        # Verify transcript is fetched and analyzer is called with correct level
        mock_r2_get_json.assert_called_once_with(f"{cloned_prefix}transcript.json")
        mock_analyze.assert_called_once_with([{"text": "テスト", "start": 0.0, "end": 2.0}], level="advanced")
        
        # Verify they were uploaded to S3 from correct local paths
        expected_uploads = [
            f"{cloned_prefix}analysis_advanced.json",
            f"{cloned_prefix}highlights_advanced.json",
            f"{cloned_prefix}cards_advanced.csv"
        ]
        assert mock_s3.upload_file.call_count == 3
        uploaded_keys = [call.args[2] for call in mock_s3.upload_file.call_args_list]
        uploaded_paths = [call.args[0] for call in mock_s3.upload_file.call_args_list]
        for key in expected_uploads:
            assert key in uploaded_keys
        for fn in ["analysis_advanced.json", "highlights_advanced.json", "cards_advanced.csv"]:
            assert str(ep_dir / fn) in uploaded_paths
            
        # Verify new Episode row is created pointing to the original R2 prefix but with the new level
        with get_db() as db:
            b_ep = db.execute(
                select(Episode).where(Episode.owner_user_id == user_b_id)
            ).scalar_one()
            assert b_ep.slug == slug
            assert b_ep.level == "advanced"
            assert b_ep.r2_prefix == cloned_prefix
            
        # Local directory is cleaned up
        assert not ep_dir.exists()
        
        # Job status is done
        with _jobs_lock:
            job = _jobs.get(job_id)
            assert job["status"] == "done"
            assert job["step_num"] == 3
