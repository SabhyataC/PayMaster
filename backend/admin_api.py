import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from backend.audit import log_decision
from backend.auth import (
    RESET_TOKEN_MINUTES, ROLE_RANK, admin_only, hash_password, hash_reset_token, validate_new_password,
)
from backend.db import get_conn

router = APIRouter(prefix="/admin", tags=["admin"])
MAX_AUTO_PAY_LIMIT = Decimal("50000")


class MemberIn(BaseModel):
    full_name: str
    email: str
    role: str
    temp_password: str


class MemberEdit(BaseModel):
    role: str | None = None
    disabled: bool | None = None


class SettingsIn(BaseModel):
    auto_pay_limit: str | None = None



def _member_row(r):
    return {"id": str(r[0]), "email": r[1], "full_name": r[2], "role": r[3],
            "disabled": r[4] is not None, "created_at": r[5].isoformat()}


def _active_admins(conn, company_id, excluding=None):
    return conn.execute(
        "SELECT count(*) FROM users WHERE company_id = %s AND role = 'admin' AND disabled_at IS NULL "
        "AND (%s::uuid IS NULL OR id <> %s::uuid)", (company_id, excluding, excluding)).fetchone()[0]


def _own_member(conn, user_id, company_id):
    row = conn.execute(
        "SELECT id, email, full_name, role, disabled_at, created_at FROM users "
        "WHERE id::text = %s AND company_id = %s FOR UPDATE", (str(user_id), company_id)).fetchone()
    if not row:
        raise HTTPException(404, "No such member")
    return row


@router.get("/users")
def list_members(user: dict = Depends(admin_only)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, full_name, role, disabled_at, created_at FROM users "
            "WHERE company_id = %s ORDER BY created_at", (user["company_id"],)).fetchall()
    return {"items": [_member_row(r) for r in rows]}


@router.post("/users")
def add_member(body: MemberIn, user: dict = Depends(admin_only)):
    email = body.email.strip().lower()
    name = body.full_name.strip()
    if body.role not in ROLE_RANK:
        raise HTTPException(422, "Role must be admin, reviewer or viewer")
    if not name or "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Name and a valid email are required")
    validate_new_password(body.temp_password, email)
    try:
        with get_conn() as conn:
            new_id = conn.execute(
                "INSERT INTO users (company_id, email, password_hash, full_name, role, must_change_password) "
                "VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
                (user["company_id"], email, hash_password(body.temp_password), name, body.role)).fetchone()[0]
            log_decision(conn, stage="admin", entity_type="user", entity_id=new_id, decision="member_added",
                         reason=f"{user['full_name']} added {email} as {body.role}",
                         inputs={"by": user["id"], "role": body.role}, actor="human")
    except UniqueViolation:
        raise HTTPException(409, "An account with this email already exists")
    return {"id": str(new_id), "email": email, "role": body.role, "must_change_password": True}


@router.patch("/users/{member_id}")
def edit_member(member_id: str, body: MemberEdit, user: dict = Depends(admin_only)):
    if body.role is None and body.disabled is None:
        raise HTTPException(422, "Nothing to change")
    if body.role is not None and body.role not in ROLE_RANK:
        raise HTTPException(422, "Role must be admin, reviewer or viewer")
    with get_conn() as conn:
        row = _own_member(conn, member_id, user["company_id"])
        was_active_admin = row[3] == "admin" and row[4] is None
        new_role = body.role if body.role is not None else row[3]
        will_be_active = (row[4] is None) if body.disabled is None else (not body.disabled)
        if was_active_admin and not (new_role == "admin" and will_be_active):
            if _active_admins(conn, user["company_id"], excluding=row[0]) == 0:
                raise HTTPException(409, "A company must keep at least one active admin")
        sets, params = [], []
        if body.role is not None:
            sets.append("role = %s")
            params.append(body.role)
        if body.disabled is not None:
            sets.append("disabled_at = " + ("now()" if body.disabled else "NULL"))
        if body.role is not None and body.role != row[3] or body.disabled:
            sets.append("token_version = token_version + 1")
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*params, row[0]))
        log_decision(conn, stage="admin", entity_type="user", entity_id=row[0], decision="member_changed",
                     reason=f"{user['full_name']} changed {row[1]}: role {row[3]} -> {new_role}, "
                            f"disabled {row[4] is not None} -> {not will_be_active}",
                     inputs={"by": user["id"], "role": new_role, "disabled": not will_be_active}, actor="human")
        out = conn.execute(
            "SELECT id, email, full_name, role, disabled_at, created_at FROM users WHERE id = %s", (row[0],)
        ).fetchone()
    return _member_row(out)


@router.post("/users/{member_id}/reset-token")
def make_reset_token(member_id: str, user: dict = Depends(admin_only)):
    """One-time token, shown once, valid for an hour. Give it to the person yourself:
    they send it to /auth/reset-password with a new password."""
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        row = _own_member(conn, member_id, user["company_id"])
        conn.execute("UPDATE password_resets SET used_at = now() WHERE user_id = %s AND used_at IS NULL", (row[0],))
        conn.execute(
            "INSERT INTO password_resets (user_id, token_hash, created_by, expires_at) VALUES (%s, %s, %s, %s)",
            (row[0], hash_reset_token(token), user["id"],
             datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_MINUTES)))
        log_decision(conn, stage="admin", entity_type="user", entity_id=row[0], decision="reset_token_created",
                     reason=f"{user['full_name']} created a password reset token for {row[1]}",
                     inputs={"by": user["id"]}, actor="human")
    return {"token": token, "expires_in_minutes": RESET_TOKEN_MINUTES}


@router.get("/settings")
def get_settings(user: dict = Depends(admin_only)):
    with get_conn() as conn:
        limit = conn.execute("SELECT auto_pay_limit FROM companies WHERE id = %s",
                             (user["company_id"],)).fetchone()[0]
    return {"auto_pay_limit": None if limit is None else str(limit)}


@router.post("/settings")
def set_settings(body: SettingsIn, user: dict = Depends(admin_only)):
    """Each field is optional and changed only when sent."""
    value = None
    change_limit = "auto_pay_limit" in body.model_fields_set
    if change_limit and body.auto_pay_limit not in (None, ""):
        try:
            value = Decimal(str(body.auto_pay_limit))
        except InvalidOperation:
            raise HTTPException(422, "auto_pay_limit must be a number")
        if not value.is_finite() or value <= 0 or value > MAX_AUTO_PAY_LIMIT:
            raise HTTPException(
                422, f"auto_pay_limit must be above 0 and at most {MAX_AUTO_PAY_LIMIT}")
    with get_conn() as conn:
        if change_limit:
            conn.execute("UPDATE companies SET auto_pay_limit = %s WHERE id = %s", (value, user["company_id"]))
            log_decision(conn, stage="admin", entity_type="company", entity_id=user["company_id"],
                         decision="settings_changed",
                         reason=f"{user['full_name']} set the auto-pay limit to {value if value is not None else 'default'}",
                         inputs={"by": user["id"], "auto_pay_limit": None if value is None else str(value)},
                         actor="human")
        limit = conn.execute("SELECT auto_pay_limit FROM companies WHERE id = %s",
                             (user["company_id"],)).fetchone()[0]
    return {"auto_pay_limit": None if limit is None else str(limit)}
