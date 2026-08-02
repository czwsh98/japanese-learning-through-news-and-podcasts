"""
Database layer — SQLAlchemy 2 models and session management.

Tables are created automatically by calling init_db() at application startup.
If DATABASE_URL is not set the module initialises in no-op mode so the app
can still run locally without Postgres (all DB-backed features are disabled).

Usage
-----
from web.db import init_db, db_available, get_db, User, ...

# at app startup:
init_db()

# in a request handler:
with get_db() as db:
    user = db.get(User, user_id)
"""
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

log = logging.getLogger(__name__)

# ── Engine / session factory (populated by init_db) ──────────────────────────

_engine = None
_SessionFactory = None


# ── ORM base ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    """Registered user account."""
    __tablename__ = "users"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email            = Column(String(320), nullable=False, unique=True)
    password_hash    = Column(Text, nullable=False)
    is_admin         = Column(Boolean, nullable=False, server_default="false")
    # Hard cap on lifetime transcription jobs; admins are treated as unlimited in code.
    transcription_limit = Column(Integer, nullable=False, server_default="3")
    created_at       = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    sessions             = relationship("UserSession",        back_populates="user", cascade="all, delete-orphan")
    episodes             = relationship("Episode",            back_populates="owner", cascade="all, delete-orphan")
    vocab                = relationship("VocabItem",          back_populates="user",  cascade="all, delete-orphan")
    transcription_usage  = relationship("TranscriptionUsage", back_populates="user",  cascade="all, delete-orphan")
    recommendation_dismissals = relationship("RecommendationDismissal", back_populates="user", cascade="all, delete-orphan")
    playback_progress     = relationship("PlaybackProgress", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    """
    Auth token row.  The token is sent as an HttpOnly cookie for the Jinja web
    UI and as 'Authorization: Bearer <token>' for the Vite SPA / Capacitor iOS
    app.  Phase 2 creates and validates these; the table exists now so the
    schema is complete from the first deploy.
    """
    __tablename__ = "sessions"

    token      = Column(String(64), primary_key=True)
    user_id    = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)

    user = relationship("User", back_populates="sessions")


class Episode(Base):
    """
    Per-user episode record.  Audio and generated files live in R2 (Phase 4);
    r2_prefix stores the key prefix so files can be fetched without scanning.
    Until Phase 4 the column stays empty and files are served from the local
    EPISODES_DIR as today.
    """
    __tablename__ = "episodes"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id  = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    slug           = Column(Text, nullable=False)          # e.g. 2026-05-11 or 2026-05-11-2
    date           = Column(String(10), nullable=False)    # YYYY-MM-DD
    title          = Column(Text, nullable=False, server_default="")
    channel        = Column(Text, nullable=False, server_default="")
    url            = Column(Text, nullable=False, server_default="")
    thumbnail      = Column(Text, nullable=False, server_default="")
    duration       = Column(Integer, nullable=False, server_default="0")   # seconds
    level          = Column(Text, nullable=False, server_default="advanced")
    source         = Column(Text, nullable=False, server_default="")       # youtube|upload|url
    # R2 key prefix — populated in Phase 4, empty until then.
    r2_prefix      = Column(Text, nullable=False, server_default="")
    # Unique token to identify duplicate YouTube / podcast URL transcribing
    source_token   = Column(Text, nullable=True, index=True)
    # Cross-device playback resume position (seconds). resume_updated_at lets
    # the client reconcile against its own localStorage copy by recency.
    resume_position   = Column(Float, nullable=True)
    resume_updated_at = Column(DateTime(timezone=True), nullable=True)
    # max_position is the playback high-water mark; completed_at preserves the
    # fact of completion even after the current resume point changes.
    max_position      = Column(Float, nullable=True)
    completed_at      = Column(DateTime(timezone=True), nullable=True)
    retention_exempt  = Column(Boolean, nullable=False, server_default="false")
    delete_after      = Column(DateTime(timezone=True), nullable=True)
    deleted_at        = Column(DateTime(timezone=True), nullable=True)
    created_at     = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "slug", name="uq_episode_owner_slug"),
    )

    owner               = relationship("User",               back_populates="episodes")
    transcription_usage = relationship("TranscriptionUsage", back_populates="episode")
    vocab_occurrences   = relationship("VocabOccurrence",    back_populates="episode", passive_deletes=True)


