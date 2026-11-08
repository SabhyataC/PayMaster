"""Read-only views for the console: payment lifecycle, approval history, reconcile status.
Everything is scoped to the logged-in person's company."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import current_member
from backend.db import get_conn
from backend.timeline import find_invoice_id

router = APIRouter(tags=["insights"])


def _iso(t):
    return t.isoformat() if t else None


# ---------------------------------------------------------------- lifecycle
@router.get("/lifecycle/{ref}")
def lifecycle(ref: str, user: dict = Depends(current_member)):
    cid = user["company_id"]
    with get_conn() as conn:
        iid = find_invoice_id(conn, ref, cid)
        if not iid:
            raise HTTPException(404, "Invoice not found")
        inv = conn.execute("SELECT invoice_number, received_at, status FROM invoices WHERE id = %s", (iid,)).fetchone()
        ver = conn.execute("SELECT created_at, decision, reason FROM audit_log WHERE entity_id = %s AND stage = 'verify' "
                           "ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
        fr = conn.execute("SELECT created_at, decision FROM audit_log WHERE entity_id = %s AND stage = 'fraud' "
                          "AND reason LIKE 'Fraud risk %%' ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
        rv = conn.execute("SELECT created_at, human_decision, reviewer FROM reviews WHERE invoice_id = %s ORDER BY created_at DESC LIMIT 1", (iid,)).fetchone()
        pay = conn.execute("SELECT id, created_at, status, paypal_batch_id, updated_at FROM payments WHERE invoice_id = %s "
                           "ORDER BY attempt DESC LIMIT 1", (iid,)).fetchone()
        hook = led = rec = None
        if pay:
            if pay[3]:
                hook = conn.execute("SELECT min(processed_at), count(*) FROM webhook_events WHERE batch_id = %s AND processed_at IS NOT NULL", (pay[3],)).fetchone()
            led = conn.execute("SELECT min(created_at), count(*) FROM ledger_entries WHERE payment_id = %s", (pay[0],)).fetchone()
            rec = conn.execute("SELECT settled_at, 'settled' FROM settlements WHERE payment_id = %s", (pay[0],)).fetchone()
            openx = conn.execute("SELECT count(*) FROM exceptions WHERE payment_id = %s AND status = 'open'", (pay[0],)).fetchone()[0]
        else:
            openx = 0
    S = []

    def add(key, label, state, at, detail):
        S.append({"key": key, "label": label, "state": state, "at": _iso(at), "detail": detail})

    add("received", "Invoice received", "done", inv[1], f"Invoice {inv[0]} arrived")
    if ver:
        bad = ver[1] in ("hold", "reject")
        add("verified", "Checked by Verify", "problem" if bad else "done", ver[0],
            {"approve": "All checks passed", "hold": "Held: a check failed", "reject": "Rejected"}.get(ver[1], ver[1]))
    else:
        add("verified", "Checked by Verify", "pending", None, "Verify has not run yet")
    add("fraud", "Fraud scan", "done" if fr else "pending", fr[0] if fr else None, f"Risk: {fr[1]}" if fr else "Not run")
    if rv:
        add("human", "Person decided", "done" if rv[1] == "approve" else "problem", rv[0], f"{rv[2]}: {rv[1]}")
    elif ver and ver[1] == "approve":
        add("human", "Person decided", "skipped", None, "Not needed: every check passed and the amount is within the auto-pay limit")
    else:
        add("human", "Person decided", "pending", None, "Waiting for a person" if ver else "Not yet")
    if pay:
        pstate = {"failed": "problem", "unknown": "problem", "returned": "problem"}.get(pay[2], "done")
        add("payout", "Payout sent to PayPal", pstate, pay[1], f"Status: {pay[2]}")
        add("webhook", "PayPal confirmed", "done" if hook and hook[0] else "pending", hook[0] if hook else None,
            f"{hook[1]} webhook event(s) processed" if hook and hook[0] else "Waiting for PayPal's webhook")
        add("ledger", "Booked in the ledger", "done" if led and led[1] else "pending", led[0] if led else None,
            f"{led[1]} ledger entries" if led and led[1] else "Booked once PayPal confirms")
        add("reconciled", "Reconciled", "problem" if openx else ("done" if rec else "pending"), rec[0] if rec else None,
            f"{openx} open exception(s)" if openx else ("Settlement matches PayPal" if rec else "Not reconciled yet"))
    else:
        for k, l in (("payout", "Payout sent to PayPal"), ("webhook", "PayPal confirmed"), ("ledger", "Booked in the ledger"), ("reconciled", "Reconciled")):
            add(k, l, "pending", None, "No payout yet")
    return {"invoice_number": inv[0], "status": inv[2], "stages": S}


# ---------------------------------------------------------------- approvals
@router.get("/approvals")
def approvals(decision: str = Query("all", pattern="^(all|approve|reject|override)$"), q: str = Query("", max_length=60),
              user: dict = Depends(current_member)):
    cid = user["company_id"]
    like = "%" + q.strip().replace("%", "").replace("_", "") + "%"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.created_at, i.id, i.invoice_number, v.name, i.amount, i.currency, r.original_ai_decision,
                      r.human_decision, r.reviewer, r.note, r.confirmed_with_vendor, r.status_before, r.status_after,
                      (SELECT p.status FROM payments p WHERE p.invoice_id = i.id ORDER BY p.attempt DESC LIMIT 1)
               FROM reviews r JOIN invoices i ON i.id = r.invoice_id JOIN vendors v ON v.id = i.vendor_id
               WHERE i.company_id = %s AND (%s = '' OR i.invoice_number ILIKE %s OR v.name ILIKE %s OR r.reviewer ILIKE %s)
               ORDER BY r.created_at DESC LIMIT 300""", (cid, q.strip(), like, like, like)).fetchall()
        items = []
        for r in rows:
            override = r[7] in ("hold", "reject") and r[8] == "approve"
            if decision == "approve" and r[8] != "approve" or decision == "reject" and r[8] != "reject" or decision == "override" and not override:
                continue
            items.append({"id": str(r[0]), "at": _iso(r[1]), "invoice_id": str(r[2]), "invoice_number": r[3], "vendor": r[4],
                          "amount": str(r[5]), "currency": r[6], "ai_decision": r[7], "decision": r[8], "reviewer": r[9],
                          "note": r[10], "confirmed_with_vendor": r[11], "status_before": r[12], "status_after": r[13],
                          "payment": r[14], "override": override})
        total = conn.execute("SELECT count(*), count(*) FILTER (WHERE r.human_decision = 'approve'), "
                             "count(*) FILTER (WHERE r.human_decision = 'reject'), "
                             "count(*) FILTER (WHERE r.original_ai_decision IN ('hold','reject') AND r.human_decision = 'approve'), "
                             "count(*) FILTER (WHERE r.confirmed_with_vendor) "
                             "FROM reviews r JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s", (cid,)).fetchone()
        bad_after = conn.execute(
            "SELECT count(*) FROM reviews r JOIN invoices i ON i.id = r.invoice_id JOIN payments p ON p.invoice_id = i.id "
            "JOIN exceptions e ON e.payment_id = p.id WHERE i.company_id = %s AND r.human_decision = 'approve'", (cid,)).fetchone()[0]
    return {"items": items, "stats": {"decisions": total[0], "approved": total[1], "rejected": total[2],
                                      "overrides": total[3], "confirmed_with_vendor": total[4], "approved_then_problem": bad_after}}


