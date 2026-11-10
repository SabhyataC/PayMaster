"""Returned payouts: ledger reversal, invoice restored, idempotency, ordering, atomicity, reconcile.
Run on a TEST database:  python -m tests.test_ledger_reversal"""
import sys

import psycopg

from tests.test_hardening import RESULTS, check, one, run, section, raises, _paypal_world  # noqa: F401
from tests.test_payment_reliability import FakePayPal, install, send  # noqa: F401
from backend import settle, reconcile  # noqa: E402
from backend.db import get_conn  # noqa: E402

FEE = {"value": "0.25", "currency": "USD"}


class PP:
    """Scripted PayPal read side: .status is what the batch says now."""

    def __init__(self, key, status="SUCCESS", fee=FEE, conversion=None, amount="100.00", cur="USD"):
        self.key, self.status, self.fee, self.conversion, self.amount, self.cur = key, status, fee, conversion, amount, cur

    def batch(self, _b):
        return {"batch_header": {"sender_batch_header": {"sender_batch_id": self.key}},
                "items": [{"transaction_status": self.status, "payout_item_id": "ITEM-R", "payout_item_fee": self.fee}]}

    def item(self, _i):
        d = {"transaction_status": self.status, "payout_item": {"amount": {"value": self.amount, "currency": self.cur}},
             "payout_item_fee": self.fee}
        if self.conversion:
            d["currency_conversion"] = self.conversion
        return d

    def install(self):
        settle.fetch_batch, settle.fetch_item = self.batch, self.item
        reconcile.fetch_item = self.item


def sync(batch):
    with get_conn() as conn:
        return settle.sync_payment(conn, batch)


def rows(pid):
    return one("SELECT count(*) FROM ledger_entries WHERE payment_id = %s", (pid,))[0]


def reversal_count(pid):
    return one("SELECT count(*) FROM ledger_entries WHERE payment_id = %s AND entry_type LIKE 'reversal\\_%%'", (pid,))[0]


def net_by_account(pid):
    with get_conn() as conn:
        return conn.execute(
            "SELECT account, currency, sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END) "
            "FROM ledger_entries WHERE payment_id = %s GROUP BY 1, 2", (pid,)).fetchall()


def balanced_per_currency(pid):
    with get_conn() as conn:
        r = conn.execute("SELECT currency, sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END) "
                         "FROM ledger_entries WHERE payment_id = %s GROUP BY 1", (pid,)).fetchall()
    return all(v == 0 for _, v in r)


def inv_status(inv):
    return one("SELECT status FROM invoices WHERE id = %s", (inv,))[0]


def pay_status(pid):
    return one("SELECT status FROM payments WHERE id = %s", (pid,))[0]


def test_success_then_return():
    section("§1 success -> returned: mirror entries, balanced, invoice restored only after PayPal confirms")
    w, inv, pid, batch, key = _paypal_world()
    pp = PP(key); pp.install()
    check("payout succeeds", sync(batch)["result"] == "updated" and inv_status(inv) == "paid")
    before = rows(pid)
    check("original ledger has 4 lines and balances", before == 4 and balanced_per_currency(pid))
    # a webhook that only *claims* a return changes nothing: we always re-read PayPal, and PayPal still says SUCCESS
    check("while PayPal still says SUCCESS nothing is reversed", sync(batch)["result"] == "no_change"
          and reversal_count(pid) == 0 and inv_status(inv) == "paid")
    pp.status = "RETURNED"
    r = sync(batch)
    check("PayPal confirms the return: payment becomes 'returned'", r["result"] == "updated" and pay_status(pid) == "returned", r)
    check("4 mirror lines appended, originals untouched", reversal_count(pid) == 4 and rows(pid) == 8)
    check("the ledger still balances per currency", balanced_per_currency(pid))
    check("every account nets to zero (money, fee and balance all restored)", all(v == 0 for _, _, v in net_by_account(pid)), net_by_account(pid))
    check("invoice is payable again (approved), not 'paid'", inv_status(inv) == "approved")
    ex = one("SELECT count(*) FROM exceptions WHERE payment_id = %s AND type = 'payment_returned' AND status = 'open'", (pid,))[0]
    check("an open 'payment_returned' exception tells a person", ex == 1)
    a = one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND decision = 'returned' AND (inputs ->> 'reversal_rows')::int = 4", (pid,))[0]
    check("the audit log records the return and how many lines were reversed", a == 1)
    check("the settlement record is kept (history is never deleted)", one("SELECT count(*) FROM settlements WHERE payment_id = %s", (pid,))[0] == 1)
    from tests.test_hardening import bearer, client
    t = client.get(f"/trust/invoice/{inv}", headers=bearer(w["users"]["admin"][0])).json()
    link = {n.get("key") or n.get("name"): n for n in t["chain"]}
    check("Trust Graph does not call a returned payout 'complete': verdict is broken and the Reconcile link is red",
          t["verdict"] != "complete" and link["reconcile"]["state"] == "problem" and "payment_returned" in link["reconcile"]["summary"], t["verdict"])
    check("Trust Graph ledger link stays green: reversal keeps debits = credits", link["ledger"]["state"] == "ok", link["ledger"])
    return w, inv, pid, batch, pp


