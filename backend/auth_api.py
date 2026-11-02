import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from backend.audit import log_decision
from backend.auth import (
    CSRF_HEADER, LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MINUTES, RESET_MAX_PER_IP,
    SIGNUP_MAX_PER_IP_HOUR, burn_password_check, check_password, clear_attempts,
    clear_session_cookie, client_ip, current_user, hash_password, hash_reset_token, make_token,
    record_attempt, set_session_cookie, too_many, validate_new_password,
)
from backend.db import get_conn

router = APIRouter(prefix="/auth", tags=["auth"])


class Signup(BaseModel):
    full_name: str
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


class CompanyIn(BaseModel):
    name: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class ResetPassword(BaseModel):
    token: str
    new_password: str


def _user_out(user_id, company_id, company_name, email, full_name, role, must_change=False) -> dict:
    return {
        "id": str(user_id),
        "company_id": str(company_id) if company_id else None,
        "company_name": company_name,
        "email": email,
        "full_name": full_name,
        "role": role,
        "must_change_password": bool(must_change),
    }


def _session_reply(request: Request, response: Response, user_id, version, user_out) -> dict:
    token = make_token(user_id, version)
    set_session_cookie(request, response, token)
    out = {"user": user_out}
    if not request.headers.get(CSRF_HEADER):
        out["token"] = token
    return out


def _audit(conn, user_id, decision, reason):
    log_decision(conn, stage="auth", entity_type="user", entity_id=user_id,
                 decision=decision, reason=reason, actor="human")


@router.post("/signup")
def signup(body: Signup, request: Request, response: Response):
    if os.environ.get("SIGNUP_ENABLED", "true").lower() in ("0", "false", "no"):
        raise HTTPException(403, "Signup is closed. Ask an admin of your company to add you")
    ip = client_ip(request)
    if too_many(f"signup-ip:{ip}", SIGNUP_MAX_PER_IP_HOUR, 60):
        raise HTTPException(429, "Too many signups from this address. Try again later")
    full_name = body.full_name.strip()
    email = body.email.strip().lower()
    if not full_name or len(full_name) > 120:
        raise HTTPException(422, "Your name is required")
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
        raise HTTPException(422, "Enter a valid email")
    validate_new_password(body.password, email)
    record_attempt(f"signup-ip:{ip}")
    try:
        with get_conn() as conn:
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash, full_name, role) "
                "VALUES (%s, %s, %s, 'admin') RETURNING id",
                (email, hash_password(body.password), full_name),
            ).fetchone()[0]
    except UniqueViolation:
        raise HTTPException(409, "An account with this email already exists")
    return _session_reply(request, response, user_id, 0, _user_out(user_id, None, None, email, full_name, "admin"))


@router.post("/login")
def login(body: Login, request: Request, response: Response):
    email = body.email.strip().lower()[:254]
    ip = client_ip(request)
    email_bucket, ip_bucket = f"login-email:{email}", f"login-ip:{ip}"
    if (too_many(email_bucket, LOGIN_MAX_PER_EMAIL, LOGIN_WINDOW_MINUTES)
            or too_many(ip_bucket, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MINUTES)):
        raise HTTPException(429, f"Too many failed logins. Try again in {LOGIN_WINDOW_MINUTES} minutes",
                            headers={"Retry-After": str(LOGIN_WINDOW_MINUTES * 60)})
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.company_id, c.name, u.email, u.full_name, u.role, u.must_change_password, "
            "u.password_hash, u.token_version, u.disabled_at "
            "FROM users u LEFT JOIN companies c ON c.id = u.company_id "
            "WHERE lower(u.email) = %s",
            (email,),
        ).fetchone()
    if not row:
        burn_password_check(body.password)
        ok = False
    else:
        ok = check_password(body.password, row[7]) and row[9] is None
    if not ok:
        record_attempt(email_bucket, ip_bucket)
        raise HTTPException(401, "Wrong email or password")
    clear_attempts(email_bucket)
    return _session_reply(request, response, row[0], row[8], _user_out(*row[:7]))


