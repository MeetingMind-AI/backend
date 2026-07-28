"""
Authentication REST API Module.

Handles user signup, login, logout, profile fetching, and profile avatar retrieval.
Manages password hashing via bcrypt and session token issuance via HTTP-only cookies.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import Response as RawResponse
import bcrypt
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import get_current_user_id
from app.db.models import Session, User
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Maximum allowable profile avatar image file size in bytes
_MAX_PHOTO_BYTES = 500 * 1024  # 500 KB


def _hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt.

    Args:
        password (str): Plaintext password string.

    Returns:
        str: Bcrypt hashed password string.
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Args:
        password (str): Plaintext password to verify.
        hashed (str): Previously computed bcrypt hash string.

    Returns:
        bool: True if password matches hash, False otherwise.
    """
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _set_cookie(response: Response, token: str) -> None:
    """Set the HTTP-only session cookie on the outgoing response.

    Args:
        response (Response): FastAPI response object.
        token (str): Opaque UUID session token string.
    """
    response.set_cookie(
        key="mm_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60,
    )


def _user_out(user: User) -> dict[str, Any]:
    """Format User model instance into a standardized JSON response dictionary.

    Args:
        user (User): User ORM model instance.

    Returns:
        dict[str, Any]: Dictionary containing public user fields and avatar URI.
    """
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "has_photo": user.photo is not None,
        "photo_url": f"/api/auth/photo/{user.id}" if user.photo else None,
    }


class SignupRequest(BaseModel):
    """Signup Request Schema.

    Attributes:
        email (str): User email address.
        name (str): Full display name.
        password (str): Account password.
        confirm_password (str): Password confirmation matching password.
        photo_b64 (str | None): Base64-encoded profile picture image data.
    """

    email: str
    name: str
    password: str
    confirm_password: str
    photo_b64: str | None = None

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        """Strip whitespace and lowercase user email address.

        Args:
            v (str): Raw input email string.

        Returns:
            str: Normalized email string.
        """
        return v.strip().lower()


class LoginRequest(BaseModel):
    """Login Request Schema.

    Attributes:
        email (str): Registered user email address.
        password (str): Plaintext account password.
    """

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        """Strip whitespace and lowercase user email address.

        Args:
            v (str): Raw input email string.

        Returns:
            str: Normalized email string.
        """
        return v.strip().lower()


@router.post("/signup")
def signup(req: SignupRequest, response: Response) -> dict[str, Any]:
    """Register a new user account and set auth cookie.

    Validates password strength, email uniqueness, and profile photo size. Creates
    user and session records in database and attaches session cookie to HTTP response.

    Args:
        req (SignupRequest): User signup data payload.
        response (Response): FastAPI HTTP response object.

    Returns:
        dict[str, Any]: Dictionary containing registered user information.

    Raises:
        HTTPException: 422 for validation errors, 413 for photo size, 409 for duplicate email.
    """
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
            password_hash=_hash_password(req.password),
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
    """Authenticate an existing user with email and password.

    Verifies credentials, generates a new session token, and sets auth cookie.

    Args:
        req (LoginRequest): User credentials payload.
        response (Response): FastAPI HTTP response object.

    Returns:
        dict[str, Any]: Dictionary containing authenticated user information.

    Raises:
        HTTPException: 401 Unauthorized if email or password is invalid.
    """
    with SessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == req.email)
        ).scalar_one_or_none()
        if not user or not _verify_password(req.password, user.password_hash):
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
    """Logout current user by revoking session token and clearing cookie.

    Args:
        response (Response): FastAPI HTTP response object.
        mm_session (str | None): Current session cookie token.

    Returns:
        dict[str, str]: Confirmation message `{"ok": "logged out"}`.
    """
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
    """Get authenticated profile information for current user.

    Args:
        user_id (int): Primary key ID of authenticated user from dependency.

    Returns:
        dict[str, Any]: Dictionary of current user attributes.

    Raises:
        HTTPException: 404 Not Found if user record does not exist.
    """
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return _user_out(user)


@router.get("/photo/{user_id}")
def get_photo(user_id: int) -> RawResponse:
    """Retrieve raw profile avatar image binary data for a user.

    Args:
        user_id (int): Primary key ID of user whose photo is requested.

    Returns:
        RawResponse: Binary image response with `image/jpeg` MIME type.

    Raises:
        HTTPException: 404 Not Found if user or photo binary does not exist.
    """
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or not user.photo:
            raise HTTPException(status_code=404, detail="Photo not found")
        return RawResponse(content=user.photo, media_type="image/jpeg")

