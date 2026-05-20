from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import Response as RawResponse
from passlib.context import CryptContext
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import get_current_user_id
from app.db.models import Session, User
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/auth", tags=["auth"])

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_MAX_PHOTO_BYTES = 500 * 1024  # 500 KB


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="mm_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60,
    )


def _user_out(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "has_photo": user.photo is not None,
        "photo_url": f"/api/auth/photo/{user.id}" if user.photo else None,
    }


class SignupRequest(BaseModel):
    email: str
    name: str
    password: str
    confirm_password: str
    photo_b64: str | None = None

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return v.strip().lower()


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return v.strip().lower()


@router.post("/signup")
def signup(req: SignupRequest, response: Response) -> dict[str, Any]:
    if req.password != req.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    if len(req.password) < 6:
        raise HTTPException(
            status_code=422, detail="Password must be at least 6 characters"
        )

    photo: bytes | None = None
    if req.photo_b64:
        try:
            photo = base64.b64decode(req.photo_b64)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid photo encoding")
        if len(photo) > _MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=413, detail="Photo too large (max 500 KB)"
            )

    with SessionLocal() as db:
        if db.execute(
            select(User).where(User.email == req.email)
        ).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        user = User(
            email=req.email,
            name=req.name.strip(),
            password_hash=_pwd.hash(req.password),
            photo=photo,
        )
        db.add(user)
        db.flush()

        token = uuid.uuid4().hex
        db.add(Session(user_id=user.id, token=token))
        db.commit()
        db.refresh(user)

        _set_cookie(response, token)
        return {"user": _user_out(user)}


@router.post("/login")
def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == req.email)
        ).scalar_one_or_none()
        if not user or not _pwd.verify(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        token = uuid.uuid4().hex
        db.add(Session(user_id=user.id, token=token))
        db.commit()
        db.refresh(user)

        _set_cookie(response, token)
        return {"user": _user_out(user)}


@router.post("/logout")
def logout(
    response: Response,
    mm_session: str | None = Cookie(default=None),
) -> dict[str, str]:
    if mm_session:
        with SessionLocal() as db:
            row = db.execute(
                select(Session).where(Session.token == mm_session)
            ).scalar_one_or_none()
            if row:
                db.delete(row)
                db.commit()
    response.delete_cookie("mm_session")
    return {"ok": "logged out"}


@router.get("/me")
def get_me(user_id: int = Depends(get_current_user_id)) -> dict[str, Any]:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return _user_out(user)


@router.get("/photo/{user_id}")
def get_photo(user_id: int) -> RawResponse:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or not user.photo:
            raise HTTPException(status_code=404, detail="Photo not found")
        return RawResponse(content=user.photo, media_type="image/jpeg")
