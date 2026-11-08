"""Adds a judge demo to the CURRENT database without erasing anything. SYNTHETIC data only.

    python -m scripts.seed_judge                 # real AI (needs GEMINI_API_KEY)
    python -m scripts.seed_judge --stub-ai       # code layers only, no Gemini calls

Creates one company "PayMaster Judge Demo (SYNTHETIC)" with
  * an owner/admin (uploads the invoices, so the four-eyes rule lets the judge release them)
  * a judge login with the REVIEWER role (approve, reject, pay; cannot change settings or people)
  * five vendors, POs, deliveries and five invoices that tell the story:
      clean -> approved | changed bank details -> held | no delivery -> held |
      copy of an earlier invoice -> rejected | over the auto-pay limit -> needs a person
Passwords come from JUDGE_PASSWORD / OWNER_PASSWORD, or are generated and printed ONCE.
Refuses to run twice on the same database (the company name is unique here).
"""
import argparse
import os
import secrets
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.auth import hash_password  # noqa: E402
from backend.db import get_conn  # noqa: E402
from backend.tools import normalize_po_number  # noqa: E402

COMPANY = "PayMaster Judge Demo (SYNTHETIC)"
JUDGE_EMAIL = os.environ.get("JUDGE_EMAIL", "judge@paymaster-demo.example.com")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@paymaster-demo.example.com")

# name, country, payout email, PO amount
VENDORS = [
    ("Northwind Stationers", "US", "ap@northwind-demo.example.com", "1200.00"),
    ("Kestrel Logistics", "GB", "ap@kestrel-demo.example.com", "2400.00"),
    ("Bluefin Packaging", "US", "ap@bluefin-demo.example.com", "860.00"),
    ("Orchard Cloud Services", "US", "ap@orchard-demo.example.com", "950.00"),
    ("Summit IT Support", "US", "ap@summit-demo.example.com", "9000.00"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub-ai", action="store_true")
    args = ap.parse_args()
    if args.stub_ai:
        from scripts.eval import install_hostile_stub
        install_hostile_stub()
    from backend.investigator import investigate

    judge_pw = os.environ.get("JUDGE_PASSWORD") or secrets.token_urlsafe(12)
    owner_pw = os.environ.get("OWNER_PASSWORD") or secrets.token_urlsafe(12)
    for pw in (judge_pw, owner_pw):
        if len(pw) < 10:
            sys.exit("Passwords must be at least 10 characters.")

    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM companies WHERE name = %s", (COMPANY,)).fetchone():
            sys.exit(f"'{COMPANY}' already exists in this database. Nothing changed.")
        cid = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (COMPANY,)).fetchone()[0]
        owner = conn.execute(
            "INSERT INTO users (company_id, email, password_hash, full_name, role) VALUES (%s,%s,%s,%s,'admin') RETURNING id",
            (cid, OWNER_EMAIL, hash_password(owner_pw), "Demo Owner")).fetchone()[0]
        conn.execute(
            "INSERT INTO users (company_id, email, password_hash, full_name, role) VALUES (%s,%s,%s,%s,'reviewer')",
            (cid, JUDGE_EMAIL, hash_password(judge_pw), "Hackathon Judge"))
        v = {}
        for name, country, email, amount in VENDORS:
            vid = conn.execute("INSERT INTO vendors (name, country, company_id) VALUES (%s,%s,%s) RETURNING id",
                               (name, country, cid)).fetchone()[0]
            conn.execute("INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
                         "VALUES (%s,%s,'onboarding',%s)", (vid, email, cid))
            po = normalize_po_number(f"PO-{uuid.uuid4().hex[:6].upper()}")
            po_id = conn.execute(
                "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) "
                "VALUES (%s,%s,%s,'USD',%s) RETURNING id", (po, vid, amount, cid)).fetchone()[0]
            v[name] = dict(vid=vid, po_id=po_id, email=email, amount=amount, po=po)

        def deliver(name):
            x = v[name]
            conn.execute("INSERT INTO deliveries (po_id, description, amount, company_id) VALUES (%s,'Goods received',%s,%s)",
                         (x["po_id"], x["amount"], cid))
        for n in ("Northwind Stationers", "Kestrel Logistics", "Orchard Cloud Services", "Summit IT Support"):
            deliver(n)      # Bluefin gets NO delivery on purpose

        def invoice(name, number, email=None):
            x = v[name]
            return conn.execute(
                "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
                "company_id, uploaded_by, received_at) VALUES (%s,%s,%s,%s,'USD',%s,'received',%s,%s,clock_timestamp()) "
                "RETURNING id", (x["vid"], x["po_id"], number, x["amount"], email or x["email"], cid, owner)).fetchone()[0]

        story = [
            ("1. Clean invoice", invoice("Northwind Stationers", "NW-1001")),
            ("2. Bank details changed", invoice("Kestrel Logistics", "KL-2044", "payments@kestrel-logistics-pay.example.net")),
            ("3. Billed before delivery", invoice("Bluefin Packaging", "BF-310")),
            ("4. Copy of an earlier invoice", invoice("Northwind Stationers", "nw 1001.")),
            ("5. Over the auto-pay limit", invoice("Summit IT Support", "SM-777")),
        ]
    print("Running Verify on the five invoices...")
    for label, inv in story:
        r = investigate(inv)
        print(f"  {label:34} -> {r['status']}")
    print(f"""
Company : {COMPANY}
Judge   : {JUDGE_EMAIL}   (role: reviewer)
Password: {judge_pw}
Owner   : {OWNER_EMAIL}   (role: admin, uploaded the invoices)
Password: {owner_pw}
Save these now; they are not stored anywhere in readable form.""")


if __name__ == "__main__":
    main()
