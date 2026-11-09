"""Full lifecycle, through the real code paths (only PayPal's network is faked). Run on a TEST database:
   python -m tests.test_lifecycle_e2e
invoice -> verify -> human approve -> pay -> webhook -> settlement + ledger -> reconcile -> Trust Graph + lifecycle strip."""
import json
import sys

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, one, run, section, uniq, world,
)
from tests.test_payment_reliability import FakePayPal, install
from scripts.eval import install_hostile_stub
from backend import reconcile, settle, webhooks  # noqa: E402
from backend.db import get_conn  # noqa: E402
from backend.investigator import investigate  # noqa: E402


def _paypal_reports(status, key_of):
    """Make settle.* answer like PayPal for the payment whose batch id is `key_of()`."""
    def batch(b):
        key = one("SELECT idempotency_key FROM payments WHERE paypal_batch_id = %s", (b,))[0]
        return {"batch_header": {"batch_status": "SUCCESS" if status == "SUCCESS" else status,
                                 "sender_batch_header": {"sender_batch_id": key}},
                "items": [{"transaction_status": status, "payout_item_id": "ITEM-" + b,
                           "payout_item_fee": {"value": "0.25", "currency": "USD"}}]}
    settle.fetch_batch = batch
    settle.fetch_item = lambda i: {"payout_item": {"amount": {"value": "100.00", "currency": "USD"}},
                                   "payout_item_fee": {"value": "0.25", "currency": "USD"}}


def _webhook(batch, event_id=None):
    ev = {"id": event_id or uniq("EVT"), "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
          "resource": {"batch_header": {"payout_batch_id": batch}}}
    return ev, client.post("/webhooks/paypal", content=json.dumps(ev))


def _nodes(c):
    return {n["key"]: n["state"] for n in c["chain"]}


def _setup_webhooks():
    import os
    os.environ["PAYPAL_WEBHOOK_ID"] = "WH-TEST"
    webhooks.verify_signature = lambda headers, raw: True


def test_full_lifecycle():
    section("E2E: one invoice, every stage, nothing inserted by hand")
    install_hostile_stub(); _setup_webhooks()
    w = world(); fake = FakePayPal(); install(fake)
    rv = bearer(w["users"]["reviewer"][0])
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="100.00")

    investigate(inv)
    st = one("SELECT status FROM invoices WHERE id = %s", (inv,))[0]
    check("verify ran and produced a verdict (approved or held for a person)", st in ("approved", "held"), st)
    if st == "held":
        r = client.post(f"/review/{inv}/decision", json={"decision": "approve", "note": "checked", "confirmed_with_vendor": True}, headers=rv)
        check("a person approves it", r.status_code == 200, r.text[:200])
    check("invoice is approved", one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] == "approved")

    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    check("before paying: verdict incomplete, nothing red", t["verdict"] == "incomplete" and "problem" not in _nodes(t).values(), t["verdict"])

    r = client.post(f"/review/{inv}/pay", json={}, headers=rv)
    check("pay returns submitted", r.status_code == 200 and r.json().get("status") == "submitted", r.text[:200])
    check("PayPal was called exactly once", fake.calls == 1, fake.calls)
    batch = one("SELECT paypal_batch_id FROM payments WHERE invoice_id = %s", (inv,))[0]
    check("a PayPal batch id was recorded", bool(batch))
    check("no ledger lines or settlement yet", one("SELECT count(*) FROM ledger_entries l JOIN payments p ON p.id = l.payment_id WHERE p.invoice_id = %s", (inv,))[0] == 0)

    _paypal_reports("SUCCESS", None)
    ev, r = _webhook(batch)
    check("the real webhook handler accepts the event", r.status_code == 200 and r.json().get("result") == "updated", r.text[:300])
    check("payment is success", one("SELECT status FROM payments WHERE invoice_id = %s", (inv,))[0] == "success")
    check("invoice is paid", one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] == "paid")
    pid = one("SELECT id FROM payments WHERE invoice_id = %s", (inv,))[0]
    deb = one("SELECT sum(amount) FROM ledger_entries WHERE payment_id = %s AND direction = 'debit'", (pid,))[0]
    cred = one("SELECT sum(amount) FROM ledger_entries WHERE payment_id = %s AND direction = 'credit'", (pid,))[0]
    check("ledger written by the settle code and balanced", deb is not None and deb == cred, (deb, cred))
    check("settlement row exists", one("SELECT count(*) FROM settlements WHERE payment_id = %s", (pid,))[0] == 1)

    r2 = client.post("/webhooks/paypal", content=json.dumps(ev))
    check("replaying the same webhook changes nothing", r2.json().get("action") == "duplicate"
          and one("SELECT count(*) FROM ledger_entries WHERE payment_id = %s", (pid,))[0] == 4)

    with get_conn() as conn:
        reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    check("reconcile finds nothing wrong with this payment", one(
        "SELECT count(*) FROM exceptions WHERE payment_id = %s AND status = 'open'", (pid,))[0] == 0)

    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    s = _nodes(t)
    check("Trust Graph: every link green", t["verdict"] == "complete" and all(v == "ok" for v in s.values()), (t["verdict"], s))
    life = client.get(f"/lifecycle/{inv}", headers=rv).json()
    check("lifecycle strip: every stage done (the human step may be skipped when auto-approved)", all(x["state"] in ("done", "skipped") and (x["state"] == "done" or x["key"] == "human") for x in life["stages"]), [(x["key"], x["state"]) for x in life["stages"]])
    rs = client.get("/reconcile/status", headers=rv).json()
    check("reconcile dashboard shows this company in sync", rs["state"] == "in_sync", rs["state"])


