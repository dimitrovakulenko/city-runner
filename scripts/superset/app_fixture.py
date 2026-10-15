"""Real app auth/session endpoints over a disposable synthetic session database.

The fixture replaces provider verification only. It calls the application's
post-verification identity/session code and uses its ordinary me/logout routes.
No provider credentials or real accounts are used.
"""

import os
import hashlib

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.auth import create_identity_and_session, utcnow
os.environ['DATABASE_URL'] = os.environ['PILOT_SESSION_DATABASE_URL']
from backend.app.main import app

engine = app.state.engine


@app.post('/fixture/session/{name}')
def synthetic_identity(name: str, expired: bool = False):
    if name not in {'pilot-a', 'pilot-b'}:
        raise HTTPException(404)
    with engine.begin() as db:
        session = create_identity_and_session(db, 'google', f'synthetic-{name}')
        if expired:
            db.execute(text('UPDATE sessions SET expires_at=:now WHERE token_digest=:digest'), {
                'now': utcnow(), 'digest': hashlib.sha256(session['token'].encode()).hexdigest(),
            })
        return session
