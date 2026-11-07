"""Purchase orders and deliveries: list and create."""
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import current_member, admin_only
from backend.audit import log_decision
from backend.db import get_conn
from backend.reprocess import reprocess_po
from backend.tools import normalize_po_number
import hashlib

from fastapi import File, UploadFile
from fastapi.responses import Response

router = APIRouter(tags=["purchase orders"])


class POIn(BaseModel):
    vendor_id: str
    po_number: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)  # optional (B6)


class DeliveryIn(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)  # optional (B6)

class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=300)


def _po(r):
    return {"id": str(r[0]), "po_number": r[1], "vendor": r[2], "amount": float(r[3]),
            "currency": r[4], "status": r[5], "delivered": float(r[6]),
            "quantity": None if r[7] is None else float(r[7])}


PO_SELECT = """
    SELECT po.id, po.po_number, v.name, po.amount, po.currency, po.status,
           COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0), po.quantity
    FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id
"""


@router.get("/pos")
def list_pos(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(PO_SELECT + " WHERE po.company_id = %s ORDER BY po.created_at DESC",
                            (user["company_id"],)).fetchall()
    return {"items": [_po(r) for r in rows]}


@router.post("/pos")
def create_po(body: POIn, background: BackgroundTasks, user: dict = Depends(admin_only)):
    number = normalize_po_number(body.po_number)
    with get_conn() as conn:
        ok = conn.execute("SELECT deactivated_at FROM vendors WHERE id::text = %s AND company_id = %s",
                          (body.vendor_id, user["company_id"])).fetchone()
        if not ok:
            raise HTTPException(404, "vendor not found")
        if ok[0] is not None:
            raise HTTPException(409, "This vendor is deactivated")
        if conn.execute("SELECT 1 FROM purchase_orders WHERE po_number = %s AND company_id = %s",
                        (number, user["company_id"])).fetchone():
            raise HTTPException(409, f"{number} already exists")
        po_id = conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id, quantity) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (number, body.vendor_id, body.amount, body.currency.upper(), user["company_id"], body.quantity),
        ).fetchone()[0]
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=po_id, decision="po_created",
                     reason=f"{user['full_name']} created {number} for {body.amount} {body.currency.upper()}",
                     inputs={"by": user["id"], "vendor_id": body.vendor_id}, actor="human",
                     company_id=user["company_id"])
        row = conn.execute(PO_SELECT + " WHERE po.id = %s", (po_id,)).fetchone()
    background.add_task(reprocess_po, user["company_id"], number)   # B8: uploads that were waiting for this PO
    return _po(row)


@router.get("/deliveries")
def list_deliveries(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.id, po.po_number, d.description, d.amount, d.received_at, d.quantity "
            "FROM deliveries d JOIN purchase_orders po ON po.id = d.po_id "
            "WHERE d.company_id = %s ORDER BY d.received_at DESC", (user["company_id"],),
        ).fetchall()
    return {"items": [{"id": str(r[0]), "po_number": r[1], "description": r[2],
                       "amount": float(r[3]), "received_at": r[4].isoformat(),
                       "quantity": None if r[5] is None else float(r[5])} for r in rows]}


@router.post("/pos/{po_id}/deliveries")
def add_delivery(po_id: str, body: DeliveryIn, background: BackgroundTasks, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        po = conn.execute("SELECT po_number FROM purchase_orders WHERE id::text = %s AND company_id = %s FOR UPDATE",
                          (po_id, user["company_id"])).fetchone()
        if not po:
            raise HTTPException(404, "PO not found")
        _assert_po_open(conn, po_id)
        r = conn.execute(
            "INSERT INTO deliveries (po_id, description, amount, company_id, quantity) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, received_at",
            (po_id, body.description.strip(), body.amount, user["company_id"], body.quantity),
        ).fetchone()
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=po_id, decision="delivery_recorded",
                     reason=f"{user['full_name']} recorded a delivery of {body.amount} on {po[0]}",
                     inputs={"by": user["id"], "amount": str(body.amount)}, actor="human",
                     company_id=user["company_id"])
    background.add_task(reprocess_po, user["company_id"], po[0])   # B8: re-check held invoices on this PO
    return {"id": str(r[0]), "po_number": po[0], "description": body.description.strip(),
            "amount": float(body.amount), "received_at": r[1].isoformat()}

