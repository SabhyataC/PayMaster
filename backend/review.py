"""Human review: the only way a held invoice can ever become approved.

Verify (the AI + code) may tighten a status but never release a 'held' invoice. A person does that
here, with a name and a written reason, and every decision lands in the audit log.
"""
import uuid

from backend.audit import log_decision
from backend.checks import REJECT_REASONS, run_hard_checks
from backend.policy import check_policy

# What a person may do to an invoice, by its current status.
HUMAN_MOVES = {
    "held":     {"approve": "approved", "reject": "rejected"},
    "approved": {"reject": "rejected"},   # an approved invoice waiting on a policy hold can still be stopped
}
MIN_NOTE = 10   # a one-word reason is not a reason


class ReviewError(Exception):
    """A decision that is refused. `code` is stable so the API and tests can match on it."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ReviewError("not_found", "No such invoice")


def _latest_audit(conn, invoice_id, stage):
    row = conn.execute(
        "SELECT decision, reason, inputs, created_at FROM audit_log "
        "WHERE entity_id = %s AND stage = %s ORDER BY id DESC LIMIT 1", (invoice_id, stage)).fetchone()
    return None if row is None else {"decision": row[0], "reason": row[1], "inputs": row[2], "at": row[3]}


def list_queue(conn):
    """Everything a person has to look at:
       verify_hold  = status 'held' (Verify wants a person)
       policy_hold  = status 'approved' but pay policy wants a person (new payee, over the limit)
    Oldest first, so nothing waits forever."""
    rows = conn.execute(
        """SELECT i.id, i.invoice_number, v.name, i.amount, i.currency, i.status, i.received_at
           FROM invoices i JOIN vendors v ON v.id = i.vendor_id
           WHERE i.status IN ('held', 'approved')
             AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.invoice_id = i.id)
           ORDER BY i.received_at""").fetchall()
    queue = []
    for inv_id, number, vendor, amount, currency, status, received in rows:
        if status == "held":
            v = _latest_audit(conn, inv_id, "verify")
            reasons = ((v or {}).get("inputs") or {}).get("verdict", {}).get("reasons", [])
            stage = "verify_hold"
        else:
            policy = check_policy(conn, inv_id)
            if policy["outcome"] != "hold":
                continue          # approved and free to pay: nobody needs to look
            reasons, stage = policy["reasons"], "policy_hold"
        queue.append({"invoice_id": str(inv_id), "invoice_number": number, "vendor": vendor,
                      "amount": str(amount), "currency": currency, "status": status,
                      "stage": stage, "reasons": reasons, "received_at": received.isoformat()})
    return queue


def get_case(conn, invoice_id):
    """Everything a reviewer needs on one screen: the invoice, why it was held, the evidence."""
    invoice_id = _uuid(invoice_id)
    row = conn.execute(
        """SELECT i.id, i.invoice_number, v.name, i.amount, i.currency, i.destination_email, i.status,
                  i.received_at, i.po_id FROM invoices i JOIN vendors v ON v.id = i.vendor_id
           WHERE i.id = %s""", (invoice_id,)).fetchone()
    if row is None:
        raise ReviewError("not_found", "No such invoice")
    verify = _latest_audit(conn, invoice_id, "verify")
    verdict = ((verify or {}).get("inputs") or {}).get("verdict") or {}
    fraud = [{"decision": d, "reason": r, "at": t.isoformat()} for d, r, t in conn.execute(
        "SELECT decision, reason, created_at FROM audit_log WHERE entity_id = %s AND stage = 'fraud' "
        "ORDER BY id DESC LIMIT 12", (invoice_id,)).fetchall()]
    history = [{"stage": s, "decision": d, "actor": a, "reason": r, "at": t.isoformat()} for s, d, a, r, t in conn.execute(
        "SELECT stage, decision, actor, reason, created_at FROM audit_log WHERE entity_id = %s "
        "AND stage IN ('review','pay') ORDER BY id", (invoice_id,)).fetchall()]
    return {
        "invoice": {"id": str(row[0]), "invoice_number": row[1], "vendor": row[2], "amount": str(row[3]),
                    "currency": row[4], "destination_email": row[5], "status": row[6],
                    "received_at": row[7].isoformat(), "has_po": row[8] is not None},
        "verify": {"decision": verdict.get("decision"), "reasons": verdict.get("reasons", []),
                   "evidence": verdict.get("evidence", []), "confidence": verdict.get("confidence")},
        "fraud_rows": fraud,
        "policy": check_policy(conn, invoice_id),
        "allowed_actions": sorted(HUMAN_MOVES.get(row[6], {})),
        "history": history,
    }


def decide(conn, invoice_id, decision, reviewer, note):
    """Apply a human decision. Raises ReviewError (and changes nothing) when it is not allowed."""
    invoice_id = _uuid(invoice_id)
    reviewer, note = (reviewer or "").strip(), (note or "").strip()
    if not reviewer:
        raise ReviewError("reviewer_required", "Say who is deciding")
    if len(note) < MIN_NOTE:
        raise ReviewError("note_too_short", f"Write at least {MIN_NOTE} characters explaining why")

    row = conn.execute("SELECT status FROM invoices WHERE id = %s", (invoice_id,)).fetchone()
    if row is None:
        raise ReviewError("not_found", "No such invoice")
    before = row[0]
    new_status = HUMAN_MOVES.get(before, {}).get(decision)
    if new_status is None:
        raise ReviewError("not_allowed", f"A person cannot '{decision}' an invoice that is '{before}'")

    if decision == "approve":
        # A person may clear a hold (changed bank, amount off, a flagged email) but never override
        # a hard failure that says the invoice must not be paid: duplicate, no PO, other vendor's PO.
        hard = run_hard_checks(invoice_id)
        stuck = [r for r in hard["reasons"] if r in REJECT_REASONS]
        if stuck:
            raise ReviewError("hard_reject", "Cannot approve: " + ", ".join(stuck))

    # Compare-and-swap: if someone else decided first (or Verify moved it), this updates 0 rows.
    cur = conn.execute("UPDATE invoices SET status = %s WHERE id = %s AND status = %s",
                       (new_status, invoice_id, before))
    if cur.rowcount != 1:
        conn.rollback()
        raise ReviewError("already_decided", "Someone else changed this invoice first; reload it")

    verify = _latest_audit(conn, invoice_id, "verify")
    log_decision(
        conn, stage="review", entity_type="invoice", entity_id=invoice_id,
        decision=new_status, actor="human",
        reason=f"{reviewer} chose '{decision}': {note}",
        inputs={"reviewer": reviewer, "note": note, "action": decision, "status_before": before,
                "status_after": new_status,
                "verify_reasons": ((verify or {}).get("inputs") or {}).get("verdict", {}).get("reasons", [])},
    )
    conn.commit()
    return {"invoice_id": str(invoice_id), "status_before": before, "status": new_status,
            "next": "POST /review/{id}/pay to send the payment" if new_status == "approved" else None}