def test_lifecycle_with_timeout_then_recovery():
    section("E2E variant: PayPal pays but our connection dies, recovery finds it, the rest still completes")
    install_hostile_stub(); _setup_webhooks()
    w = world(); fake = FakePayPal(["timeout_after_process"] * 3); install(fake)
    rv = bearer(w["users"]["reviewer"][0])
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="100.00")
    investigate(inv)
    if one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] == "held":
        client.post(f"/review/{inv}/decision", json={"decision": "approve", "note": "ok", "confirmed_with_vendor": True}, headers=rv)
    r = client.post(f"/review/{inv}/pay", json={}, headers=rv)
    check("timeout: payment is unknown", one("SELECT status FROM payments WHERE invoice_id = %s", (inv,))[0] == "unknown", r.text[:200])
    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    check("Trust Graph does not say complete while unknown", t["verdict"] != "complete", t["verdict"])
    run("UPDATE payments SET updated_at = now() - interval '5 minutes' WHERE invoice_id = %s", (inv,))
    with get_conn() as conn:
        counts = reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    check("the reconcile job recovered it", counts.get("recovery_recovered") == 1, counts)
    check("PayPal still holds exactly one payout", len(fake.batches) == 1, fake.batches)
    batch = one("SELECT paypal_batch_id FROM payments WHERE invoice_id = %s", (inv,))[0]
    _paypal_reports("SUCCESS", None)
    _, r = _webhook(batch)
    with get_conn() as conn:
        reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    check("after the webhook the chain is complete", t["verdict"] == "complete", (t["verdict"], _nodes(t)))
    check("one payment row, one set of ledger lines", one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 1
          and one("SELECT count(*) FROM ledger_entries l JOIN payments p ON p.id = l.payment_id WHERE p.invoice_id = %s", (inv,))[0] == 4)


def test_lifecycle_paypal_failure():
    section("E2E variant: PayPal reports FAILED, money did not move, nothing is booked")
    install_hostile_stub(); _setup_webhooks()
    w = world(); fake = FakePayPal(); install(fake)
    rv = bearer(w["users"]["reviewer"][0])
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount="100.00")
    client.post(f"/review/{inv}/pay", json={}, headers=rv)
    batch = one("SELECT paypal_batch_id FROM payments WHERE invoice_id = %s", (inv,))[0]
    _paypal_reports("FAILED", None)
    _webhook(batch)
    check("payment is failed", one("SELECT status FROM payments WHERE invoice_id = %s", (inv,))[0] == "failed")
    check("no ledger lines for a failed payout", one("SELECT count(*) FROM ledger_entries l JOIN payments p ON p.id = l.payment_id WHERE p.invoice_id = %s", (inv,))[0] == 0)
    check("invoice is not marked paid", one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] != "paid")
    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    check("Trust Graph is not complete", t["verdict"] != "complete", t["verdict"])


