"""Provider sign-in and opaque bearer session helpers."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection


PROVIDERS = {
    "google": {
        "issuers": ["https://accounts.google.com", "accounts.google.com"],
        "jwks_url": "https://www.googleapis.com/oauth2/v3/certs",
        "audience_env": "GOOGLE_CLIENT_IDS",
        "algorithms": ["RS256"],
    },
    "apple": {
        "issuers": ["https://appleid.apple.com"],
        "jwks_url": "https://appleid.apple.com/auth/keys",
        "audience_env": "APPLE_CLIENT_IDS",
        "algorithms": ["RS256"],
    },
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def configured_audiences(provider: str) -> list[str]:
    value = os.getenv(PROVIDERS[provider]["audience_env"], "")
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=2)
def jwks_client(provider: str):
    return jwt.PyJWKClient(PROVIDERS[provider]["jwks_url"], timeout=5)


def verify_id_token(provider: str, token: str, expected_nonce: str) -> str:
    config = PROVIDERS[provider]
    audiences = configured_audiences(provider)
    if not audiences:
        raise HTTPException(status_code=503, detail="This sign-in provider is not configured.")
    try:
        # These URLs are constants owned by the providers. No claim from the token
        # influences which issuer or signing keys are trusted.
        client = jwks_client(provider)
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=config["algorithms"],
            audience=audiences,
            issuer=config["issuers"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        subject = claims.get("sub")
        if (not isinstance(subject, str) or not subject.strip() or len(subject) > 255
                or claims.get("nonce") != expected_nonce):
            raise ValueError("Invalid identity claims")
        return subject
    except HTTPException:
        raise
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(status_code=503, detail="Provider identity verification is temporarily unavailable.") from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid provider identity token.") from exc


def new_challenge(db: Connection, provider: str) -> dict[str, Any]:
    if provider not in PROVIDERS or not configured_audiences(provider):
        raise HTTPException(status_code=503, detail="This sign-in provider is not configured.")
    challenge_id = str(uuid.uuid4())
    nonce = secrets.token_hex(32)
    expires_at = utcnow() + timedelta(minutes=5)
    db.execute(text("""INSERT INTO login_challenges (id, provider, nonce, expires_at)
        VALUES (:id, :provider, :nonce, :expires_at)"""), {
        "id": challenge_id, "provider": provider, "nonce": nonce, "expires_at": expires_at,
    })
    return {"id": challenge_id, "nonce": nonce, "expires_at": expires_at}


def consume_challenge(db: Connection, challenge_id: str, provider: str) -> str:
    now = utcnow()
    result = db.execute(text("""UPDATE login_challenges SET consumed_at=:now
        WHERE id=:id AND provider=:provider AND consumed_at IS NULL AND expires_at>:now
        RETURNING nonce"""), {"id": challenge_id, "provider": provider, "now": now}).first()
    if result is None:
        raise HTTPException(status_code=401, detail="Login challenge is invalid, expired, or already used.")
    return result[0]


def create_identity_and_session(db: Connection, provider: str, subject: str) -> dict[str, Any]:
    # Serialize first sign-ins for one provider identity. A collision in hashtext
    # only causes harmless extra serialization.
    if db.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:identity_key))"), {
            "identity_key": f"{provider}:{subject}",
        })
    identity = db.execute(text("""SELECT account_id FROM login_identities
        WHERE provider=:provider AND subject=:subject"""), {
        "provider": provider, "subject": subject,
    }).first()
    if identity is None:
        account_id = str(uuid.uuid4())
        db.execute(text("INSERT INTO accounts (id) VALUES (:id)"), {"id": account_id})
        db.execute(text("""INSERT INTO login_identities (provider, subject, account_id)
            VALUES (:provider, :subject, :account_id)"""), {
            "provider": provider, "subject": subject, "account_id": account_id,
        })
    else:
        account_id = identity[0]

    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=session_lifetime_days())
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db.execute(text("""INSERT INTO sessions (token_digest, account_id, expires_at)
        VALUES (:digest, :account_id, :expires_at)"""), {
        "digest": digest, "account_id": account_id, "expires_at": expires_at,
    })
    return {"token": token, "expires_at": expires_at, "account": {"id": account_id}}


def session_lifetime_days() -> int:
    try:
        days = int(os.getenv("SESSION_LIFETIME_DAYS", "30"))
    except ValueError:
        raise HTTPException(status_code=503, detail="Session lifetime configuration is invalid.")
    if not 1 <= days <= 90:
        raise HTTPException(status_code=503, detail="Session lifetime configuration is invalid.")
    return days


def account_for_token(db: Connection, token: str) -> str | None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    result = db.execute(text("""SELECT account_id FROM sessions
        WHERE token_digest=:digest AND revoked_at IS NULL AND expires_at>:now"""), {
        "digest": digest, "now": utcnow(),
    }).first()
    return result[0] if result else None


def revoke_token(db: Connection, token: str) -> bool:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    result = db.execute(text("""UPDATE sessions SET revoked_at=:now
        WHERE token_digest=:digest AND revoked_at IS NULL AND expires_at>:now"""), {
        "digest": digest, "now": utcnow(),
    })
    return bool(result.rowcount)
