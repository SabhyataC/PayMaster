"""Lifecycle, approvals and reconcile-status views. Run on a TEST database:  python -m tests.test_insights"""
import sys

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, mk_po, mk_vendor, one, run, section, world,
)
from scripts.eval import install_hostile_stub

install_hostile_stub()
from backend.investigator import investigate  # noqa: E402
from backend import review  # noqa: E402
from backend.db import get_conn  # noqa: E402


def test_lifecycle():
    section("Lifecycle timeline")
    w, o = world(), world()
    h = bearer(w["users"]["viewer"][0])
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
    r = client.get(f"/lifecycle/{inv}", headers=h)
    st = {s["key"]: s for s in r.json()["stages"]}
    check("new invoice: received done, rest pending", r.status_code == 200 and st["received"]["state"] == "done" and st["verified"]["state"] == "pending" and st["payout"]["state"] == "pending")
    investigate(inv)
    st = {s["key"]: s for s in client.get(f"/lifecycle/{inv}", headers=h).json()["stages"]}
    check("after Verify: verified has a time and a result", st["verified"]["at"] and st["verified"]["state"] in ("done", "problem"))
    check("eight stages in order", [s["key"] for s in client.get(f"/lifecycle/{inv}", headers=h).json()["stages"]] ==
          ["received", "verified", "fraud", "human", "payout", "webhook", "ledger", "reconciled"])
    check("other company: 404", client.get(f"/lifecycle/{inv}", headers=bearer(o["users"]["viewer"][0])).status_code == 404)
    check("no login: refused", client.get(f"/lifecycle/{inv}").status_code in (401, 403))


def test_approvals():
    section("Approval history")
    w, o = world(), world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="held", email="x@other-domain.example.net")
    with get_conn() as conn:
        pass
    run("INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note, confirmed_with_vendor, status_before, status_after) "
        "VALUES (%s, 'hold', 'approve', 'Priya Rao', 'Called the vendor', true, 'held', 'approved')", (inv,))
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="rejected")
    run("INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note) VALUES (%s, 'hold', 'reject', 'Sam Lee', 'Not ours')", (inv2,))
    h = bearer(w["users"]["viewer"][0])
    d = client.get("/approvals", headers=h).json()
    check("lists both decisions", len(d["items"]) == 2 and d["stats"]["decisions"] == 2, str(d["stats"]))
    check("override counted (AI said hold, person approved)", d["stats"]["overrides"] == 1 and any(i["override"] for i in d["items"]))
    check("filter: reject", [i["reviewer"] for i in client.get("/approvals?decision=reject", headers=h).json()["items"]] == ["Sam Lee"])
    check("filter: override", len(client.get("/approvals?decision=override", headers=h).json()["items"]) == 1)
    check("search by reviewer", len(client.get("/approvals?q=priya", headers=h).json()["items"]) == 1)
    check("search with wildcard characters is harmless", client.get("/approvals?q=%25%25_", headers=h).status_code == 200)
    check("bad filter refused", client.get("/approvals?decision=drop", headers=h).status_code == 422)
    check("other company sees nothing", client.get("/approvals", headers=bearer(o["users"]["viewer"][0])).json()["items"] == [])


