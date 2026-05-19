"""
Authentication helpers for the Japanese Pipeline web app.

Token lifecycle
---------------
- A random 32-byte URL-safe token is generated on login/register.
- The token is stored in the `sessions` table and sent to the client as:
    • an HttpOnly cookie  (`session_token`) for the Jinja web UI
    • a plain JSON value  for the Vite SPA / Capacitor iOS app, which then
      sends it as `Authorization: Bearer <token>` on subsequent requests.
- Both paths are checked in get_current_user() on every request.
- Sessions expire after TOKEN_EXPIRY_DAYS days (default 30).

No-database mode
----------------
If DATABASE_URL is not set, db_available() returns False and every helper
in this module is a safe no-op — login_required() lets all requests through,
get_current_user() returns None, and the auth routes are still registered
but return 503.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import g, jsonify, redirect, request, url_for
from sqlalchemy import func, select
from werkzeug.security import check_password_hash, generate_password_hash

from web.db import User, UserSession, db_available, get_db

log = logging.getLogger(__name__)

TOKEN_COOKIE      = "session_token"
# SESSION_DAYS env var lets operators extend the default; 90 days reduces
# re-login friction especially on mobile without sacrificing much security.
TOKEN_EXPIRY_DAYS = int(os.environ.get("SESSION_DAYS", "90"))
MIN_PASSWORD_LEN  = 8


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


# ── Token / session helpers ───────────────────────────────────────────────────

def _extract_token() -> str | None:
    """Pull the session token from the cookie or Authorization header."""
    token = request.cookies.get(TOKEN_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def get_current_user() -> User | None:
    """
    Return the authenticated User for this request, or None.
    Result is cached on Flask's `g` so the DB is hit at most once per request.
    Accessing basic column attributes (id, email, is_admin, …) on the returned
    object is safe after the session closes because SQLAlchemy retains loaded
    column values on detached instances.
    """
    if not db_available():
        return None
    if hasattr(g, "_current_user"):
        return g._current_user

    token = _extract_token()
    if not token:
        g._current_user = None
        return None

    try:
        with get_db() as db:
            sess = db.get(UserSession, token)
            if sess is None or sess.expires_at <= datetime.now(timezone.utc):
                g._current_user = None
                return None
            # Load the user directly so column values are available after detach.
            user = db.get(User, sess.user_id)
    except Exception:
        log.exception("Error resolving session token")
        g._current_user = None
        return None

    g._current_user = user
    return user


def _create_session(db, user: User) -> str:
    """Insert a new session row and return the raw token string."""
    token      = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRY_DAYS)
    db.add(UserSession(token=token, user_id=user.id, expires_at=expires_at))
    return token


def set_auth_cookie(response, token: str):
    """Attach the HttpOnly session cookie to *response*."""
    # Only set Secure when the request arrived over HTTPS (Railway / production).
    is_secure = request.headers.get("X-Forwarded-Proto") == "https"
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        max_age=TOKEN_EXPIRY_DAYS * 24 * 3600,
        httponly=True,
        secure=is_secure,
        samesite="Lax",
    )
    return response


def clear_auth_cookie(response):
    response.delete_cookie(TOKEN_COOKIE, samesite="Lax")
    return response


# ── Business logic ────────────────────────────────────────────────────────────

def register_user(
    email: str,
    password: str,
    allowed_emails: "set | None" = None,
) -> tuple[User, str]:
    """
    Create a new user account and open a session.

    The very first user ever registered is automatically made admin
    (transcription_limit ignored — admin = unlimited in rate-limit logic).

    If *allowed_emails* is a non-empty set, only addresses in that set may
    register (the very first user is always allowed so the owner can bootstrap).

    Returns (user, token).
    Raises ValueError with a user-facing message on validation failure.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")

    with get_db() as db:
        existing = db.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if existing:
            raise ValueError("An account with that email already exists.")

        # First-ever user becomes admin.
        is_first = db.execute(select(func.count()).select_from(User)).scalar() == 0

        # Enforce registration whitelist (skip for the bootstrap admin account).
        if not is_first and allowed_emails and email not in allowed_emails:
            raise ValueError("Registration is not open. Contact the administrator.")

        user  = User(
            email=email,
            password_hash=hash_password(password),
            is_admin=is_first,
        )
        db.add(user)
        db.flush()          # resolve user.id (generated in Python, but flush is safe)
        token = _create_session(db, user)
        # Detach user so its attributes remain accessible after the session closes.
        db.expunge(user)

    log.info("Registered %s (admin=%s)", email, is_first)
    return user, token


def authenticate_user(email: str, password: str) -> tuple[User, str] | None:
    """
    Verify credentials and create a new session.
    Returns (user, token) on success, None on bad credentials.
    """
    email = email.strip().lower()
    with get_db() as db:
        user = db.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if not user or not verify_password(password, user.password_hash):
            return None
        token = _create_session(db, user)
        db.expunge(user)

    return user, token


def logout_token(token: str | None) -> None:
    """Delete the session row for *token* (no-op if token is None or missing)."""
    if not token or not db_available():
        return
    try:
        with get_db() as db:
            sess = db.get(UserSession, token)
            if sess:
                db.delete(sess)
    except Exception:
        log.exception("Error deleting session token")


# ── Decorator ─────────────────────────────────────────────────────────────────

def login_required(f):
    """
    Route decorator that requires a valid session.

    Behaviour:
    - db_available() == False  →  passes through (local dev without Postgres)
    - /api/* routes            →  returns 401 JSON
    - All other routes         →  redirects to /login
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not db_available():
            return f(*args, **kwargs)
        user = get_current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized", "code": 401}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated
