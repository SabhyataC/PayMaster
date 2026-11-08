"""PayPal timeout, retry and unknown-outcome recovery, and amount tampering. Run on a TEST database:
   python -m tests.test_payment_reliability"""
import sys

import requests

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, mk_po, mk_vendor, one, run, section, uniq, world,
)
from backend import paypal_client, pay, reconcile  # noqa: E402
from backend.db import get_conn  # noqa: E402


class FakePayPal:
    """Behaves like PayPal Payouts: the same PayPal-Request-Id never pays twice. `script` lists what the next calls do."""

    def __init__(self, script=()):
        self.script, self.batches, self.calls = list(script), {}, 0

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls += 1
        rid = headers["PayPal-Request-Id"]
        step = self.script.pop(0) if self.script else "normal"
        if step == "timeout_after_process":      # PayPal paid, our connection died
            self.batches.setdefault(rid, uniq("BATCH"))
            raise requests.ReadTimeout("read timed out")
        if step == "timeout_lost":               # the request never arrived
            raise requests.ConnectTimeout("connect timed out")
        if step == "500":
            return _R(500, {"name": "INTERNAL_SERVICE_ERROR"})
        if step == "400":
            return _R(400, {"name": "VALIDATION_ERROR", "message": "bad receiver"})
        if rid in self.batches:                  # same key again: PayPal says it already has it
            return _R(400, {"name": "USER_BUSINESS_ERROR", "details": [
                {"field": "SENDER_BATCH_ID", "issue": "duplicate",
                 "link": [{"rel": "self", "href": f"https://api.sandbox.paypal.com/v1/payments/payouts/{self.batches[rid]}"}]}]})
        self.batches[rid] = uniq("BATCH")
        return _R(201, {"batch_header": {"payout_batch_id": self.batches[rid], "batch_status": "PENDING"}})


class _R:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, str(data)

    def json(self):
        return self._d


SLEEPS = []


def install(fake):
    paypal_client.requests.post = fake.post
    paypal_client.get_token = lambda: "TOKEN"
    paypal_client._sleep = SLEEPS.append
    SLEEPS.clear()
    import os
    os.environ["PAYPAL_BASE_URL"] = "https://api.sandbox.paypal.com"


def send(inv, **kw):
    with get_conn() as conn:
        return pay.pay_invoice(conn, inv, approved_by="Person A", **kw)


def prow(inv):
    return one("SELECT status, paypal_batch_id, count(*) OVER () FROM payments WHERE invoice_id = %s ORDER BY attempt DESC LIMIT 1", (inv,))


def test_retry_and_permanent_failure():
    section("PayPal retries: temporary trouble is retried with backoff, permanent refusal is not")
    w = world()
    f = FakePayPal(["500", "500"]); install(f)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    r = send(inv)
    check("two 500s then success: payment submitted", r["status"] == "submitted", str(r))
    check("PayPal was called 3 times", f.calls == 3, f.calls)
    check("waited 1-1.5s then 2-2.5s between tries", len(SLEEPS) == 2 and 1 <= SLEEPS[0] < 1.6 and 2 <= SLEEPS[1] < 2.6, str(SLEEPS))
    check("one batch at PayPal", len(f.batches) == 1)
    f2 = FakePayPal(["400"]); install(f2)
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    r2 = send(inv2)
    check("a 4xx is a permanent failure", r2["status"] == "failed", str(r2))
    check("and is NOT retried", f2.calls == 1 and not SLEEPS, (f2.calls, SLEEPS))


def test_timeout_after_paypal_paid():
    section("THE CRITICAL SCENARIO: PayPal pays, we time out, we recover with no second payment")
    w = world()
    f = FakePayPal(["timeout_after_process"] * 3); install(f)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    r = send(inv)
    check("every try timed out: payment is 'unknown', not failed", r["status"] == "unknown", str(r))
    check("we tried 3 times with the same key", f.calls == 3 and len(f.batches) == 1)
    st = prow(inv)
    check("exactly one payment row, status unknown, no batch id known", st[0] == "unknown" and st[1] is None and st[2] == 1, str(st))
    again = send(inv)
    check("pressing Pay again while unknown re-uses the same key (no new payment row)", one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 1, str(again))
    # (that call may itself have recovered it; make a clean unknown for the job test)
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    f = FakePayPal(["timeout_after_process"] * 3); install(f)
    send(inv2)
    f.calls = 0
    with get_conn() as conn:
        out = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("recovery job reports one recovered payment", out["recovered"] == 1, str(out))
    st = prow(inv2)
    check("payment is now 'submitted' with PayPal's batch id", st[0] == "submitted" and st[1] == list(f.batches.values())[-1], str(st))
    check("PayPal still holds ONE batch for it (no duplicate payout)", len([k for k in f.batches if inv2.hex in k.replace("-", "")]) == 1, str(f.batches))
    n = one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND stage = 'pay' AND inputs ->> 'recovery' = 'true'", (one("SELECT id FROM payments WHERE invoice_id = %s", (inv2,))[0],))[0]
    check("recovery is written to the audit log", n == 1)
    with get_conn() as conn:
        out2 = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("running the job again does nothing", sum(out2.values()) == 0, str(out2))


