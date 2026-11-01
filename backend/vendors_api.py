"""Vendors: list, add, edit. A payout email change is written to vendor_bank_history."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import current_member
from backend.db import get_conn

router = APIRouter(prefix="/vendors", tags=["vendors"])

SELECT = """
    SELECT v.id, v.name, v.country, h.destination_email
    FROM vendors v
    LEFT JOIN vendor_bank_history h ON h.vendor_id = v.id AND h.valid_to IS NULL
"""


def _row(r):
    return {"id": str(r[0]), "name": r[1], "country": r[2], "payout_email": r[3]}


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=2, max_length=60)
    payout_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class VendorEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    country: str | None = Field(default=None, min_length=2, max_length=60)
    payout_email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("")
def list_vendors(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(SELECT + " WHERE v.company_id = %s ORDER BY v.name",
                            (user["company_id"],)).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.post("")
def add_vendor(body: VendorIn, user: dict = Depends(current_member)):
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT 1 FROM vendors WHERE company_id = %s AND lower(name) = lower(%s)",
            (user["company_id"], body.name.strip()),
        ).fetchone()
        if dup:
            raise HTTPException(409, "A vendor with this name already exists")
        vid = conn.execute(
            "INSERT INTO vendors (name, country, is_synthetic, company_id) "
            "VALUES (%s, %s, FALSE, %s) RETURNING id",
            (body.name.strip(), body.country.strip(), user["company_id"]),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source) "
            "VALUES (%s, %s, 'onboarding')",
            (vid, body.payout_email.strip().lower()),
        )
        row = conn.execute(SELECT + " WHERE v.id = %s", (vid,)).fetchone()
    return _row(row)


@router.post("/{vendor_id}")
def edit_vendor(vendor_id: str, body: VendorEdit, user: dict = Depends(current_member)):
    with get_conn() as conn:
        v = conn.execute(
            "SELECT 1 FROM vendors WHERE id = %s AND company_id = %s",
            (vendor_id, user["company_id"]),
        ).fetchone()
        if not v:
            raise HTTPException(404, "vendor not found")
        if body.name is not None or body.country is not None:
            conn.execute(
                "UPDATE vendors SET name = COALESCE(%s, name), country = COALESCE(%s, country) "
                "WHERE id = %s",
                (body.name and body.name.strip(), body.country and body.country.strip(), vendor_id),
            )
        if body.payout_email is not None:
            new = body.payout_email.strip().lower()
            cur = conn.execute(
                "SELECT destination_email FROM vendor_bank_history "
                "WHERE vendor_id = %s AND valid_to IS NULL", (vendor_id,),
            ).fetchone()
            if not cur or cur[0].lower() != new:
                conn.execute(
                    "UPDATE vendor_bank_history SET valid_to = now() "
                    "WHERE vendor_id = %s AND valid_to IS NULL", (vendor_id,),
                )
                conn.execute(
                    "INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source) "
                    "VALUES (%s, %s, 'manual_edit')", (vendor_id, new),
                )
        row = conn.execute(SELECT + " WHERE v.id = %s", (vendor_id,)).fetchone()
    return _row(row)