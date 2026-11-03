"""'Needs attention': everything that is waiting for a person or for another record."""
from fastapi import APIRouter, Depends, HTTPException

from backend.auth import current_member, reviewer_or_admin
from backend.reprocess import retry_extraction, reverify_invoice
from backend.db import get_conn

router = APIRouter(tags=["attention"])


@router.get("/attention")
def attention(user: dict = Depends(current_member)):
    cid = user["company_id"]
    with get_conn() as conn:
        waiting_delivery = conn.execute(
            """SELECT i.id, i.invoice_number, v.name, i.amount, i.currency, po.po_number,
                      COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0),
                      EXTRACT(DAY FROM now() - i.received_at)::int
               FROM invoices i JOIN vendors v ON v.id = i.vendor_id
               JOIN purchase_orders po ON po.id = i.po_id
               WHERE i.company_id = %s AND i.status = 'held'
                 AND COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0) < i.amount
               ORDER BY i.received_at""", (cid,)).fetchall()

        needs_fix = conn.execute(
            """SELECT id, source_name, reason, EXTRACT(DAY FROM now() - created_at)::int
               FROM extractions WHERE company_id = %s AND status = 'needs_review'
               ORDER BY created_at""", (cid,)).fetchall()

        ready_to_pay = conn.execute(
            """SELECT i.id, i.invoice_number, v.name, i.amount, i.currency,
                      EXTRACT(DAY FROM now() - i.received_at)::int
               FROM invoices i JOIN vendors v ON v.id = i.vendor_id
               WHERE i.company_id = %s AND i.status = 'approved'
               ORDER BY i.received_at""", (cid,)).fetchall()

        no_invoice = conn.execute(
            """SELECT po.id, po.po_number, v.name, po.amount, po.currency,
                      EXTRACT(DAY FROM now() - (SELECT MAX(d.received_at) FROM deliveries d WHERE d.po_id = po.id))::int
               FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id
               WHERE po.company_id = %s
                 AND COALESCE((SELECT SUM(d.amount) FROM deliveries d WHERE d.po_id = po.id), 0) >= po.amount
                 AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.po_id = po.id)
               ORDER BY po.created_at""", (cid,)).fetchall()

    out = {
        "waiting_delivery": [{"id": str(r[0]), "invoice": r[1], "vendor": r[2], "amount": float(r[3]),
                              "currency": r[4], "po": r[5], "delivered": float(r[6]), "days": r[7]}
                             for r in waiting_delivery],
        "needs_fix": [{"id": str(r[0]), "file": r[1], "reason": r[2], "days": r[3]} for r in needs_fix],
        "ready_to_pay": [{"id": str(r[0]), "invoice": r[1], "vendor": r[2], "amount": float(r[3]),
                          "currency": r[4], "days": r[5]} for r in ready_to_pay],
        "delivered_no_invoice": [{"id": str(r[0]), "po": r[1], "vendor": r[2], "amount": float(r[3]),
                                  "currency": r[4], "days": r[5]} for r in no_invoice],
    }
    out["total"] = sum(len(v) for v in out.values())
    return out

@router.post("/invoices/{invoice_id}/reverify")
def reverify(invoice_id: str, user: dict = Depends(reviewer_or_admin)):
    """One click: run Verify again on a held invoice (e.g. its PO or delivery has arrived since)."""
    try:
        return reverify_invoice(invoice_id, user["company_id"])
    except LookupError:
        raise HTTPException(404, "Invoice not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/extractions/{extraction_id}/retry")
def retry(extraction_id: str, user: dict = Depends(reviewer_or_admin)):
    """One click: read an upload again (e.g. the AI service was overloaded the first time)."""
    try:
        return retry_extraction(extraction_id, user["company_id"])
    except LookupError:
        raise HTTPException(404, "Upload not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