def test_request_never_arrived():
    section("Recovery when the request never reached PayPal")
    w = world()
    f = FakePayPal(["timeout_lost"] * 3); install(f)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    send(inv)
    check("nothing at PayPal yet", len(f.batches) == 0 and prow(inv)[0] == "unknown")
    with get_conn() as conn:
        out = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("job sends it once and it is submitted", out["recovered"] == 1 and prow(inv)[0] == "submitted" and len(f.batches) == 1, str(out))


def test_recovery_guards():
    section("Recovery guards: not re-sent if the invoice is no longer approved; still-unknown stays unknown; company scoped")
    w, o = world(), world()
    f = FakePayPal(["timeout_lost"] * 3); install(f)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    send(inv)
    run("UPDATE invoices SET status = 'rejected' WHERE id = %s", (inv,))
    f.calls = 0
    with get_conn() as conn:
        out = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("rejected invoice: PayPal is NOT called", f.calls == 0 and out["needs_person"] == 1, str(out))
    check("payment stays unknown and a person is told", prow(inv)[0] == "unknown" and one(
        "SELECT count(*) FROM exceptions e JOIN payments p ON p.id = e.payment_id WHERE p.invoice_id = %s AND e.type = 'unknown_outcome' AND e.status = 'open'", (inv,))[0] == 1)
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    f = FakePayPal(["timeout_lost"] * 3 + ["timeout_lost"] * 3); install(f)
    send(inv2)
    with get_conn() as conn:
        out = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("PayPal still silent: stays unknown", out["still_unknown"] >= 1 and prow(inv2)[0] == "unknown", str(out))
    inv3 = mk_invoice(o["cid"], o["vid"], o["po_id"], status="approved")
    f = FakePayPal(["timeout_lost"] * 3); install(f); send(inv3)
    with get_conn() as conn:
        reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("another company's payment is not touched (it would have succeeded if it were)", prow(inv3)[0] == "unknown")
    f = FakePayPal(["timeout_lost"] * 3 + ["400"]); install(f)
    inv4 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved"); send(inv4)
    with get_conn() as conn:
        out = reconcile.recover_unknown(conn, w["cid"], older_than=reconcile.timedelta(0)); conn.commit()
    check("PayPal refuses on recovery: payment failed with an exception (no money moved)", prow(inv4)[0] == "failed" and out["failed"] >= 1, str(out))
    f = FakePayPal(["timeout_lost"] * 3); install(f)
    inv5 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved"); send(inv5)
    run("UPDATE payments SET updated_at = now() - interval '5 minutes' WHERE invoice_id = %s", (inv5,))
    with get_conn() as conn:
        counts = reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    check("run_reconcile runs the recovery step", counts.get("recovery_recovered", 0) >= 1 and prow(inv5)[0] == "submitted", str(counts))
    inv6 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    install(FakePayPal(["timeout_lost"] * 3)); send(inv6)
    with get_conn() as conn:
        reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    check("a payment younger than 60s is left alone by the normal job", prow(inv6)[0] == "unknown")


def test_amount_cannot_be_tampered():
    section("Amount tampering: nobody can change what is paid after approval")
    w = world()
    f = FakePayPal(); install(f)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount="100.00")
    rv = bearer(w["users"]["reviewer"][0])
    r = client.post(f"/review/{inv}/pay", json={"confirm_missing_delivery": True, "amount": "1.00", "currency": "EUR", "destination_email": "thief@evil.test"}, headers=rv)
    check("extra fields (amount, currency, email) in the request are ignored", r.status_code in (200, 409, 422), r.text[:200])
    sent = one("SELECT amount, currency, destination_email FROM payments WHERE invoice_id = %s", (inv,))
    if sent:
        check("payout used the stored invoice amount, currency and email", str(sent[0]) == "100.00" and sent[1] == "USD" and sent[2] != "thief@evil.test", str(sent))
    else:
        check("no payment was made with tampered fields", True)
    check("no API route accepts an invoice amount edit", client.patch(f"/invoices/{inv}", json={"amount": "1"}, headers=rv).status_code in (404, 405))
    check("no PUT/POST either", client.put(f"/invoices/{inv}", json={"amount": "1"}, headers=rv).status_code in (404, 405))
    # raising the PO amount after approval cannot let a bigger payment slip through: re-verify runs and any change is audited
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount="100.00")
    run("UPDATE invoices SET amount = 900.00 WHERE id = %s", (inv2,))   # a direct database edit, the only way in
    from backend.investigator import investigate
    from scripts.eval import install_hostile_stub
    install_hostile_stub()
    investigate(inv2)
    check("if the stored amount is changed behind our back, Verify no longer approves it", one("SELECT status FROM invoices WHERE id = %s", (inv2,))[0] != "approved")
    res = send(inv2)
    check("and pay does not send it to PayPal", one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv2,))[0] == 0, str(res))


def main():
    for t in (test_retry_and_permanent_failure, test_timeout_after_paypal_paid, test_request_never_arrived,
              test_recovery_guards, test_amount_cannot_be_tampered):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