def test_duplicate_and_out_of_order():
    section("§1 duplicate return webhook, delayed older events, return before the success was ever booked")
    w, inv, pid, batch, key = _paypal_world()
    pp = PP(key); pp.install()
    sync(batch); pp.status = "RETURNED"; sync(batch)
    n = rows(pid)
    for i in range(3):
        r = sync(batch)
    check("the return delivered 3 more times: no_change, no extra lines", r["result"] == "no_change" and rows(pid) == n)
    check("still exactly one open exception", one("SELECT count(*) FROM exceptions WHERE payment_id = %s AND type = 'payment_returned'", (pid,))[0] == 1)
    for late in ("SUCCESS", "PENDING", "FAILED"):
        pp.status = late
        r = sync(batch)
        check(f"a delayed {late} event after the return cannot undo it", r["result"] == "no_change" and pay_status(pid) == "returned" and inv_status(inv) == "approved", r)
    check("and wrote nothing", rows(pid) == n)
    # return arrives while we never booked the payout (we were still 'pending')
    w2, inv2, pid2, batch2, key2 = _paypal_world(status="pending")
    pp2 = PP(key2, status="RETURNED"); pp2.install()
    r = sync(batch2)
    check("return before success was booked: payment returned, nothing to reverse", pay_status(pid2) == "returned" and rows(pid2) == 0, r)
    check("invoice is not left 'paid' and can be paid again", inv_status(inv2) == "approved")
    pp2.status = "SUCCESS"
    r = sync(batch2)
    check("the delayed SUCCESS that arrives afterwards books nothing", r["result"] == "no_change" and rows(pid2) == 0 and pay_status(pid2) == "returned", r)
    # unclaimed then returned (the usual PayPal case): no ledger was ever written
    w3, inv3, pid3, batch3, key3 = _paypal_world(status="pending")
    pp3 = PP(key3, status="UNCLAIMED"); pp3.install(); sync(batch3)
    pp3.status = "RETURNED"; sync(batch3)
    check("unclaimed -> returned: no ledger lines either way", rows(pid3) == 0 and pay_status(pid3) == "returned")


def test_fx_reversal():
    section("§1 reversal of a currency-converted payout is balanced in EVERY currency")
    w, inv, pid, batch, key = _paypal_world()
    run("UPDATE payments SET currency = 'EUR', amount = 100 WHERE id = %s", (pid,))
    conv = {"from_amount": {"value": "110.00", "currency": "USD"}, "to_amount": {"value": "100.00", "currency": "EUR"}, "exchange_rate": "0.9090"}
    pp = PP(key, fee={"value": "0", "currency": "USD"}, conversion=conv, amount="100.00", cur="EUR"); pp.install()
    sync(batch)
    check("converted payout booked and balanced per currency", rows(pid) > 0 and balanced_per_currency(pid))
    pp.status = "RETURNED"; sync(batch)
    check("reversal posted for the converted payout", reversal_count(pid) > 0 and pay_status(pid) == "returned")
    check("still balanced in USD and in EUR", balanced_per_currency(pid))
    check("each account in each currency nets to zero", all(v == 0 for _, _, v in net_by_account(pid)), net_by_account(pid))


