"""
FastAPI Dependencies Module.

Provides reusable authentication dependencies for FastAPI route handlers,
validating HTTP session cookies against active database sessions.
"""

from __future__ import annotations

from fastapi import Cookie, HTTPException
from sqlalchemy import select

from app.db.models import Session
from app.db.session import SessionLocal


def get_current_user_id(mm_session: str | None = Cookie(default=None)) -> int:
    """Validate user session cookie and return authenticated user ID.

    Reads the `mm_session` cookie from incoming HTTP requests, checks for an active
    session record in the database, and resolves the owner's primary key ID.

    Args:
        mm_session (str | None): Session token string extracted from HTTP cookie.

    Returns:
        int: Primary key ID of the authenticated user.

    Raises:
        HTTPException: HTTP 401 Unauthorized if cookie is missing, invalid, or expired.
    """
    if not mm_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with SessionLocal() as db:
        row = db.execute(
            select(Session).where(Session.token == mm_session)
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        return int(row.user_id)

