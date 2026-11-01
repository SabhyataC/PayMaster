"""Purchase orders and deliveries: list and create."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import current_member
from backend.db import get_conn
from backend.tools import normalize_po_number

router = APIRouter(tags=["purchase orders"])


class POIn(BaseModel):
    vendor_id: str
    po_number: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class DeliveryIn(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


def _po(r):
    return {"id": str(r[0]), "po_number": r[1], "vendor": r[2], "amount": float(r[3]),
            "currency": r[4], "status": r[5], "delivered": float(r[6])}


PO_SELECT = """
    SELECT po.id, po.po_number, v.name, po.amount, po.currency, po.status,
           COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0)
    FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id
"""


@router.get("/pos")
def list_pos(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(PO_SELECT + " WHERE po.company_id = %s ORDER BY po.created_at DESC",
                            (user["company_id"],)).fetchall()
    return {"items": [_po(r) for r in rows]}


@router.post("/pos")
def create_po(body: POIn, user: dict = Depends(current_member)):
    number = normalize_po_number(body.po_number)
    with get_conn() as conn:
        ok = conn.execute("SELECT 1 FROM vendors WHERE id = %s AND company_id = %s",
                          (body.vendor_id, user["company_id"])).fetchone()
        if not ok:
            raise HTTPException(404, "vendor not found")
        if conn.execute("SELECT 1 FROM purchase_orders WHERE po_number = %s", (number,)).fetchone():
            raise HTTPException(409, f"{number} already exists")
        po_id = conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (number, body.vendor_id, body.amount, body.currency.upper(), user["company_id"]),
        ).fetchone()[0]
        row = conn.execute(PO_SELECT + " WHERE po.id = %s", (po_id,)).fetchone()
    return _po(row)


@router.get("/deliveries")
def list_deliveries(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.id, po.po_number, d.description, d.amount, d.received_at "
            "FROM deliveries d JOIN purchase_orders po ON po.id = d.po_id "
            "WHERE d.company_id = %s ORDER BY d.received_at DESC", (user["company_id"],),
        ).fetchall()
    return {"items": [{"id": str(r[0]), "po_number": r[1], "description": r[2],
                       "amount": float(r[3]), "received_at": r[4].isoformat()} for r in rows]}


@router.post("/pos/{po_id}/deliveries")
def add_delivery(po_id: str, body: DeliveryIn, user: dict = Depends(current_member)):
    with get_conn() as conn:
        po = conn.execute("SELECT po_number FROM purchase_orders WHERE id = %s AND company_id = %s",
                          (po_id, user["company_id"])).fetchone()
        if not po:
            raise HTTPException(404, "PO not found")
        r = conn.execute(
            "INSERT INTO deliveries (po_id, description, amount, company_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id, received_at",
            (po_id, body.description.strip(), body.amount, user["company_id"]),
        ).fetchone()
    return {"id": str(r[0]), "po_number": po[0], "description": body.description.strip(),
            "amount": float(body.amount), "received_at": r[1].isoformat()}

@router.post("/pos/{po_id}/deliver-all")
def deliver_all(po_id: str, user: dict = Depends(current_member)):
    """Record one delivery for whatever part of the PO is not yet delivered."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT po.po_number, po.amount, "
            "COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0) "
            "FROM purchase_orders po WHERE po.id = %s AND po.company_id = %s",
            (po_id, user["company_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "PO not found")
        remaining = row[1] - row[2]
        if remaining <= 0:
            raise HTTPException(409, "PO is already fully delivered")
        r = conn.execute(
            "INSERT INTO deliveries (po_id, description, amount, company_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id, received_at",
            (po_id, "Fully delivered (marked in console)", remaining, user["company_id"]),
        ).fetchone()
    return {"id": str(r[0]), "po_number": row[0], "amount": float(remaining),
            "received_at": r[1].isoformat()}