@router.post("/pos/{po_id}/deliver-all")
def deliver_all(po_id: str, background: BackgroundTasks, user: dict = Depends(admin_only)):
    """Record one delivery for whatever part of the PO is not yet delivered."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT po.po_number, po.amount, "
            "COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0), "
            "po.quantity, COALESCE((SELECT SUM(d.quantity) FROM deliveries d WHERE d.po_id = po.id), 0) "
            "FROM purchase_orders po WHERE po.id::text = %s AND po.company_id = %s FOR UPDATE OF po",
            (po_id, user["company_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "PO not found")
        _assert_po_open(conn, po_id)
        remaining = row[1] - row[2]
        if remaining <= 0:
            raise HTTPException(409, "PO is already fully delivered")
        r = conn.execute(
            "INSERT INTO deliveries (po_id, description, amount, company_id, quantity) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, received_at",
            (po_id, "Fully delivered (marked in console)", remaining, user["company_id"],
             (row[3] - row[4]) if row[3] is not None and row[3] - row[4] > 0 else None),
        ).fetchone()
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=po_id, decision="delivery_recorded",
                     reason=f"{user['full_name']} marked {row[0]} fully delivered ({remaining})",
                     inputs={"by": user["id"], "amount": str(remaining)}, actor="human",
                     company_id=user["company_id"])
    background.add_task(reprocess_po, user["company_id"], row[0])
    return {"id": str(r[0]), "po_number": row[0], "amount": float(remaining),
            "received_at": r[1].isoformat()}

def _delivery_for_update(conn, delivery_id, company_id):
    d = conn.execute(
        "SELECT d.id, d.po_id, po.po_number, d.description, d.amount, d.quantity "
        "FROM deliveries d JOIN purchase_orders po ON po.id = d.po_id "
        "WHERE d.id::text = %s AND d.company_id = %s FOR UPDATE OF d",
        (delivery_id, company_id)).fetchone()
    if not d:
        raise HTTPException(404, "Delivery not found")
    if conn.execute(
            "SELECT 1 FROM invoices i JOIN payments p ON p.invoice_id = i.id "
            "WHERE i.po_id = %s AND p.status NOT IN ('failed', 'returned') LIMIT 1", (d[1],)).fetchone():
        raise HTTPException(409, "An invoice on this PO has already been paid or is being paid, so this delivery is locked")
    return d


class DeliveryEdit(DeliveryIn):
    reason: str = Field(min_length=10, max_length=300)


@router.post("/deliveries/{delivery_id}/edit")
def edit_delivery(delivery_id: str, body: DeliveryEdit, background: BackgroundTasks, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        d = _delivery_for_update(conn, delivery_id, user["company_id"])
        conn.execute("UPDATE deliveries SET description = %s, amount = %s, quantity = %s WHERE id = %s",
                     (body.description.strip(), body.amount, body.quantity, d[0]))
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=d[1], decision="delivery_edited",
                     reason=f"{user['full_name']} edited a delivery on {d[2]}: {body.reason.strip()}",
                     inputs={"by": user["id"], "delivery_id": str(d[0]),
                             "before": {"description": d[3], "amount": str(d[4]), "quantity": None if d[5] is None else str(d[5])},
                             "after": {"description": body.description.strip(), "amount": str(body.amount),
                                       "quantity": None if body.quantity is None else str(body.quantity)}},
                     actor="human", company_id=user["company_id"])
    background.add_task(reprocess_po, user["company_id"], d[2])
    return {"ok": True}


@router.post("/deliveries/{delivery_id}/delete")
def delete_delivery(delivery_id: str, body: ReasonIn, background: BackgroundTasks, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        d = _delivery_for_update(conn, delivery_id, user["company_id"])
        conn.execute("DELETE FROM deliveries WHERE id = %s", (d[0],))
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=d[1], decision="delivery_deleted",
                     reason=f"{user['full_name']} deleted a delivery on {d[2]}: {body.reason.strip()}",
                     inputs={"by": user["id"], "delivery_id": str(d[0]), "description": d[3],
                             "amount": str(d[4]), "quantity": None if d[5] is None else str(d[5])},
                     actor="human", company_id=user["company_id"])
    background.add_task(reprocess_po, user["company_id"], d[2])
    return {"ok": True}

class POEdit(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    reason: str = Field(min_length=10, max_length=300)


def _po_for_update(conn, po_id, company_id):
    po = conn.execute(
        "SELECT id, po_number, amount, currency, quantity, status FROM purchase_orders "
        "WHERE id::text = %s AND company_id = %s FOR UPDATE", (po_id, company_id)).fetchone()
    if not po:
        raise HTTPException(404, "PO not found")
    if po[5] == "cancelled":
        raise HTTPException(409, "This PO is cancelled")
    if conn.execute(
            "SELECT 1 FROM invoices i JOIN payments p ON p.invoice_id = i.id "
            "WHERE i.po_id = %s AND p.status NOT IN ('failed', 'returned') LIMIT 1", (po[0],)).fetchone():
        raise HTTPException(409, "An invoice on this PO has been paid or is being paid, so the PO is locked")
    return po


def _assert_po_open(conn, po_id):
    if conn.execute("SELECT 1 FROM purchase_orders WHERE id::text = %s AND status = 'cancelled'",
                    (po_id,)).fetchone():
        raise HTTPException(409, "This PO is cancelled")


@router.post("/pos/{po_id}/edit")
def edit_po(po_id: str, body: POEdit, background: BackgroundTasks, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        po = _po_for_update(conn, po_id, user["company_id"])
        delivered = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM deliveries WHERE po_id = %s", (po[0],)).fetchone()[0]
        if body.amount < delivered:
            raise HTTPException(409, f"Amount cannot be below what is already delivered ({delivered})")
        conn.execute("UPDATE purchase_orders SET amount = %s, currency = %s, quantity = %s WHERE id = %s",
                     (body.amount, body.currency.upper(), body.quantity, po[0]))
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=po[0], decision="po_edited",
                     reason=f"{user['full_name']} edited {po[1]}: {body.reason.strip()}",
                     inputs={"by": user["id"],
                             "before": {"amount": str(po[2]), "currency": po[3], "quantity": None if po[4] is None else str(po[4])},
                             "after": {"amount": str(body.amount), "currency": body.currency.upper(),
                                       "quantity": None if body.quantity is None else str(body.quantity)}},
                     actor="human", company_id=user["company_id"])
    background.add_task(reprocess_po, user["company_id"], po[1])
    return {"ok": True}


@router.post("/pos/{po_id}/cancel")
def cancel_po(po_id: str, body: ReasonIn, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        po = _po_for_update(conn, po_id, user["company_id"])
        if conn.execute("SELECT 1 FROM invoices WHERE po_id = %s AND status IN ('held', 'approved', 'paid') LIMIT 1",
                        (po[0],)).fetchone():
            raise HTTPException(409, "This PO still has held, approved or paid invoices. Reject them first")
        conn.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE id = %s", (po[0],))
        log_decision(conn, stage="po", entity_type="purchase_order", entity_id=po[0], decision="po_cancelled",
                     reason=f"{user['full_name']} cancelled {po[1]}: {body.reason.strip()}",
                     inputs={"by": user["id"], "status_before": po[5]}, actor="human",
                     company_id=user["company_id"])
    return {"ok": True}

ALLOWED_TYPES = {"application/pdf", "image/png", "image/jpeg"}
MAX_FILE = 5 * 1024 * 1024


def _save_attachment(conn, user, entity_type, entity_id, file: UploadFile):
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Only PDF, PNG or JPG files are accepted")
    data = file.file.read(MAX_FILE + 1)
    if not data:
        raise HTTPException(422, "The file is empty")
    if len(data) > MAX_FILE:
        raise HTTPException(413, "The file is larger than 5 MB")
    if not (data.startswith(b"%PDF") or data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
        raise HTTPException(415, "That does not look like a real PDF, PNG or JPG")
    name = (file.filename or "file")[:120]
    aid = conn.execute(
        "INSERT INTO attachments (company_id, entity_type, entity_id, filename, content_type, sha256, data, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user["company_id"], entity_type, entity_id, name, file.content_type,
         hashlib.sha256(data).hexdigest(), data, user["id"])).fetchone()[0]
    log_decision(conn, stage="po", entity_type=entity_type, entity_id=entity_id, decision="file_attached",
                 reason=f"{user['full_name']} attached {name}",
                 inputs={"by": user["id"], "attachment_id": str(aid), "bytes": len(data)},
                 actor="human", company_id=user["company_id"])
    return {"id": str(aid), "filename": name}


@router.post("/pos/{po_id}/attachments")
def attach_to_po(po_id: str, file: UploadFile = File(...), user: dict = Depends(admin_only)):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM purchase_orders WHERE id::text = %s AND company_id = %s",
                           (po_id, user["company_id"])).fetchone()
        if not row:
            raise HTTPException(404, "PO not found")
        return _save_attachment(conn, user, "purchase_order", row[0], file)


@router.post("/deliveries/{delivery_id}/attachments")
def attach_to_delivery(delivery_id: str, file: UploadFile = File(...), user: dict = Depends(admin_only)):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM deliveries WHERE id::text = %s AND company_id = %s",
                           (delivery_id, user["company_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Delivery not found")
        return _save_attachment(conn, user, "delivery", row[0], file)


@router.get("/attachments")
def list_attachments(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, entity_type, entity_id, filename FROM attachments WHERE company_id = %s ORDER BY created_at",
            (user["company_id"],)).fetchall()
    return {"items": [{"id": str(r[0]), "entity_type": r[1], "entity_id": str(r[2]), "filename": r[3]} for r in rows]}


@router.get("/attachments/{attachment_id}")
def open_attachment(attachment_id: str, user: dict = Depends(current_member)):
    with get_conn() as conn:
        r = conn.execute("SELECT content_type, data FROM attachments WHERE id::text = %s AND company_id = %s",
                         (attachment_id, user["company_id"])).fetchone()
    if not r:
        raise HTTPException(404, "File not found")
    return Response(content=bytes(r[1]), media_type=r[0])


@router.post("/attachments/{attachment_id}/delete")
def delete_attachment(attachment_id: str, body: ReasonIn, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        r = conn.execute("SELECT id, entity_type, entity_id, filename, sha256 FROM attachments "
                         "WHERE id::text = %s AND company_id = %s FOR UPDATE",
                         (attachment_id, user["company_id"])).fetchone()
        if not r:
            raise HTTPException(404, "File not found")
        conn.execute("DELETE FROM attachments WHERE id = %s", (r[0],))
        log_decision(conn, stage="po", entity_type=r[1], entity_id=r[2], decision="file_removed",
                     reason=f"{user['full_name']} removed {r[3]}: {body.reason.strip()}",
                     inputs={"by": user["id"], "sha256": r[4]}, actor="human", company_id=user["company_id"])
    return {"ok": True}