@router.post("/logout")
def logout(request: Request, response: Response, user: dict = Depends(current_user)):
    """Ends this session everywhere: every token issued before now stops working."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user["id"],))
        if user["company_id"]:
            _audit(conn, user["id"], "logout", f"{user['full_name']} logged out; all sessions ended")
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/change-password")
def change_password(body: ChangePassword, request: Request, response: Response,
                    user: dict = Depends(current_user)):
    ip = client_ip(request)
    bucket = f"pwchange:{user['id']}"
    if too_many(bucket, LOGIN_MAX_PER_EMAIL, LOGIN_WINDOW_MINUTES):
        raise HTTPException(429, "Too many attempts. Try again later")
    with get_conn() as conn:
        current_hash = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],)).fetchone()[0]
    if not check_password(body.current_password, current_hash):
        record_attempt(bucket, f"login-ip:{ip}")
        raise HTTPException(401, "Current password is wrong")
    validate_new_password(body.new_password, user["email"])
    if body.new_password == body.current_password:
        raise HTTPException(422, "Choose a different password")
    with get_conn() as conn:
        version = conn.execute(
            "UPDATE users SET password_hash = %s, token_version = token_version + 1, "
            "must_change_password = FALSE WHERE id = %s RETURNING token_version",
            (hash_password(body.new_password), user["id"]),
        ).fetchone()[0]
        if user["company_id"]:
            _audit(conn, user["id"], "password_changed", f"{user['full_name']} changed their password")
    clear_attempts(bucket)
    return _session_reply(request, response, user["id"], version,
                          _user_out(user["id"], user["company_id"], None, user["email"],
                                    user["full_name"], user["role"]))


@router.post("/reset-password")
def reset_password(body: ResetPassword, request: Request):
    """Uses the one-time token an admin created for this person (see /admin/users/{id}/reset-token)."""
    ip = client_ip(request)
    if too_many(f"reset-ip:{ip}", RESET_MAX_PER_IP, LOGIN_WINDOW_MINUTES):
        raise HTTPException(429, "Too many attempts. Try again later")
    token_hash = hash_reset_token(body.token.strip())
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.id, r.user_id, r.expires_at, r.used_at, u.email, u.company_id, u.full_name "
            "FROM password_resets r JOIN users u ON u.id = r.user_id WHERE r.token_hash = %s FOR UPDATE OF r",
            (token_hash,),
        ).fetchone()
        if not row or row[3] is not None or row[2] < datetime.now(timezone.utc):
            conn.commit()
            record_attempt(f"reset-ip:{ip}")
            raise HTTPException(400, "This reset link is invalid or has expired")
        validate_new_password(body.new_password, row[4])
        conn.execute("UPDATE password_resets SET used_at = now() WHERE id = %s", (row[0],))
        conn.execute(
            "UPDATE users SET password_hash = %s, token_version = token_version + 1, "
            "must_change_password = FALSE WHERE id = %s",
            (hash_password(body.new_password), row[1]),
        )
        if row[5]:
            _audit(conn, row[1], "password_reset", f"{row[6]} reset their password with a one-time token")
    return {"ok": True}


@router.post("/company")
def create_company(body: CompanyIn, user: dict = Depends(current_user)):
    """Second step after signup: create the company and attach this user to it."""
    name = body.name.strip()
    if not name or len(name) > 160:
        raise HTTPException(422, "Company name is required")
    if user["company_id"]:
        raise HTTPException(409, "You already belong to a company")
    with get_conn() as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()[0]
        conn.execute("UPDATE users SET company_id = %s, role = 'admin' WHERE id = %s", (company_id, user["id"]))
    return {"user": _user_out(user["id"], company_id, name, user["email"], user["full_name"], "admin")}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    name = None
    if user["company_id"]:
        with get_conn() as conn:
            name = conn.execute(
                "SELECT name FROM companies WHERE id = %s", (user["company_id"],)
            ).fetchone()[0]
    return _user_out(user["id"], user["company_id"], name, user["email"], user["full_name"],
                     user["role"], user["must_change_password"])


@router.get("/paypal-account")
def paypal_account(user: dict = Depends(current_user)):
    """The one PayPal account all payments run from (shown read-only in the console)."""
    return {"email": os.environ.get("PAYPAL_ACCOUNT_EMAIL", "")}
