"""Signup, login and 'who am I'. Signup creates a new company and its first admin user."""
from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from backend.auth import check_password, current_user, hash_password, make_token
from backend.db import get_conn

router = APIRouter(prefix="/auth", tags=["auth"])


class Signup(BaseModel):
    company_name: str
    full_name: str
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


def _user_out(user_id, company_id, company_name, email, full_name, role) -> dict:
    return {
        "id": str(user_id),
        "company_id": str(company_id),
        "company_name": company_name,
        "email": email,
        "full_name": full_name,
        "role": role,
    }


@router.post("/signup")
def signup(body: Signup):
    company_name = body.company_name.strip()
    full_name = body.full_name.strip()
    email = body.email.strip().lower()
    if not company_name or not full_name:
        raise HTTPException(422, "Company name and your name are required")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Enter a valid email")
    if not 8 <= len(body.password) <= 72:
        raise HTTPException(422, "Password must be 8 to 72 characters")
    try:
        with get_conn() as conn:
            company_id = conn.execute(
                "INSERT INTO companies (name) VALUES (%s) RETURNING id", (company_name,)
            ).fetchone()[0]
            user_id = conn.execute(
                "INSERT INTO users (company_id, email, password_hash, full_name, role) "
                "VALUES (%s, %s, %s, %s, 'admin') RETURNING id",
                (company_id, email, hash_password(body.password), full_name),
            ).fetchone()[0]
    except UniqueViolation:
        raise HTTPException(409, "An account with this email already exists")
    return {
        "token": make_token(user_id),
        "user": _user_out(user_id, company_id, company_name, email, full_name, "admin"),
    }


@router.post("/login")
def login(body: Login):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.company_id, c.name, u.email, u.full_name, u.role, u.password_hash "
            "FROM users u JOIN companies c ON c.id = u.company_id WHERE lower(u.email) = %s",
            (body.email.strip().lower(),),
        ).fetchone()
    if not row or not check_password(body.password, row[6]):
        raise HTTPException(401, "Wrong email or password")
    return {"token": make_token(row[0]), "user": _user_out(*row[:6])}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    with get_conn() as conn:
        name = conn.execute(
            "SELECT name FROM companies WHERE id = %s", (user["company_id"],)
        ).fetchone()[0]
    return _user_out(user["id"], user["company_id"], name, user["email"], user["full_name"], user["role"])