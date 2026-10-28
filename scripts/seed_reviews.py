"""SYNTHETIC past reviews for case memory. Safe to re-run (deletes its own SYN-* rows first)."""
from psycopg.types.json import Jsonb

from backend.audit import log_decision
from backend.db import get_conn

SIGNALS = ["amount_near_limit", "bank_details_differ", "new_payee"]

# (vendor invoice name, number, signals, human decision, final status, confirmed, note)
CASES = [
    ("INV-6001", "SYN-MER-01", SIGNALS, "reject", "rejected", False,
     "SYNTHETIC: Vendor did not recognise the new bank details when we called."),
    ("INV-6001", "SYN-MER-02", SIGNALS, "reject", "rejected", False,
     "SYNTHETIC: Same pattern as the last one, bank change came by email only."),
    ("INV-6001", "SYN-MER-03", SIGNALS, "approve", "paid", True,
     "SYNTHETIC: Called the vendor on the number we already had, they confirmed the bank change."),
    ("INV-6001", "SYN-MER-04", SIGNALS, "reject", "rejected", False,
     "SYNTHETIC: Amount just under the limit with new bank details, rejected."),
    ("INV-1001", "SYN-NW-01", ["new_payee"], "approve", "paid", False,
     "SYNTHETIC: First invoice from a real vendor, PO and delivery matched."),
]

with get_conn() as conn:
    # clean up earlier runs
    ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE invoice_number LIKE 'SYN-%'").fetchall()]
    if ids:
        conn.execute("DELETE FROM reviews WHERE invoice_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM audit_log WHERE entity_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM invoices WHERE id = ANY(%s)", (ids,))

    for like, number, signals, decision, final, confirmed, note in CASES:
        vendor_id = conn.execute(
            "SELECT vendor_id FROM invoices WHERE invoice_number = %s", (like,)).fetchone()[0]
        inv_id = conn.execute(
            """INSERT INTO invoices (vendor_id, invoice_number, amount, currency, status)
               VALUES (%s, %s, 4800.00, 'USD', %s) RETURNING id""",
            (vendor_id, number, final)).fetchone()[0]
        log_decision(conn, stage="fraud", entity_type="invoice", entity_id=inv_id,
                     decision="high", reason=f"Fraud risk high (SYNTHETIC) from {len(signals)} signal(s)",
                     inputs={"signals": signals})
        conn.execute(
            """INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note,
                                    confirmed_with_vendor, status_before, status_after)
               VALUES (%s, 'hold', %s, 'SYNTHETIC reviewer', %s, %s, 'held', %s)""",
            (inv_id, decision, note, confirmed, "approved" if decision == "approve" else "rejected"))
    conn.commit()
    print("seeded", len(CASES), "synthetic reviews")