class VocabItem(Base):
    """
    Per-user saved vocabulary item.  Replaces the global vocab.json in Phase 3.
    Deduplicated on (user_id, word).
    """
    __tablename__ = "vocab"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    word           = Column(Text, nullable=False)
    reading        = Column(Text, nullable=False, server_default="")
    en             = Column(Text, nullable=False, server_default="")
    zh             = Column(Text, nullable=False, server_default="")
    example        = Column(Text, nullable=False, server_default="")
    level          = Column(Text, nullable=False, server_default="")
    type           = Column(Text, nullable=False, server_default="vocab")  # vocab|grammar|expression|context-specific
    source_episode = Column(Text, nullable=False, server_default="")
    due_at         = Column(DateTime(timezone=True), nullable=True)
    interval_days  = Column(Float, nullable=False, server_default="0")
    repetitions    = Column(Integer, nullable=False, server_default="0")
    lapses         = Column(Integer, nullable=False, server_default="0")
    suspended      = Column(Boolean, nullable=False, default=False, server_default="false")
    last_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    saved_at       = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "word", name="uq_vocab_user_word"),
    )

    user        = relationship("User", back_populates="vocab")
    occurrences = relationship(
        "VocabOccurrence",
        back_populates="vocab_item",
        cascade="all, delete-orphan",
        order_by="VocabOccurrence.saved_at.desc()",
    )
    review_logs = relationship("ReviewLog", cascade="all, delete-orphan")


class VocabOccurrence(Base):
    """One contextual encounter with a saved vocabulary item.

    Episode references may be cleared by retention, while immutable snapshots
    keep the learning context useful after the source episode is purged.
    """
    __tablename__ = "vocab_occurrences"

    id                     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vocab_item_id          = Column(UUID(as_uuid=True), ForeignKey("vocab.id", ondelete="CASCADE"), nullable=False)
    episode_id             = Column(UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True)
    episode_slug_snapshot  = Column(Text, nullable=False, server_default="")
    episode_title_snapshot = Column(Text, nullable=False, server_default="")
    segment_index          = Column(Integer, nullable=True)
    start_time             = Column(Float, nullable=True)
    end_time               = Column(Float, nullable=True)
    source_text            = Column(Text, nullable=False, server_default="")
    source_en              = Column(Text, nullable=False, server_default="")
    source_zh              = Column(Text, nullable=False, server_default="")
    clip_key               = Column(Text, nullable=False, server_default="")
    saved_at               = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "vocab_item_id", "episode_id", "start_time",
            name="uq_vocab_occurrence_item_episode_time",
        ),
    )

    vocab_item = relationship("VocabItem", back_populates="occurrences")
    episode    = relationship("Episode", back_populates="vocab_occurrences")


class ReviewLog(Base):
    """One reversible review answer with the item's previous schedule."""
    __tablename__ = "review_logs"

    id                      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vocab_item_id           = Column(UUID(as_uuid=True), ForeignKey("vocab.id", ondelete="CASCADE"), nullable=False)
    user_id                 = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating                  = Column(String(16), nullable=False)
    previous_due_at         = Column(DateTime(timezone=True), nullable=True)
    previous_interval_days  = Column(Float, nullable=False, server_default="0")
    previous_repetitions    = Column(Integer, nullable=False, server_default="0")
    previous_lapses         = Column(Integer, nullable=False, server_default="0")
    previous_last_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_at             = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TranscriptionUsage(Base):
    """
    One row per transcription attempt.  Used to enforce per-user job caps
    (Phase 5).  status lifecycle: 'started' → 'completed' | 'failed'.

    Counting rule: jobs that count against the limit are those with
    status IN ('started', 'completed').  A 'failed' job (e.g. download
    error before the Whisper call) does NOT consume quota.
    """
    __tablename__ = "transcription_usage"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # episode_id may be null if the episode row hasn't been created yet when
    # the usage row is inserted (e.g. download failed before we had a slug).
    episode_id  = Column(UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True)
    audio_bytes = Column(BigInteger, nullable=False, server_default="0")
    # started | completed | failed
    status      = Column(String(16), nullable=False, server_default="started")
    created_at  = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user    = relationship("User",    back_populates="transcription_usage")
    episode = relationship("Episode", back_populates="transcription_usage")


class RecommendationDismissal(Base):
    """Permanent per-user dismissal of one curated catalog candidate."""
    __tablename__ = "recommendation_dismissals"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id      = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    candidate_id = Column(String(100), nullable=False)
    dismissed_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "candidate_id", name="uq_recommendation_dismissal_user_candidate"),
    )
    user = relationship("User", back_populates="recommendation_dismissals")


