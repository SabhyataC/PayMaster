"""Trust Graph tests. Run on a TEST database:  python -m tests.test_trust"""
import sys
import uuid
from decimal import Decimal

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, mk_po, mk_vendor, one, run, section, uniq, world,
)
from scripts.eval import install_hostile_stub

install_hostile_stub()
from backend.investigator import investigate  # noqa: E402
from backend.trust import ORDER, build_chain  # noqa: E402
from backend.db import get_conn  # noqa: E402


def chain(w, inv, payment_id=None):
    with get_conn() as conn:
        return build_chain(conn, inv, w["cid"], payment_id)


def states(c):
    return {n["key"]: n["state"] for n in c["chain"]}


def add_payment(w, inv, status="success", email=None, amount=None, minutes_ago=60, batch=None):
    run("UPDATE vendor_bank_history SET valid_from = now() - interval '2 days' WHERE vendor_id = %s AND valid_from > now() - interval '2 days'", (w["vid"],))
    email = email or one("SELECT destination_email FROM vendor_bank_history WHERE vendor_id = %s AND valid_to IS NULL", (w["vid"],))[0]
    amount = amount or one("SELECT amount FROM invoices WHERE id = %s", (inv,))[0]
    pid = one("INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, status, "
              "paypal_batch_id, company_id, created_at) VALUES (%s, %s, %s, %s, 'USD', %s, %s, %s, now() - make_interval(mins => %s)) "
              "RETURNING id", (inv, uniq("k"), email, amount, status, batch or uniq("BATCH"), w["cid"], minutes_ago))[0]
    return pid


def add_ledger(w, pid, amount, balanced=True):
    amount = Decimal(str(amount))
    rows = [("payment", "accounts_payable", "debit", amount), ("payment", "paypal_balance", "credit", amount if balanced else amount - 1)]
    for t, a, d, v in rows:
        run("INSERT INTO ledger_entries (payment_id, entry_type, account, direction, amount, currency, company_id) "
            "VALUES (%s, %s, %s, %s, %s, 'USD', %s)", (pid, t, a, d, v, w["cid"]))


def add_webhook(pid, processed=True):
    batch = one("SELECT paypal_batch_id FROM payments WHERE id = %s", (pid,))[0]
    run("INSERT INTO webhook_events (event_id, event_type, batch_id, payload, attempts, processed_at, result) "
        "VALUES (%s, 'PAYMENT.PAYOUTSBATCH.SUCCESS', %s, '{}'::jsonb, 2, " + ("now()" if processed else "NULL") + ", 'ok')",
        (uniq("WH"), batch))


def add_settlement(pid, amount):
    run("INSERT INTO settlements (payment_id, amount_sent, currency_sent, amount_received, currency_received, fee_amount) "
        "VALUES (%s, %s, 'USD', %s, 'USD', 0.25)", (pid, amount, amount))


def test_chain_shape_and_happy_path():
    section("Trust Graph: nine links in order; a fully proven payment is all green")
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
    investigate(inv)
    c = chain(w, inv)
    check("nine nodes in the documented order", [n["key"] for n in c["chain"]] == ORDER, [n["key"] for n in c["chain"]])
    s = states(c)
    check("before payment: the first five links are green", all(s[k] == "ok" for k in ORDER[:5]), s)
    check("before payment: payout, webhook, ledger, reconcile are pending (not red)",
          all(s[k] == "pending" for k in ORDER[5:]), s)
    check("verdict is 'incomplete', not 'broken'", c["verdict"] == "incomplete", c["verdict"])
    pid = add_payment(w, inv, minutes_ago=60)
    add_webhook(pid); add_ledger(w, pid, 100); add_settlement(pid, 100)
    c = chain(w, inv)
    check("every link green after payout, webhook, ledger and settlement", all(n["state"] == "ok" for n in c["chain"]) and c["verdict"] == "complete", states(c))
    check("every node carries a kind: fact or ai", all(n["kind"] in ("fact", "ai") for n in c["chain"]))
    check("every node lists the record ids behind it", all(n["records"] for n in c["chain"] if n["key"] != "reconcile") , [n["key"] for n in c["chain"] if not n["records"]])
    check("the approval node carries the AI's suggestion, labelled as only a suggestion",
          c["chain"][4]["ai"] and "only suggests" in c["chain"][4]["ai"]["note"])
    run("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, confidence, invoice_id, company_id) "
        "VALUES ('inv.txt', 'email', 'x', %s, 'accepted', 0.93, %s, %s)", (uniq("sha"), inv, w["cid"]))
    c = chain(w, inv)
    check("an invoice read by Gemini is tagged 'ai'", c["chain"][1]["kind"] == "ai" and c["chain"][0]["kind"] == "fact")


