"""Read-only views for the console: payment lifecycle, approval history, reconcile status.
Everything is scoped to the logged-in person's company."""
from datetime import datetime, timezone
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
    # how long each stage took after the one before it, and which stage the invoice is waiting on now
    prev = None
    for st in S:
        t = datetime.fromisoformat(st["at"]) if st["at"] else None
        st["seconds"] = max(0, (t - prev).total_seconds()) if t and prev else None
        if t:
            prev = t
    active = next((i for i, st in enumerate(S) if st["state"] == "pending"), None)
    first, last = S[0]["at"], max((st["at"] for st in S if st["at"]), default=None)
    complete = all(st["state"] in ("done", "skipped") for st in S)
    total = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() if first and last else None
    return {"invoice_number": inv[0], "status": inv[2], "stages": S, "active": active, "complete": complete,
            "has_problem": any(st["state"] == "problem" for st in S), "total_seconds": total,
            "started_at": first, "now": datetime.now(timezone.utc).isoformat()}


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
    return {"items": items, "analytics": _approval_analytics(cid), "stats": {"decisions": total[0], "approved": total[1], "rejected": total[2],
                                      "overrides": total[3], "confirmed_with_vendor": total[4], "approved_then_problem": bad_after}}


def _approval_analytics(cid, days=14):
    with get_conn() as conn:
        daily = conn.execute(
            """SELECT d::date, count(r.id) FILTER (WHERE r.human_decision = 'approve' AND NOT (r.original_ai_decision IN ('hold','reject'))),
                      count(r.id) FILTER (WHERE r.human_decision = 'reject'),
                      count(r.id) FILTER (WHERE r.human_decision = 'approve' AND r.original_ai_decision IN ('hold','reject'))
               FROM generate_series(current_date - (%s - 1), current_date, interval '1 day') d
               LEFT JOIN (SELECT r.* FROM reviews r JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s) r
                      ON r.created_at::date = d::date
               GROUP BY d ORDER BY d""", (days, cid)).fetchall()
        people = conn.execute(
            """SELECT r.reviewer, count(*), count(*) FILTER (WHERE r.human_decision = 'approve'),
                      count(*) FILTER (WHERE r.original_ai_decision IN ('hold','reject') AND r.human_decision = 'approve'),
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (r.created_at - i.received_at)))
               FROM reviews r JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s
               GROUP BY r.reviewer ORDER BY 2 DESC LIMIT 8""", (cid,)).fetchall()
        mx = conn.execute(
            """SELECT COALESCE(r.original_ai_decision, 'none'), r.human_decision, count(*)
               FROM reviews r JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s GROUP BY 1, 2""", (cid,)).fetchall()
        lat = conn.execute(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x), percentile_cont(0.9) WITHIN GROUP (ORDER BY x),
                      count(*) FILTER (WHERE x < 300), count(*) FILTER (WHERE x >= 300 AND x < 1800),
                      count(*) FILTER (WHERE x >= 1800 AND x < 7200), count(*) FILTER (WHERE x >= 7200 AND x < 86400),
                      count(*) FILTER (WHERE x >= 86400)
               FROM (SELECT extract(epoch FROM (r.created_at - i.received_at)) x FROM reviews r
                     JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s) t""", (cid,)).fetchone()
        money = conn.execute(
            """SELECT r.human_decision, COALESCE(sum(i.amount), 0) FROM reviews r JOIN invoices i ON i.id = r.invoice_id
               WHERE i.company_id = %s GROUP BY 1""", (cid,)).fetchall()
        later = conn.execute(
            """SELECT count(DISTINCT r.id) FILTER (WHERE r.original_ai_decision IN ('hold','reject') AND r.human_decision = 'approve'),
                      count(DISTINCT r.id) FILTER (WHERE r.original_ai_decision IN ('hold','reject') AND r.human_decision = 'approve'
                                                   AND EXISTS (SELECT 1 FROM payments p JOIN exceptions e ON e.payment_id = p.id WHERE p.invoice_id = i.id))
               FROM reviews r JOIN invoices i ON i.id = r.invoice_id WHERE i.company_id = %s""", (cid,)).fetchone()
    ai_total = sum(n for a, h, n in mx if a != "none")
    agree = sum(n for a, h, n in mx if (a == "approve" and h == "approve") or (a in ("hold", "reject") and h == "reject"))
    return {
        "daily": [{"day": d.isoformat(), "approved": a, "rejected": r, "overrode": o} for d, a, r, o in daily],
        "people": [{"name": n, "decisions": c, "approve_rate": round(a / c, 3) if c else 0, "overrides": o,
                    "median_seconds": float(m) if m is not None else None} for n, c, a, o, m in people],
        "matrix": [{"ai": a, "human": h, "count": n} for a, h, n in mx],
        "agreement": round(agree / ai_total, 3) if ai_total else None,
        "latency": {"median": float(lat[0]) if lat[0] is not None else None, "p90": float(lat[1]) if lat[1] is not None else None,
                    "buckets": [{"label": l, "count": c} for l, c in zip(("< 5 min", "5-30 min", "30 min-2 h", "2-24 h", "> 1 day"), lat[2:])]},
        "money": {d: str(a) for d, a in money},
        "overrides": {"total": later[0] or 0, "later_problem": later[1] or 0},
    }


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
    score = 100 - (35 if not balanced else 0) - min(30, 10 * (no_ledger + hard_open)) - min(20, 5 * other_open) - min(10, 5 * no_hook) - min(10, 3 * stuck)
    return {"health": max(0, score), "analytics": _reconcile_analytics(cid), "state": state, "problems": problems, "payments": {s: {"count": n, "amount": str(a)} for s, (n, a) in pays.items()},
            "exceptions": ex, "stuck": stuck, "ledger": [{"currency": c, "debits": str(d), "credits": str(cr)} for c, d, cr in led],
            "ledger_balanced": balanced, "last_run": {"at": _iso(last[0]), "result": last[1]} if last else None, "runs": runs}


def _reconcile_analytics(cid, days=14):
    with get_conn() as conn:
        daily = conn.execute(
            """SELECT d::date,
                      (SELECT count(*) FROM settlements s JOIN payments p ON p.id = s.payment_id WHERE p.company_id = %(c)s AND s.settled_at::date = d::date),
                      (SELECT COALESCE(sum(s.amount_received), 0) FROM settlements s JOIN payments p ON p.id = s.payment_id WHERE p.company_id = %(c)s AND s.settled_at::date = d::date),
                      (SELECT count(*) FROM payments p WHERE p.company_id = %(c)s AND p.status = 'failed' AND p.updated_at::date = d::date),
                      (SELECT count(*) FROM exceptions e WHERE e.company_id = %(c)s AND e.created_at::date = d::date),
                      (SELECT count(*) FROM exceptions e WHERE e.company_id = %(c)s AND e.resolved_at::date = d::date)
               FROM generate_series(current_date - (%(n)s - 1), current_date, interval '1 day') d ORDER BY d""", {"c": cid, "n": days}).fetchall()
        lat = conn.execute(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x), percentile_cont(0.9) WITHIN GROUP (ORDER BY x), count(*)
               FROM (SELECT extract(epoch FROM (s.settled_at - p.created_at)) x FROM settlements s
                     JOIN payments p ON p.id = s.payment_id WHERE p.company_id = %s) t""", (cid,)).fetchone()
        flow = conn.execute(
            """SELECT COALESCE(sum(s.amount_sent), 0), COALESCE(sum(s.amount_received), 0), COALESCE(sum(s.fee_amount), 0), count(*)
               FROM settlements s JOIN payments p ON p.id = s.payment_id WHERE p.company_id = %s""", (cid,)).fetchone()
        fix = conn.execute(
            """SELECT count(*) FILTER (WHERE status = 'resolved'), count(*) FILTER (WHERE status = 'open'),
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (resolved_at - created_at))) FILTER (WHERE status = 'resolved'),
                      extract(epoch FROM (now() - min(created_at) FILTER (WHERE status = 'open')))
               FROM exceptions WHERE company_id = %s""", (cid,)).fetchone()
    return {
        "daily": [{"day": d.isoformat(), "settled": n, "amount": str(a), "failed": f, "opened": o, "resolved": r} for d, n, a, f, o, r in daily],
        "latency": {"median": float(lat[0]) if lat[0] is not None else None, "p90": float(lat[1]) if lat[1] is not None else None, "count": lat[2]},
        "flow": {"sent": str(flow[0]), "received": str(flow[1]), "fees": str(flow[2]), "payments": flow[3]},
        "exceptions": {"resolved": fix[0], "open": fix[1], "median_fix_seconds": float(fix[2]) if fix[2] is not None else None,
                       "oldest_open_seconds": float(fix[3]) if fix[3] is not None else None},
    }