class PlaybackProgress(Base):
    """Latest server-side listening state used by recommendation ranking."""
    __tablename__ = "playback_progress"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    episode_id = Column(UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False)
    percent    = Column(Integer, nullable=False, server_default="0")  # 0..100
    finished   = Column(Boolean, nullable=False, server_default="false")
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "episode_id", name="uq_playback_progress_user_episode"),
    )
    user = relationship("User", back_populates="playback_progress")


# ── Public helpers ────────────────────────────────────────────────────────────

def init_db() -> bool:
    """
    Connect to Postgres, create all tables (idempotent), and return True.
    Returns False silently if DATABASE_URL is unset — the app runs in
    "no-database" mode and all DB-backed features (auth, rate limiting,
    per-user vocab) are disabled.
    """
    global _engine, _SessionFactory

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        log.warning(
            "DATABASE_URL not set — running without database. "
            "Auth, rate limiting, and per-user vocab are disabled."
        )
        return False

    # Railway provides postgres:// but SQLAlchemy 2 requires postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    _engine = create_engine(url, pool_pre_ping=True, future=True)
    # expire_on_commit=False keeps column values accessible on detached instances
    # after the session closes — prevents DetachedInstanceError in templates.
    _SessionFactory = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)

    Base.metadata.create_all(_engine)
    try:
        from sqlalchemy import text as sa_text
        with _engine.begin() as conn:
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS source_token TEXT;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS resume_position DOUBLE PRECISION;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS resume_updated_at TIMESTAMPTZ;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS max_position DOUBLE PRECISION;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS retention_exempt BOOLEAN NOT NULL DEFAULT FALSE;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS delete_after TIMESTAMPTZ;"))
            conn.execute(sa_text("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS due_at TIMESTAMPTZ;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS interval_days DOUBLE PRECISION NOT NULL DEFAULT 0;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS repetitions INTEGER NOT NULL DEFAULT 0;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS lapses INTEGER NOT NULL DEFAULT 0;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT FALSE;"))
            conn.execute(sa_text("ALTER TABLE vocab ADD COLUMN IF NOT EXISTS last_reviewed_at TIMESTAMPTZ;"))
            conn.execute(sa_text(
                "UPDATE episodes SET delete_after = completed_at + INTERVAL '30 days' "
                "WHERE completed_at IS NOT NULL AND delete_after IS NULL "
                "AND retention_exempt = FALSE AND deleted_at IS NULL;"
            ))
        log.info("Database migration: episode source/resume/completion columns ensured")
    except Exception as e:
        log.warning(f"Could not ensure episode migration columns: {e}")
    # Backfill the legacy single source_episode value into the normalized
    # occurrence table. This is deliberately ORM-based so it works in local
    # SQLite tests as well as production Postgres.
    try:
        with _SessionFactory() as session:
            existing_ids = set(session.query(VocabOccurrence.vocab_item_id).all())
            existing_ids = {row[0] for row in existing_ids}
            legacy_items = session.query(VocabItem).filter(
                VocabItem.source_episode != "",
                ~VocabItem.id.in_(existing_ids) if existing_ids else True,
            ).all()
            for item in legacy_items:
                episode = session.query(Episode).filter(
                    Episode.owner_user_id == item.user_id,
                    Episode.slug == item.source_episode,
                ).one_or_none()
                session.add(VocabOccurrence(
                    vocab_item_id=item.id,
                    episode_id=episode.id if episode else None,
                    episode_slug_snapshot=item.source_episode,
                    episode_title_snapshot=episode.title if episode else "",
                    source_text=item.example,
                ))
            session.commit()
        if legacy_items:
            log.info("Database migration: backfilled %d vocab occurrences", len(legacy_items))
    except Exception as e:
        log.warning(f"Could not backfill vocab occurrences: {e}")
    log.info("Database connected — all tables ensured")
    return True


def db_available() -> bool:
    """Return True if the database has been successfully initialised."""
    return _engine is not None


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    Context manager that yields a SQLAlchemy Session.
    Commits on clean exit, rolls back on exception, always closes.

    Example::

        with get_db() as db:
            user = db.get(User, some_uuid)
    """
    if _SessionFactory is None:
        raise RuntimeError(
            "Database not initialised — ensure DATABASE_URL is set and "
            "init_db() has been called before using get_db()."
        )
    session: Session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
