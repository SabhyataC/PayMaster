"""Passwords and login tokens. Every protected route uses current_user."""
import os
from datetime import datetime, timedelta, timezone
from fastapi import Depends, Header, HTTPException
import bcrypt
import jwt

from backend.db import get_conn

ALGORITHM = "HS256"
TOKEN_HOURS = 8


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "JWT_SECRET is missing or shorter than 32 characters")
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def make_token(user_id) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expires}, _secret(), algorithm=ALGORITHM)


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """Reads 'Authorization: Bearer <token>', returns the logged-in user and their company."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Login required")
    try:
        data = jwt.decode(authorization[7:], _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired or invalid. Please log in again")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, company_id, email, full_name, role FROM users WHERE id = %s",
            (data["sub"],),
        ).fetchone()
    if not row:
        raise HTTPException(401, "User no longer exists")
    return {
        "id": str(row[0]),
        "company_id": str(row[1]) if row[1] else None,
        "email": row[2],
        "full_name": row[3],
        "role": row[4],
    }

def current_member(user: dict = Depends(current_user)) -> dict:
    """Like current_user, but the user must already have created their company."""
    if not user["company_id"]:
        raise HTTPException(403, "Create your company first")
    return user

