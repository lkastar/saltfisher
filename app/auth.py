"""Single-token bearer auth.

No user table, no sessions, no password hashing: for a single-user
self-hosted deployment those are pure overhead. See design.md section 7.
"""

import secrets

from fastapi import Header, HTTPException

from app.config import settings


def require_token(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, settings.api_token):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