def test_reconcile_status():
    section("Reconcile status")
    w, o = world(), world()
    h = bearer(w["users"]["viewer"][0])
    check("no payments: state empty", client.get("/reconcile/status", headers=h).json()["state"] == "empty")
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="paid")
    batch = "B-" + str(inv)[:12]
    pid = one("INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, status, paypal_batch_id, company_id) "
              "VALUES (%s, %s, 'a@b.c', 100, 'USD', 'success', %s, %s) RETURNING id", (inv, "k" + str(inv), batch, w["cid"]))[0]
    run("UPDATE payments SET updated_at = now() - interval '1 hour' WHERE id = %s", (pid,))
    d = client.get("/reconcile/status", headers=h).json()
    check("success payment with no ledger and no webhook needs attention", d["state"] == "attention" and len(d["problems"]) == 2, str(d["problems"]))
    run("INSERT INTO webhook_events (event_id, event_type, batch_id, payload, processed_at) VALUES (%s, 'PAYMENT.PAYOUTSBATCH.SUCCESS', %s, '{}', now())", ("e" + str(pid), batch))
    run("INSERT INTO ledger_entries (payment_id, entry_type, account, direction, amount, currency, company_id) VALUES (%s, 'payout', 'cash', 'credit', 100, 'USD', %s), (%s, 'payout', 'payables', 'debit', 100, 'USD', %s)", (pid, w["cid"], pid, w["cid"]))
    d = client.get("/reconcile/status", headers=h).json()
    check("with ledger and webhook: in sync, balanced", d["state"] == "in_sync" and d["ledger_balanced"], str(d))
    run("INSERT INTO exceptions (payment_id, type, detail, company_id) VALUES (%s, 'amount_mismatch', '{}', %s)", (pid, w["cid"]))
    d = client.get("/reconcile/status", headers=h).json()
    check("an open mismatch exception flips it to attention", d["state"] == "attention" and d["exceptions"][0]["type"] == "amount_mismatch")
    check("other company is empty", client.get("/reconcile/status", headers=bearer(o["users"]["viewer"][0])).json()["state"] == "empty")
    check("run history appears after a run", client.post("/reconcile/run", headers=bearer(w["users"]["reviewer"][0])).status_code == 200 and
          client.get("/reconcile/status", headers=h).json()["last_run"] is not None)


def test_analytics():
    section("Analytics series behind the charts (counted exactly, scoped to the company)")
    w, o = world(), world()
    h = bearer(w["users"]["reviewer"][0])
    rows = [("approve", "approve", 0), ("hold", "approve", 0), ("hold", "reject", 1), ("approve", "reject", 2), ("reject", "reject", 20)]
    for ai, hd, ago in rows:
        inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="held")
        run("UPDATE invoices SET received_at = now() - interval '2 hours' - (%s || ' days')::interval WHERE id = %s", (ago, inv))
        run("INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note, created_at) "
            "VALUES (%s, %s, %s, 'Asha', 'n', now() - (%s || ' days')::interval)", (inv, ai, hd, ago))
    oi = mk_invoice(o["cid"], o["vid"], o["po_id"], status="held")
    run("INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note) VALUES (%s, 'hold', 'approve', 'Zed', 'n')", (oi,))
    a = client.get("/approvals", headers=h).json()["analytics"]
    check("14 daily rows, oldest first", len(a["daily"]) == 14 and a["daily"][0]["day"] < a["daily"][-1]["day"])
    check("the 20-day-old decision is outside the 14-day chart", sum(r["approved"] + r["rejected"] + r["overrode"] for r in a["daily"]) == 4)
    today = a["daily"][-1]
    check("today: one plain approval and one approval against the AI", today["approved"] == 1 and today["overrode"] == 1 and today["rejected"] == 0, today)
    check("agreement = 3 of 5 (approve/approve, hold/reject, reject/reject)", a["agreement"] == 0.6, a["agreement"])
    cell = {(c["ai"], c["human"]): c["count"] for c in a["matrix"]}
    check("matrix counts are exact and exclude other companies", cell == {("approve", "approve"): 1, ("hold", "approve"): 1, ("hold", "reject"): 1, ("approve", "reject"): 1, ("reject", "reject"): 1}, cell)
    check("people table has only this company's reviewer", [p["name"] for p in a["people"]] == ["Asha"] and a["people"][0]["decisions"] == 5)
    check("latency buckets add up to the decisions", sum(b["count"] for b in a["latency"]["buckets"]) == 5, a["latency"])
    check("overrides counted", a["overrides"]["total"] == 1)
    r = client.get("/reconcile/status", headers=h).json()
    check("health score is 0-100", 0 <= r["health"] <= 100)
    ra = r["analytics"]
    check("reconcile series has 14 days and the exceptions block", len(ra["daily"]) == 14 and "median_fix_seconds" in ra["exceptions"])
    l = client.get(f"/lifecycle/{inv}", headers=h).json()
    check("lifecycle reports active stage, completeness and timings", l["complete"] is False and l["active"] is not None and "seconds" in l["stages"][0] and l["started_at"], l.get("active"))


def main():
    for t in (test_lifecycle, test_approvals, test_reconcile_status, test_analytics):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
