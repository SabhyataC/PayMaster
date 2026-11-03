"""B8: invoices that arrived before their PO or delivery are re-checked, never forgotten.

Two triggers: (1) automatically when a PO or a delivery is recorded, (2) one click ("Re-verify")
on a held invoice or ("Retry") on a failed upload. Both only run the normal pipeline again:
hard checks, fraud gate, AI, human review for holds. Nothing here pays anything.
"""
from backend.db import get_conn


def reverify_invoice(invoice_id, company_id):
    """Re-run Verify on one HELD invoice of this company. Returns {status, reasons}."""
    from backend.investigator import investigate
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM invoices WHERE id::text = %s AND company_id = %s",
                           (str(invoice_id), company_id)).fetchone()
    if row is None:
        raise LookupError("invoice not found")
    if row[0] != "held":
        raise ValueError(f"only held invoices can be re-verified (this one is '{row[0]}')")
    v = investigate(invoice_id, allow_release=True)
    return {"invoice_id": str(invoice_id), "status": v["status"], "decision": v["verdict"]["decision"],
            "reasons": v["verdict"]["reasons"]}


def retry_extraction(extraction_id, company_id):
    """Run Intake again on a failed / needs_review upload (e.g. the AI was overloaded)."""
    from backend.intake import extract_row
    from backend.investigator import investigate
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM extractions WHERE id::text = %s AND company_id = %s",
                           (str(extraction_id), company_id)).fetchone()
    if row is None:
        raise LookupError("upload not found")
    if row[0] not in ("failed", "needs_review"):
        raise ValueError(f"only failed or needs_review uploads can be retried (this one is '{row[0]}')")
    res = extract_row(extraction_id)
    out = {"extraction_id": str(extraction_id), "intake": res["routing"]["status"],
           "reason": res["routing"].get("reason"), "invoice_id": None, "invoice_status": None}
    if res["routing"]["status"] == "accepted" and res["routing"].get("invoice_id"):
        v = investigate(res["routing"]["invoice_id"])
        out.update(invoice_id=str(res["routing"]["invoice_id"]), invoice_status=v["status"])
    return out


def reprocess_po(company_id, po_number):
    """After a PO or delivery is recorded: re-route uploads that were waiting for this PO and
    re-verify held invoices on it. Errors are collected, never raised: the PO/delivery the person
    just saved must not fail because a re-check did."""
    from backend.investigator import investigate
    from backend.intake import route_row
    done, errors = [], []
    with get_conn() as conn:
        waiting = [r[0] for r in conn.execute(
            "SELECT id FROM extractions WHERE company_id = %s AND status = 'needs_review' "
            "AND reason ILIKE %s", (company_id, f"%{po_number} is not in the system%")).fetchall()]
        held = [r[0] for r in conn.execute(
            "SELECT i.id FROM invoices i JOIN purchase_orders po ON po.id = i.po_id "
            "WHERE i.company_id = %s AND i.status = 'held' AND po.po_number = %s",
            (company_id, po_number)).fetchall()]
    for ext_id in waiting:
        try:
            r = route_row(ext_id)
            if r["status"] == "accepted" and r.get("invoice_id"):
                v = investigate(r["invoice_id"])
                done.append({"invoice_id": str(r["invoice_id"]), "status": v["status"], "from": "waiting_upload"})
        except Exception as e:
            errors.append(f"upload {ext_id}: {str(e)[:120]}")
    for inv_id in held:
        try:
            v = investigate(inv_id, allow_release=True)
            done.append({"invoice_id": str(inv_id), "status": v["status"], "from": "held"})
        except Exception as e:
            errors.append(f"invoice {inv_id}: {str(e)[:120]}")
    return {"rechecked": done, "errors": errors}


def reprocess_vendor(company_id, vendor_id):
    """After an admin changes a vendor's payout email: re-check that vendor's held invoices
    (one may have been held only because the invoice email differed from the one on file)."""
    from backend.investigator import investigate
    done, errors = [], []
    with get_conn() as conn:
        held = [r[0] for r in conn.execute(
            "SELECT id FROM invoices WHERE company_id = %s AND vendor_id::text = %s AND status = 'held'",
            (company_id, str(vendor_id))).fetchall()]
    for inv_id in held:
        try:
            v = investigate(inv_id, allow_release=True)
            done.append({"invoice_id": str(inv_id), "status": v["status"], "from": "held"})
        except Exception as e:
            errors.append(f"invoice {inv_id}: {str(e)[:120]}")
    return {"rechecked": done, "errors": errors}