def _stub_reader(fields):
    """Replace only the Gemini call inside Intake. Everything after it (parsing, scoring, routing) is the real code."""
    from backend import intake
    reply = json.dumps({"fields": fields, "field_confidence": {k: 1.0 for k in intake.FIELDS}})
    intake._call_model = lambda contents: ("stub-model", reply)


def test_upload_to_reconcile():
    section("E2E from the UPLOAD button: file in, reconciled payment and a complete Trust Graph out")
    from backend.db import get_conn as gc
    install_hostile_stub(); _setup_webhooks()
    w = world(); fake = FakePayPal(); install(fake)
    rv = bearer(w["users"]["reviewer"][0])
    vendor = one("SELECT name FROM vendors WHERE id = %s", (w["vid"],))[0]
    email = one("SELECT destination_email FROM vendor_bank_history WHERE vendor_id = %s AND valid_to IS NULL", (w["vid"],))[0]
    num = uniq("UP")
    _stub_reader({"vendor_name": vendor, "invoice_number": num, "invoice_date": "2026-11-01", "due_date": None, "po_number": w["po_number"],
                  "line_items": [{"description": "goods", "quantity": 1, "unit_price": 100.0}], "currency": "USD", "tax": None, "total": 100.0,
                  "bank_details": {"account_holder": None, "bank_name": None, "account_number": None, "routing_or_swift": None, "payee_email": email},
                  "sender_email": email})
    text = f"From: {email}\nSubject: Invoice {num}\n\nPlease pay {num}, total 100.00 USD, PO {w['po_number']}.".encode()
    r = client.post("/invoices/upload", headers=rv, files={"file": (f"{num}.txt", text, "text/plain")})
    check("upload accepted and read", r.status_code == 200 and r.json()["intake"] == "accepted", r.text[:300])
    d = r.json(); inv = d["invoice_id"]
    check("the invoice exists with the extracted amount and vendor", one("SELECT amount, vendor_id FROM invoices WHERE id = %s", (inv,))[0] == 100 and
          one("SELECT vendor_id FROM invoices WHERE id = %s", (inv,))[0] == w["vid"])
    check("intake is on the audit trail", one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND stage = 'intake'", (inv,))[0] == 1)
    check("Verify decided", d["verdict"] in ("approve", "hold", "reject"), d["verdict"])
    ad = bearer(w["users"]["admin"][0])      # four-eyes: the person who uploaded may not release their own invoice
    if d["invoice_status"] == "held":
        client.post(f"/review/{inv}/decision", json={"decision": "approve", "note": "ok", "confirmed_with_vendor": True}, headers=ad)
    check("a first-time payee is held for a second person, not paid automatically", d.get("payment") in ("held", "submitted"), d.get("payment"))
    if one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 0:
        own = client.post(f"/review/{inv}/pay", json={}, headers=rv)
        check("the uploader cannot release their own invoice", own.status_code in (403, 409, 422) and one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 0, (own.status_code, own.text[:120]))
        client.post(f"/review/{inv}/pay", json={}, headers=ad)
    pay_row = one("SELECT status, paypal_batch_id FROM payments WHERE invoice_id = %s", (inv,))
    check("a payment was sent exactly once", pay_row and pay_row[0] == "submitted" and fake.calls == 1, (pay_row, fake.calls))
    _paypal_reports("SUCCESS", None)
    _, wr = _webhook(pay_row[1])
    check("PayPal's webhook settles it", wr.status_code == 200 and one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] == "paid")
    with gc() as conn:
        reconcile.run_reconcile(conn, company_id=w["cid"], commit=True)
    t = client.get(f"/trust/invoice/{inv}", headers=rv).json()
    check("Trust Graph: every link green from a real upload", t["verdict"] == "complete" and all(v == "ok" for v in _nodes(t).values()), (t["verdict"], _nodes(t)))
    again = client.post("/invoices/upload", headers=rv, files={"file": (f"{num}.txt", text, "text/plain")})
    check("uploading the same file again is refused, not paid twice", again.status_code == 409 and fake.calls == 1, again.text[:120])
    from backend import audit_chain
    with gc() as conn:
        v = audit_chain.verify(conn, w["cid"])
    check("the whole story is on a valid, tamper-evident audit chain", v["ok"] and v["checked"] >= 4, v)


def main():
    for t in (test_full_lifecycle, test_lifecycle_with_timeout_then_recovery, test_lifecycle_paypal_failure, test_upload_to_reconcile):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
