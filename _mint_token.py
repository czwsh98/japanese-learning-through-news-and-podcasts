"""Mint a session token for the given owner email and write it to stdout.
Runs INSIDE the mimichan-web container (has app deps + DB access).
Invoked by the bot via: docker exec mimichan-web-1 python /app/_mint_token.py <email>
The token is captured by the bot over the subprocess pipe and stored in a
0600 file — it is never logged.
"""
import sys
from web.db import init_db, get_db, User
from web.auth import _create_session
from sqlalchemy import select

email = sys.argv[1]
init_db()
with get_db() as db:
    user = db.execute(select(User).where(User.email == email)).scalar_one()
    token = _create_session(db, user)
sys.stdout.write(token)