def test_problems_show_red():
    section("Trust Graph: missing or mismatching evidence turns the right link red")
    w = world()
    # bank details differ from the one on file
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="other@evil.example.net")
    investigate(inv)
    s = states(chain(w, inv))
    check("changed bank details: the Invoice link is red", s["invoice"] == "problem", s)
    # no delivery
    po2, _ = mk_po(w["cid"], w["vid"]); run("DELETE FROM deliveries WHERE po_id = %s", (po2,))
    inv2 = mk_invoice(w["cid"], w["vid"], po2, status="received"); investigate(inv2)
    c = chain(w, inv2)
    check("no delivery: the Delivery link is red and says so", states(c)["delivery"] == "problem" and "delivery" in c["chain"][3]["summary"].lower(), c["chain"][3]["summary"])
    # no PO
    inv3 = mk_invoice(w["cid"], w["vid"], None, status="received"); investigate(inv3)
    s = states(chain(w, inv3))
    check("no PO: the PO and Delivery links are red", s["po"] == "problem" and s["delivery"] == "problem", s)
    # duplicate
    orig = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    num = one("SELECT invoice_number FROM invoices WHERE id = %s", (orig,))[0]
    dup = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
    run("UPDATE invoices SET invoice_number = %s WHERE id = %s", (num.lower() + " ", dup)); investigate(dup)
    c = chain(w, dup)
    check("a duplicate: the Invoice link is red and the Approval link is red (rejected)", states(c)["invoice"] == "problem" and states(c)["approval"] == "problem", states(c))

    good = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount="50.00")
    investigate(good)
    pid = add_payment(w, good, email="attacker@evil.example.net")
    check("paid to an email that was not on file: the Payout link is red", states(chain(w, good))["payout"] == "problem")
    run("UPDATE payments SET destination_email = (SELECT destination_email FROM vendor_bank_history WHERE vendor_id = %s AND valid_to IS NULL), amount = 49 WHERE id = %s", (w["vid"], pid))
    check("paid the wrong amount: the Payout link is red", states(chain(w, good))["payout"] == "problem")
    run("UPDATE payments SET amount = 50 WHERE id = %s", (pid,))
    check("a successful payout with no webhook after 10 minutes: Webhook link red", states(chain(w, good))["webhook"] == "problem")
    add_webhook(pid, processed=False)
    check("a webhook that was never processed: Webhook link red", states(chain(w, good))["webhook"] == "problem")
    check("a successful payout with no ledger rows: Ledger link red", states(chain(w, good))["ledger"] == "problem")
    add_ledger(w, pid, 50, balanced=False)
    c = chain(w, good)
    check("an unbalanced ledger: Ledger link red", states(c)["ledger"] == "problem", c["chain"][7]["summary"])
    run("DELETE FROM ledger_entries WHERE payment_id = %s", (pid,)); add_ledger(w, pid, 40)
    c = chain(w, good)
    check("the ledger records a different amount than was paid: Ledger link red",
          states(c)["ledger"] == "problem" and "ledger records" in c["chain"][7]["summary"], c["chain"][7]["summary"])
    run("INSERT INTO exceptions (payment_id, type, detail, company_id) VALUES (%s, 'unclaimed_payout', '{}'::jsonb, %s)", (pid, w["cid"]))
    check("an open exception: Reconcile link red", states(chain(w, good))["reconcile"] == "problem")
    check("the overall verdict is 'broken'", chain(w, good)["verdict"] == "broken")
    c = chain(w, good)
    check("problem links explain themselves in words", all(n["problems"] for n in c["chain"] if n["state"] == "problem"))


def test_human_approval_and_api_isolation():
    section("Trust Graph: a person's approval is shown by name; other companies see nothing")
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="new@vendor.example.com")
    investigate(inv)
    check("held for a person: Approval link is pending", states(chain(w, inv))["approval"] == "pending")
    run("INSERT INTO reviews (invoice_id, original_ai_decision, human_decision, reviewer, note, confirmed_with_vendor, "
        "status_before, status_after, company_id) VALUES (%s, 'hold', 'approve', 'Priya Shah', 'Called the vendor, email confirmed.', true, 'held', 'approved', %s)",
        (inv, w["cid"]))
    c = chain(w, inv)
    check("Approval names the person and their note", "Priya Shah" in c["chain"][4]["summary"] and any("Called the vendor" in f["value"] for f in c["chain"][4]["facts"]))

    viewer = bearer(w["users"]["viewer"][0])
    r = client.get(f"/trust/invoice/{inv}", headers=viewer)
    check("a viewer can read the graph", r.status_code == 200 and len(r.json()["chain"]) == 9, r.status_code)
    r2 = client.get(f"/trust/invoice/{one('SELECT invoice_number FROM invoices WHERE id = %s', (inv,))[0]}", headers=viewer)
    check("it can also be looked up by invoice number", r2.status_code == 200)
    pid = add_payment(w, inv, minutes_ago=1, status="pending")
    r = client.get(f"/trust/payment/{pid}", headers=viewer)
    check("it can be opened from a payment id", r.status_code == 200 and r.json()["payment_id"] == str(pid), r.status_code)
    check("a recent pending payout is pending, not red", states(r.json() and {"chain": r.json()["chain"]})["webhook"] == "pending")
    other = world()
    ob = bearer(other["users"]["admin"][0])
    check("another company gets 404 by invoice id", client.get(f"/trust/invoice/{inv}", headers=ob).status_code == 404)
    check("another company gets 404 by payment id", client.get(f"/trust/payment/{pid}", headers=ob).status_code == 404)
    check("no login: refused", client.get(f"/trust/invoice/{inv}").status_code in (401, 403))
    check("unknown invoice: 404", client.get(f"/trust/invoice/{uuid.uuid4()}", headers=viewer).status_code == 404)
    cc = chain(w, inv)
    check("the graph never contains the other company's data",
          all(w["cid"] != other["cid"] for _ in [0]) and other["po_number"] not in str(cc))


def main():
    for t in (test_chain_shape_and_happy_path, test_problems_show_red, test_human_approval_and_api_isolation):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
