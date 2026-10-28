"""One invoice's full story, in order: intake, verify, fraud, review, pay (policy lives here),
reconcile. Read-only. Used by scripts/show_audit.py and later by GET /audit/{invoice_id}."""


def find_invoice_id(conn, ref):
    """ref = invoice number (INV-2001) or invoice UUID."""
    row = conn.execute(
        "SELECT id FROM invoices WHERE id::text = %s OR invoice_number = %s "
        "ORDER BY received_at DESC LIMIT 1", (ref, ref)).fetchone()
    return row[0] if row else None


def get_timeline(conn, invoice_id):
    rows = conn.execute(
        """SELECT created_at, stage, entity_type, decision, actor, reason
           FROM audit_log
           WHERE entity_id = %(i)s
              OR entity_id IN (SELECT id FROM payments WHERE invoice_id = %(i)s)
              OR entity_id IN (SELECT id FROM extractions WHERE invoice_id = %(i)s)
           ORDER BY id""", {"i": invoice_id}).fetchall()
    return [{"at": t.isoformat(), "stage": s, "on": et, "decision": d, "actor": a, "reason": r}
            for t, s, et, d, a, r in rows]