# ---------------------------------------------------------------- reconcile status
HARD = ("success_mismatch", "amount_mismatch", "unknown_payout", "paypal_status_mismatch")


@router.get("/reconcile/status")
def reconcile_status(user: dict = Depends(current_member)):
    cid = user["company_id"]
    with get_conn() as conn:
        pays = {s: (n, a) for s, n, a in conn.execute(
            "SELECT status, count(*), COALESCE(sum(amount), 0) FROM payments WHERE company_id = %s GROUP BY status", (cid,)).fetchall()}
        ex = [{"type": t, "open": n, "oldest": _iso(o)} for t, n, o in conn.execute(
            "SELECT type, count(*), min(created_at) FROM exceptions WHERE company_id = %s AND status = 'open' GROUP BY type ORDER BY 2 DESC", (cid,)).fetchall()]
        last = conn.execute("SELECT created_at, reason, inputs FROM audit_log WHERE stage = 'reconcile' AND entity_type = 'reconcile_run' "
                            "AND company_id = %s ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        runs = [{"at": _iso(t), "result": r} for t, r in conn.execute(
            "SELECT created_at, reason FROM audit_log WHERE stage = 'reconcile' AND entity_type = 'reconcile_run' AND company_id = %s "
            "ORDER BY id DESC LIMIT 8", (cid,)).fetchall()]
        led = conn.execute(
            "SELECT l.currency, COALESCE(sum(l.amount) FILTER (WHERE l.direction = 'debit'), 0), "
            "COALESCE(sum(l.amount) FILTER (WHERE l.direction = 'credit'), 0) FROM ledger_entries l "
            "JOIN payments p ON p.id = l.payment_id WHERE p.company_id = %s GROUP BY l.currency", (cid,)).fetchall()
        no_ledger = conn.execute("SELECT count(*) FROM payments p WHERE p.company_id = %s AND p.status = 'success' "
                                 "AND NOT EXISTS (SELECT 1 FROM ledger_entries l WHERE l.payment_id = p.id)", (cid,)).fetchone()[0]
        no_hook = conn.execute(
            "SELECT count(*) FROM payments p WHERE p.company_id = %s AND p.status = 'success' AND p.updated_at < now() - interval '10 minutes' "
            "AND NOT EXISTS (SELECT 1 FROM webhook_events w WHERE w.batch_id = p.paypal_batch_id)", (cid,)).fetchone()[0]
    balanced = all(Decimal(d) == Decimal(c) for _, d, c in led)
    hard_open = sum(e["open"] for e in ex if e["type"] in HARD)
    other_open = sum(e["open"] for e in ex) - hard_open
    stuck = pays.get("unknown", (0, 0))[0] + pays.get("created", (0, 0))[0]
    problems = []
    if not balanced:
        problems.append("The ledger does not balance")
    if no_ledger:
        problems.append(f"{no_ledger} successful payment(s) have no ledger entries")
    if hard_open:
        problems.append(f"{hard_open} open exception(s) where PayPal and our records disagree")
    if other_open:
        problems.append(f"{other_open} other open exception(s) need a person (failed, stuck or unclaimed payouts)")
    if no_hook:
        problems.append(f"{no_hook} successful payment(s) have no webhook on record")
    state = "attention" if problems else ("in_sync" if pays else "empty")
    return {"state": state, "problems": problems, "payments": {s: {"count": n, "amount": str(a)} for s, (n, a) in pays.items()},
            "exceptions": ex, "stuck": stuck, "ledger": [{"currency": c, "debits": str(d), "credits": str(cr)} for c, d, cr in led],
            "ledger_balanced": balanced, "last_run": {"at": _iso(last[0]), "result": last[1]} if last else None, "runs": runs}
