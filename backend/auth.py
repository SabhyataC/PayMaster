import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response

from backend.db import get_conn

ALGORITHM = "HS256"
TOKEN_HOURS = 8
COOKIE_NAME = "pm_session"
CSRF_HEADER = "x-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

ROLE_RANK = {"viewer": 1, "reviewer": 2, "admin": 3}

LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_PER_EMAIL = 5
LOGIN_MAX_PER_IP = 30
SIGNUP_MAX_PER_IP_HOUR = 10
RESET_MAX_PER_IP = 10
RESET_TOKEN_MINUTES = 60

_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


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


def burn_password_check(password: str) -> None:
    check_password(password, _DUMMY_HASH)


def validate_new_password(password: str, email: str = "") -> None:
    if not 10 <= len(password) <= 72:
        raise HTTPException(422, "Password must be 10 to 72 characters")
    if len(set(password)) < 5:
        raise HTTPException(422, "Password is too simple")
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 4 and local in password.lower():
        raise HTTPException(422, "Password must not contain your email name")


def make_token(user_id, version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "ver": int(version), "iat": now,
               "exp": now + timedelta(hours=TOKEN_HOURS), "jti": secrets.token_hex(8)}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    samesite = os.environ.get("COOKIE_SAMESITE", "lax").lower()
    if samesite not in ("lax", "strict", "none"):
        samesite = "lax"
    response.set_cookie(
        COOKIE_NAME, token, max_age=TOKEN_HOURS * 3600, httponly=True,
        secure=_is_https(request) or samesite == "none", samesite=samesite, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def client_ip(request: Request) -> str:
    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def too_many(bucket: str, limit: int, window_minutes: int) -> bool:
    with get_conn() as conn:
        n = conn.execute(
            "SELECT count(*) FROM login_attempts WHERE bucket = %s "
            "AND attempted_at > now() - make_interval(mins => %s)",
            (bucket, window_minutes),
        ).fetchone()[0]
    return n >= limit


def record_attempt(*buckets: str) -> None:
    with get_conn() as conn:
        for b in buckets:
            conn.execute("INSERT INTO login_attempts (bucket) VALUES (%s)", (b,))
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < now() - interval '1 day'")


def clear_attempts(bucket: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE bucket = %s", (bucket,))


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request) -> dict:
    """Reads the HttpOnly session cookie, or 'Authorization: Bearer <token>' for API clients.
    Cookie sessions must also send the X-CSRF header on every request that changes data."""
    header = request.headers.get("authorization")
    if header and header.startswith("Bearer "):
        token, via_cookie = header[7:], False
    else:
        token, via_cookie = request.cookies.get(COOKIE_NAME), True
    if not token:
        raise HTTPException(401, "Login required")
    if via_cookie and request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        raise HTTPException(403, "Missing CSRF header")
    try:
        data = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired or invalid. Please log in again")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, company_id, email, full_name, role, token_version, disabled_at, must_change_password "
            "FROM users WHERE id = %s",
            (data["sub"],),
        ).fetchone()
    if not row:
        raise HTTPException(401, "User no longer exists")
    if row[6] is not None:
        raise HTTPException(401, "This account is disabled")
    if int(data.get("ver", 0)) != row[5]:
        raise HTTPException(401, "Session ended. Please log in again")
    return {
        "id": str(row[0]),
        "company_id": str(row[1]) if row[1] else None,
        "email": row[2],
        "full_name": row[3],
        "role": row[4],
        "token_version": row[5],
        "must_change_password": row[7],
    }


def current_member(user: dict = Depends(current_user)) -> dict:
    """Like current_user, but the user must already have created their company."""
    if not user["company_id"]:
        raise HTTPException(403, "Create your company first")
    if user["must_change_password"]:
        raise HTTPException(403, "Change your password first")
    return user


def require_role(minimum: str):
    """Dependency: the member's role must be at least `minimum` (viewer < reviewer < admin)."""
    def dep(user: dict = Depends(current_member)) -> dict:
        if ROLE_RANK.get(user["role"], 0) < ROLE_RANK[minimum]:
            raise HTTPException(403, f"This needs the {minimum} role")
        return user
    return dep


reviewer_or_admin = require_role("reviewer")
admin_only = require_role("admin")
