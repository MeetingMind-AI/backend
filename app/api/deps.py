from __future__ import annotations

from fastapi import Cookie, HTTPException
from sqlalchemy import select

from app.db.models import Session
from app.db.session import SessionLocal


def get_current_user_id(mm_session: str | None = Cookie(default=None)) -> int:
    if not mm_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with SessionLocal() as db:
        row = db.execute(
            select(Session).where(Session.token == mm_session)
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        return int(row.user_id)
