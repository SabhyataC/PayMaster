"""Demo data button (Team page): adds a small SYNTHETIC story to the signed-in admin's OWN company.

It never touches another company and never deletes anything. Off unless ALLOW_DEMO_DATA=1, so a real
deployment cannot be filled with fake invoices by accident. One load per company.

The five invoices:  clean -> approved | changed bank details -> held | billed before delivery -> held |
copy of an earlier invoice -> rejected | over the auto-pay limit -> approved by Verify, held when you press Pay.
Payout emails of the clean vendors are the PayPal sandbox accounts, so approved payouts reach SUCCESS.
The attacker email of the bank-change invoice is fake on purpose."""
import os
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from backend.audit import log_decision
from backend.auth import admin_only
from backend.db import get_conn
from backend.tools import normalize_po_number

router = APIRouter(prefix="/admin", tags=["admin"])

# name, country, payout email, PO amount
VENDORS = [
    ("Northwind Stationers", "US", "paymaster-vendor-us@example.com", "1200.00"),
    ("Kestrel Logistics", "GB", "paymaster-vendor-uk@example.com", "2400.00"),
    ("Bluefin Packaging", "IN", "paymaster-vendor-in@example.com", "860.00"),
    ("Orchard Cloud Services", "DE", "paymaster-vendor-gm@example.com", "950.00"),
    ("Summit IT Support", "US", "paymaster-vendor-us2@example.com", "9000.00"),
]
NO_DELIVERY = "Bluefin Packaging"
ATTACKER_EMAIL = "payments@kestrel-logistics-pay.example.net"   # fake on purpose
STORY = [  # label, vendor, invoice number, payout email override
    ("Clean invoice", "Northwind Stationers", "NW-1001", None),
    ("Bank details changed", "Kestrel Logistics", "KL-2044", ATTACKER_EMAIL),
    ("Billed before delivery", "Bluefin Packaging", "BF-310", None),
    ("Copy of an earlier invoice", "Northwind Stationers", "nw 1001.", None),
    ("Over the auto-pay limit", "Summit IT Support", "SM-777", None),
]


def enabled():
    return os.environ.get("ALLOW_DEMO_DATA") == "1"


def _loaded(conn, cid):
    return conn.execute("SELECT count(*) FROM vendors WHERE company_id = %s AND name = ANY(%s)",
                        (cid, [v[0] for v in VENDORS])).fetchone()[0] > 0


def _new_po_number(conn):
    while True:
        po = normalize_po_number(f"PO-{secrets.randbelow(900000) + 100000}")
        if not conn.execute("SELECT 1 FROM purchase_orders WHERE po_number = %s", (po,)).fetchone():
            return po


def load(conn, cid, user_id):
    """Insert the story. Returns [(label, invoice_id)]. Caller commits."""
    v = {}
    for name, country, email, amount in VENDORS:
        vid = conn.execute("INSERT INTO vendors (name, country, company_id) VALUES (%s,%s,%s) RETURNING id",
                           (name, country, cid)).fetchone()[0]
        conn.execute("INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
                     "VALUES (%s,%s,'onboarding',%s)", (vid, email, cid))
        po = _new_po_number(conn)
        po_id = conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) "
            "VALUES (%s,%s,%s,'USD',%s) RETURNING id", (po, vid, amount, cid)).fetchone()[0]
        if name != NO_DELIVERY:
            conn.execute("INSERT INTO deliveries (po_id, description, amount, company_id) VALUES (%s,'Goods received',%s,%s)",
                         (po_id, amount, cid))
        v[name] = dict(vid=vid, po_id=po_id, email=email, amount=amount)
    out = []
    for label, name, number, email in STORY:
        x = v[name]
        inv = conn.execute(
            "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
            "company_id, uploaded_by, received_at) VALUES (%s,%s,%s,%s,'USD',%s,'received',%s,%s,clock_timestamp()) "
            "RETURNING id", (x["vid"], x["po_id"], number, x["amount"], email or x["email"], cid, user_id)).fetchone()[0]
        out.append((label, inv))
    return out


def verify_all(invoice_ids):
    from backend.investigator import investigate   # imported here: it pulls in the AI client
    for inv in invoice_ids:
        try:
            investigate(inv)
        except Exception:
            pass   # one failure must not stop the rest; the invoice simply stays 'received' for a human


@router.get("/demo-data")
def status(user: dict = Depends(admin_only)):
    with get_conn() as conn:
        return {"enabled": enabled(), "loaded": _loaded(conn, user["company_id"])}


@router.post("/demo-data")
def add_demo_data(background: BackgroundTasks, user: dict = Depends(admin_only)):
    if not enabled():
        raise HTTPException(403, "Demo data is switched off on this server (set ALLOW_DEMO_DATA=1)")
    with get_conn() as conn:
        if _loaded(conn, user["company_id"]):
            raise HTTPException(409, "Demo data is already in this company")
        story = load(conn, user["company_id"], user["id"])
        log_decision(conn, stage="admin", entity_type="company", entity_id=user["company_id"], decision="demo_data_loaded",
                     reason=f"{user['full_name']} added the synthetic demo data (5 vendors, 5 invoices)",
                     inputs={"by": user["id"], "invoices": [str(i) for _, i in story]}, actor="human",
                     company_id=user["company_id"])
    background.add_task(verify_all, [i for _, i in story])
    return {"created": [{"label": l, "invoice_id": str(i)} for l, i in story],
            "note": "Verify is running on the five invoices; refresh the review queue in a minute."}
