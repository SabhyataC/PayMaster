"""
SYNTHETIC DATA ONLY.

Everything this script creates is fictional: vendor names, PO and invoice numbers,
amounts, and every email address (all end in .example.com, a domain reserved for
examples). No real company, person or bank account is represented.

Running this script ERASES all tables first, then rebuilds the demo company.
Do not run it against a database holding data you want to keep.
"""

from backend.db import get_conn

# (name, country, current PayPal email). Everything here is fake and marked synthetic.
VENDORS = [
    ("Northwind Stationers",   "US", "paymaster-vendor-us@example.com"),
    ("Kestrel Logistics",      "GB", "paymaster-vendor-uk@example.com"),
    ("Bluefin Packaging",      "IN", "paymaster-vendor-in@example.com"),
    ("Orchard Cloud Services", "DE", "paymaster-vendor-gm@example.com"),
    ("Summit IT Support",      "US", "paymaster-vendor-us2@example.com"),
    ("Meridian Print Works",   "DE", "ap@meridian-print.example.com"),
    ("Harbor Office Supplies", "US", "ap@harbor-office.example.com"),
    ("Lakeside Catering",      "CA", "ap@lakeside-catering.example.com"),
    ("Acme Supplies Ltd",      "GB", "ap@acme-supplies.example.com"),
    ("Acrne Supplies Ltd",     "GB", "ap@acrne-supplies.example.com"),
]


def wipe(conn):
    """DEV ONLY: empties every table so the seed can be re-run from scratch."""
    conn.execute(
        "TRUNCATE audit_log, ledger_entries, settlements, payments, invoices, "
        "deliveries, purchase_orders, vendor_bank_history, vendors CASCADE"
    )


def seed_vendors(conn):
    """Insert vendors plus their current payout email. Returns {name: vendor_id}."""
    ids = {}
    for name, country, email in VENDORS:
        vendor_id = conn.execute(
            "INSERT INTO vendors (name, country) VALUES (%s, %s) RETURNING id",
            (name, country),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source) "
            "VALUES (%s, %s, 'onboarding')",
            (vendor_id, email),
        )
        ids[name] = vendor_id
    return ids

# (vendor name, PO number, agreed amount, what was delivered)
PO_PLAN = [
    ("Northwind Stationers",   "PO-1001", 1200.00, "Office stationery order"),
    ("Kestrel Logistics",      "PO-1002", 3450.00, "Freight, Q4 shipments"),
    ("Bluefin Packaging",      "PO-1003",  860.50, "Packaging materials"),
    ("Orchard Cloud Services", "PO-1004", 2100.00, "Cloud hosting, October"),
    ("Summit IT Support",      "PO-1005", 1500.00, "IT support retainer"),
    ("Meridian Print Works",   "PO-1006", 4800.00, "Printed brochures"),
    ("Harbor Office Supplies", "PO-1007",  725.00, "Office furniture"),
    ("Acme Supplies Ltd",      "PO-1008", 2640.00, "Bulk supplies order"),
    # Lakeside Catering and Acrne Supplies deliberately have no PO.
]


def seed_pos_and_deliveries(conn, vendor_ids):
    """One PO and one full delivery per vendor in PO_PLAN. Returns {vendor name: po_id}."""
    po_ids = {}
    for name, po_number, amount, description in PO_PLAN:
        po_id = conn.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, amount) "
            "VALUES (%s, %s, %s) RETURNING id",
            (po_number, vendor_ids[name], amount),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO deliveries (po_id, description, amount) VALUES (%s, %s, %s)",
            (po_id, description, amount),
        )
        po_ids[name] = po_id
    return po_ids

def seed_invoices(conn, vendor_ids, po_ids):
    """Insert the invoices, including the planted traps. Returns how many were inserted."""
    email = {name: e for name, _, e in VENDORS}  # each vendor's email on file

    # (vendor, invoice number, amount, which vendor's PO it points to or None, payout email, status)
    plan = [
        # clean
        ("Northwind Stationers",   "INV-1001", 1200.00, "Northwind Stationers",   email["Northwind Stationers"],   "received"),
        ("Kestrel Logistics",      "INV-2001", 3450.00, "Kestrel Logistics",      email["Kestrel Logistics"],      "received"),
        ("Bluefin Packaging",      "INV-3001",  860.50, "Bluefin Packaging",      email["Bluefin Packaging"],      "received"),
        ("Orchard Cloud Services", "INV-4001", 2100.00, "Orchard Cloud Services", email["Orchard Cloud Services"], "received"),
        ("Summit IT Support",      "INV-5001", 1500.00, "Summit IT Support",      email["Summit IT Support"],      "received"),
        # changed bank details: invoice names a different payout email than the one on file
        ("Meridian Print Works",   "INV-6001", 4800.00, "Meridian Print Works",   "ap@meridian-print-billing.example.com", "received"),
        # duplicate: original, then the same invoice again with different capitalisation
        ("Harbor Office Supplies", "INV-7001",  725.00, "Harbor Office Supplies", email["Harbor Office Supplies"], "approved"),
        ("Harbor Office Supplies", "inv-7001",  725.00, "Harbor Office Supplies", email["Harbor Office Supplies"], "received"),
        # no PO
        ("Lakeside Catering",      "INV-8001",  640.00, None,                     email["Lakeside Catering"],      "received"),
        # look-alike vendor, pointing at the real Acme's PO
        ("Acrne Supplies Ltd",     "INV-9001", 2640.00, "Acme Supplies Ltd",      email["Acrne Supplies Ltd"],     "received"),
    ]

    for vendor, number, amount, po_vendor, dest_email, status in plan:
        conn.execute(
            "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, destination_email, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (vendor_ids[vendor], po_ids.get(po_vendor), number, amount, dest_email, status),
        )
    return len(plan)


def main():
    with get_conn() as conn:
        wipe(conn)
        vendor_ids = seed_vendors(conn)
        po_ids = seed_pos_and_deliveries(conn, vendor_ids)
        n_invoices = seed_invoices(conn, vendor_ids, po_ids)
        print(f"Seeded {len(vendor_ids)} SYNTHETIC vendors, {len(po_ids)} POs with deliveries, {n_invoices} invoices")


if __name__ == "__main__":
    main()