def test_atomicity():
    section("§1 partial failure: a reversal that cannot be written leaves NOTHING half-done, and a retry works")
    w, inv, pid, batch, key = _paypal_world()
    pp = PP(key); pp.install(); sync(batch)
    pp.status = "RETURNED"
    real = settle.reversal_rows
    settle.reversal_rows = lambda orig: real(orig)[:-1]          # one line missing: would be unbalanced
    try:
        r = raises(lambda: sync(batch), ValueError)
    finally:
        settle.reversal_rows = real
    check("an unbalanced reversal is refused", r is True, r)
    check("payment still 'success', no reversal lines, invoice still 'paid'", pay_status(pid) == "success" and reversal_count(pid) == 0 and inv_status(inv) == "paid")
    check("no exception raised for a return we did not apply", one("SELECT count(*) FROM exceptions WHERE payment_id = %s AND type = 'payment_returned'", (pid,))[0] == 0)
    r = sync(batch)
    check("the retry (PayPal re-sends the webhook) applies it fully", r["result"] == "updated" and reversal_count(pid) == 4 and inv_status(inv) == "approved", r)


def test_new_attempt_and_reconcile():
    section("§1 returned -> a NEW payment attempt with a new key; reconcile finds returns nobody told us about")
    w, inv, pid, batch, key = _paypal_world()
    pp = PP(key); pp.install(); sync(batch)
    pp.status = "RETURNED"; sync(batch)
    f = FakePayPal(); install(f)
    r = send(inv)
    check("paying again is allowed and creates attempt 2", r["status"] == "submitted" and
          one("SELECT max(attempt) FROM payments WHERE invoice_id = %s", (inv,))[0] == 2, str(r))
    keys = [k[0] for k in [one("SELECT idempotency_key FROM payments WHERE invoice_id = %s AND attempt = %s", (inv, a)) for a in (1, 2)]]
    check("with a different PayPal-Request-Id (PayPal would otherwise answer 'duplicate')", keys[0] != keys[1])
    check("the first payment's history is unchanged", pay_status(pid) == "returned" and reversal_count(pid) == 4)
    with get_conn() as conn:
        out = reconcile.close_returned(conn, w["cid"]); conn.commit()
    check("reconcile closes the 'returned' exception once a new attempt exists", out["returned_closed"] == 1, str(out))
    # no webhook ever arrived: reconcile asks PayPal and applies the return itself
    w2, inv2, pid2, batch2, key2 = _paypal_world()
    pp2 = PP(key2); pp2.install(); sync(batch2)
    run("UPDATE payments SET created_at = now() - interval '3 days', paypal_item_id = 'ITEM-R' WHERE id = %s", (pid2,))
    pp2.status = "RETURNED"
    with get_conn() as conn:
        out = reconcile.detect_paypal_disagreements(conn, w2["cid"]); conn.commit()
    check("reconcile detects the returned payout and applies it", out.get("returned_applied") == 1, str(out))
    check("ledger reversed, invoice payable, no webhook needed", reversal_count(pid2) == 4 and inv_status(inv2) == "approved" and pay_status(pid2) == "returned")
    with get_conn() as conn:
        out = reconcile.detect_paypal_disagreements(conn, w2["cid"]); conn.commit()
    check("running reconcile again changes nothing", not out.get("returned_applied") and reversal_count(pid2) == 4)


def test_append_only_and_history():
    section("§1 the ledger is append-only: nobody can edit or delete a line, so history is auditable")
    w, inv, pid, batch, key = _paypal_world()
    pp = PP(key); pp.install(); sync(batch)
    check("UPDATE of a ledger line is refused", raises(lambda: run("UPDATE ledger_entries SET amount = 1 WHERE payment_id = %s", (pid,)), psycopg.Error) is True)
    check("DELETE of a ledger line is refused", raises(lambda: run("DELETE FROM ledger_entries WHERE payment_id = %s", (pid,)), psycopg.Error) is True)
    check("TRUNCATE is refused", raises(lambda: run("TRUNCATE ledger_entries"), psycopg.Error) is True)
    pp.status = "RETURNED"; sync(batch)
    with get_conn() as conn:
        hist = [r[0] for r in conn.execute("SELECT entry_type FROM ledger_entries WHERE payment_id = %s ORDER BY id", (pid,))]
    check("history keeps the 4 original lines and adds 4 reversal lines beside them", sorted(h for h in hist if not h.startswith("reversal_")) == ["fee", "fee", "payment", "payment"]
          and len([h for h in hist if h.startswith("reversal_")]) == 4, hist)
    with get_conn() as conn:
        n = conn.execute("SELECT count(*) FROM audit_log WHERE entity_id = %s AND stage = 'pay'", (pid,)).fetchone()[0]
    check("success and return are both in the audit log", n >= 2, n)


def main():
    for t in (test_success_then_return, test_duplicate_and_out_of_order, test_fx_reversal, test_atomicity,
              test_new_attempt_and_reconcile, test_append_only_and_history):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
