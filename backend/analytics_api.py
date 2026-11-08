"""Numbers for the Overview charts. Read-only, one company, one time window."""
from fastapi import APIRouter, Depends, Query

from backend.auth import current_member
from backend.db import get_conn

router = APIRouter(tags=["analytics"])

STOPPED = ("held", "rejected", "cancelled")
GOOD = ("approved", "paid")

# latest Verify audit row per invoice received in the window
LATEST_VERIFY = """
    SELECT DISTINCT ON (a.entity_id) a.entity_id, a.inputs, a.created_at, i.received_at, i.vendor_id
    FROM audit_log a JOIN invoices i ON i.id = a.entity_id
    WHERE a.stage = 'verify' AND i.company_id = %s AND i.received_at >= now() - make_interval(days => %s)
    ORDER BY a.entity_id, a.id DESC"""


@router.get("/analytics")
def analytics(days: int = Query(30, ge=7, le=180), user: dict = Depends(current_member)):
    cid = user["company_id"]
    with get_conn() as conn:
        win = "i.company_id = %s AND i.received_at >= now() - make_interval(days => %s)"
        cur_row = conn.execute(f"SELECT i.currency, count(*) FROM invoices i WHERE {win} GROUP BY 1 ORDER BY 2 DESC",
                               (cid, days)).fetchall()
        currency = cur_row[0][0] if cur_row else "USD"
        total = sum(n for _, n in cur_row)

        timeline = [{"day": d.isoformat(), "good": g, "stopped": s, "other": o} for d, g, s, o in conn.execute(
            """SELECT d::date,
                      count(i.id) FILTER (WHERE i.status = ANY(%s)),
                      count(i.id) FILTER (WHERE i.status = ANY(%s)),
                      count(i.id) FILTER (WHERE i.id IS NOT NULL AND NOT (i.status = ANY(%s)) AND NOT (i.status = ANY(%s)))
               FROM generate_series(current_date - (%s - 1), current_date, interval '1 day') d
               LEFT JOIN invoices i ON i.company_id = %s AND i.received_at::date = d::date
               GROUP BY 1 ORDER BY 1""",
            (list(GOOD), list(STOPPED), list(GOOD), list(STOPPED), days, cid)).fetchall()]

        status = {s: n for s, n in conn.execute(
            f"SELECT i.status, count(*) FROM invoices i WHERE {win} GROUP BY 1 ORDER BY 2 DESC", (cid, days)).fetchall()}

        stopped_amt, stopped_n = conn.execute(
            f"SELECT COALESCE(sum(i.amount), 0), count(*) FROM invoices i WHERE {win} AND i.status = ANY(%s) AND i.currency = %s",
            (cid, days, list(STOPPED), currency)).fetchone()
        paid_amt, paid_n = conn.execute(
            "SELECT COALESCE(sum(p.amount), 0), count(*) FROM payments p JOIN invoices i ON i.id = p.invoice_id "
            "WHERE p.status = 'success' AND i.company_id = %s AND p.created_at >= now() - make_interval(days => %s) AND p.currency = %s",
            (cid, days, currency)).fetchone()

        paid_series = [{"day": d.isoformat(), "amount": float(a)} for d, a in conn.execute(
            """SELECT d::date, COALESCE(sum(p.amount) FILTER (WHERE p.status = 'success' AND p.currency = %s), 0)
               FROM generate_series(current_date - (%s - 1), current_date, interval '1 day') d
               LEFT JOIN (SELECT p.* FROM payments p JOIN invoices i ON i.id = p.invoice_id WHERE i.company_id = %s) p
                      ON p.created_at::date = d::date
               GROUP BY 1 ORDER BY 1""", (currency, days, cid)).fetchall()]

        reasons = [{"key": r, "n": n} for r, n in conn.execute(
            f"""SELECT r, count(*) FROM ({LATEST_VERIFY}) x,
                       jsonb_array_elements_text(COALESCE(x.inputs -> 'verdict' -> 'reasons', '[]'::jsonb)) r
                WHERE r <> 'all_checks_passed' GROUP BY r ORDER BY 2 DESC, 1 LIMIT 6""", (cid, days)).fetchall()]

        signals = [{"key": r, "n": n} for r, n in conn.execute(
            """SELECT s, count(*) FROM (
                   SELECT DISTINCT ON (a.entity_id) a.inputs FROM audit_log a JOIN invoices i ON i.id = a.entity_id
                   WHERE a.stage = 'fraud' AND a.reason LIKE 'Fraud risk %%' AND i.company_id = %s
                     AND i.received_at >= now() - make_interval(days => %s)
                   ORDER BY a.entity_id, a.id DESC) x,
                 jsonb_array_elements_text(COALESCE(x.inputs -> 'signals', '[]'::jsonb)) s
               GROUP BY s ORDER BY 2 DESC, 1 LIMIT 6""", (cid, days)).fetchall()]

        v = conn.execute(
            f"""SELECT count(*),
                       count(*) FILTER (WHERE x.inputs -> 'verdict' ->> 'decision' = 'approve'),
                       count(*) FILTER (WHERE x.inputs ->> 'agent_decision' = 'approve'
                                          AND x.inputs -> 'verdict' ->> 'decision' <> 'approve'),
                       count(*) FILTER (WHERE x.inputs ->> 'agent_decision' IS NOT NULL),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (x.created_at - x.received_at)))
                FROM ({LATEST_VERIFY}) x""", (cid, days)).fetchone()
        verified, auto_ok, overruled, ai_ran, median_s = v

        reviews = {d: n for d, n in conn.execute(
            "SELECT r.human_decision, count(*) FROM reviews r JOIN invoices i ON i.id = r.invoice_id "
            "WHERE i.company_id = %s AND r.created_at >= now() - make_interval(days => %s) GROUP BY 1", (cid, days)).fetchall()}

        vendors = [{"name": n, "paid": float(a), "invoices": c, "stopped": s} for n, a, c, s in conn.execute(
            """SELECT v.name,
                      COALESCE(sum(p.amount) FILTER (WHERE p.status = 'success' AND p.currency = %s), 0) AS paid,
                      count(DISTINCT i.id), count(DISTINCT i.id) FILTER (WHERE i.status = ANY(%s))
               FROM vendors v JOIN invoices i ON i.vendor_id = v.id AND i.company_id = v.company_id
               LEFT JOIN payments p ON p.invoice_id = i.id
               WHERE v.company_id = %s AND i.received_at >= now() - make_interval(days => %s)
               GROUP BY v.id, v.name ORDER BY 2 DESC, 3 DESC, 1 LIMIT 5""", (currency, list(STOPPED), cid, days)).fetchall()]

    return {"days": days, "currency": currency, "mixed_currency": len(cur_row) > 1, "total": total,
            "timeline": timeline, "status": status, "paid_series": paid_series,
            "kpi": {"stopped_amount": float(stopped_amt), "stopped_count": stopped_n, "paid_amount": float(paid_amt),
                    "paid_count": paid_n, "verified": verified, "auto_approved": auto_ok,
                    "auto_rate": (auto_ok / verified) if verified else None,
                    "median_seconds": float(median_s) if median_s is not None else None},
            "reasons": reasons, "signals": signals,
            "ai": {"ran": ai_ran, "overruled_by_code": overruled},
            "human": {"approved": reviews.get("approve", 0), "rejected": reviews.get("reject", 0)},
            "vendors": vendors}
