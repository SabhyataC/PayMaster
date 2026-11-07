"""Vendors: list, add, edit. A payout email change is written to vendor_bank_history."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from backend.changes import request_vendor_email
from backend.auth import current_member, admin_only
from backend.audit import log_decision
from backend.db import get_conn
from backend.reprocess import reprocess_vendor

router = APIRouter(prefix="/vendors", tags=["vendors"])

SELECT = """
    SELECT v.id, v.name, v.country, h.destination_email, v.deactivated_at
    FROM vendors v
    LEFT JOIN vendor_bank_history h ON h.vendor_id = v.id AND h.valid_to IS NULL
"""


def _row(r):
    return {"id": str(r[0]), "name": r[1], "country": r[2], "payout_email": r[3], "active": r[4] is None}


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
def add_vendor(body: VendorIn, user: dict = Depends(admin_only)):
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
            "INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
            "VALUES (%s, %s, 'onboarding', %s)",
            (vid, body.payout_email.strip().lower(), user["company_id"]),
        )
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=vid, decision="vendor_added",
                     reason=f"{user['full_name']} added vendor {body.name.strip()}",
                     inputs={"by": user["id"], "payout_email": body.payout_email.strip().lower()},
                     actor="human")
        row = conn.execute(SELECT + " WHERE v.id = %s", (vid,)).fetchone()
    return _row(row)


@router.post("/{vendor_id}")
def edit_vendor(vendor_id: str, body: VendorEdit, background: BackgroundTasks, user: dict = Depends(admin_only)):
    email_changed = False
    email_requested = False
    with get_conn() as conn:
        v = conn.execute(
            "SELECT name, country FROM vendors WHERE id::text = %s AND company_id = %s FOR UPDATE",
            (vendor_id, user["company_id"]),
        ).fetchone()
        if not v:
            raise HTTPException(404, "vendor not found")
        new_name = body.name.strip() if body.name is not None else v[0]
        new_country = body.country.strip() if body.country is not None else v[1]
        if (new_name, new_country) != (v[0], v[1]):
            if conn.execute("SELECT 1 FROM vendors WHERE company_id = %s AND lower(name) = lower(%s) AND id::text <> %s",
                            (user["company_id"], new_name, vendor_id)).fetchone():
                raise HTTPException(409, "A vendor with this name already exists")
            conn.execute("UPDATE vendors SET name = %s, country = %s WHERE id::text = %s",
                         (new_name, new_country, vendor_id))
            log_decision(conn, stage="vendor", entity_type="vendor", entity_id=vendor_id, decision="vendor_edited",
                         reason=f"{user['full_name']} edited vendor {v[0]}",
                         inputs={"by": user["id"], "before": {"name": v[0], "country": v[1]},
                                 "after": {"name": new_name, "country": new_country}},
                         actor="human", company_id=user["company_id"])
        if body.payout_email is not None:
            result = request_vendor_email(conn, user, vendor_id, body.payout_email)
            if result == "applied":
                email_changed = True        # held invoices get re-checked, as before
            elif result:
                email_requested = True
        row = conn.execute(SELECT + " WHERE v.id::text = %s", (vendor_id,)).fetchone()
    if email_changed:   # B8: held invoices of this vendor are checked again against the new email
        background.add_task(reprocess_vendor, user["company_id"], vendor_id)
    out = _row(row)
    if email_requested:
        out["email_change"] = "waiting for a second admin to approve"
    return out


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=300)


def _vendor_locked(conn, vendor_id, company_id):
    v = conn.execute("SELECT id, name FROM vendors WHERE id::text = %s AND company_id = %s FOR UPDATE",
                     (vendor_id, company_id)).fetchone()
    if not v:
        raise HTTPException(404, "vendor not found")
    return v


@router.post("/{vendor_id}/deactivate")
def deactivate_vendor(vendor_id: str, body: ReasonIn, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        v = _vendor_locked(conn, vendor_id, user["company_id"])
        if conn.execute("SELECT 1 FROM invoices WHERE vendor_id = %s AND status IN ('held', 'approved') LIMIT 1",
                        (v[0],)).fetchone():
            raise HTTPException(409, "This vendor has held or approved invoices. Resolve them first")
        conn.execute("UPDATE vendors SET deactivated_at = now() WHERE id = %s", (v[0],))
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=v[0], decision="vendor_deactivated",
                     reason=f"{user['full_name']} deactivated {v[1]}: {body.reason.strip()}",
                     inputs={"by": user["id"]}, actor="human", company_id=user["company_id"])
    return {"ok": True}


@router.post("/{vendor_id}/reactivate")
def reactivate_vendor(vendor_id: str, body: ReasonIn, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        v = _vendor_locked(conn, vendor_id, user["company_id"])
        conn.execute("UPDATE vendors SET deactivated_at = NULL WHERE id = %s", (v[0],))
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=v[0], decision="vendor_reactivated",
                     reason=f"{user['full_name']} reactivated {v[1]}: {body.reason.strip()}",
                     inputs={"by": user["id"]}, actor="human", company_id=user["company_id"])
    return {"ok": True}


@router.post("/{vendor_id}/delete")
def delete_vendor(vendor_id: str, body: ReasonIn, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        v = _vendor_locked(conn, vendor_id, user["company_id"])
        if conn.execute("SELECT 1 FROM purchase_orders WHERE vendor_id = %s LIMIT 1", (v[0],)).fetchone() or \
           conn.execute("SELECT 1 FROM invoices WHERE vendor_id = %s LIMIT 1", (v[0],)).fetchone():
            raise HTTPException(409, "This vendor has POs or invoices, so it cannot be deleted. Deactivate it instead")
        conn.execute("DELETE FROM vendor_bank_history WHERE vendor_id = %s", (v[0],))
        conn.execute("DELETE FROM vendors WHERE id = %s", (v[0],))
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=v[0], decision="vendor_deleted",
                     reason=f"{user['full_name']} deleted {v[1]}: {body.reason.strip()}",
                     inputs={"by": user["id"]}, actor="human", company_id=user["company_id"])
    return {"ok": True}