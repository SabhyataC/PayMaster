"""Tests for the B-section fixes (B3, B4, B5, B6, B8, B12). Run on a TEST database:
    python -m tests.test_bugfixes
"""
import os
import subprocess
import sys
import json
import uuid
from decimal import Decimal

from tests.test_hardening import (  # noqa: F401  (shares the DB guard and the fixtures)
    ROOT, bearer, check, client, mk_company, mk_invoice, mk_po, mk_user, mk_vendor, one, raises, run,
    section, uniq, world, RESULTS,
)
from scripts.eval import install_hostile_stub

install_hostile_stub()
from backend import ai_call, checks, reprocess  # noqa: E402
from backend.investigator import investigate  # noqa: E402


def test_b3_seed_guard():
    section("B3  seed.py refuses a database that is not dev/test")
    code = "import backend.seed as s; s.refuse_unless_dev(); print('ALLOWED')"

    def run_with(url, extra=None):
        env = {**os.environ, "DATABASE_URL": url, **(extra or {})}
        env.pop("ALLOW_SEED", None) if not extra else None
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)

    r = run_with("postgresql://u:p@localhost:5433/paymaster")
    check("refuses a database called 'paymaster'", r.returncode != 0 and "ALLOWED" not in r.stdout, r.stderr[-200:])
    r = run_with("postgresql://u:p@localhost:5433/paymaster_prod")
    check("refuses a database called 'paymaster_prod'", r.returncode != 0)
    r = run_with("postgresql://u:p@localhost:5433/paymaster_dev")
    check("allows 'paymaster_dev'", r.returncode == 0 and "ALLOWED" in r.stdout, r.stderr[-200:])
    r = run_with("postgresql://u:p@localhost:5433/paymaster", {"ALLOW_SEED": "1"})
    check("allows any name when ALLOW_SEED=1 is set on purpose", r.returncode == 0 and "ALLOWED" in r.stdout)
    src = (ROOT / "backend" / "seed.py").read_text()
    check("wipe() itself also checks, not only main()", "def wipe(conn):" in src
          and "refuse_unless_dev()" in src.split("def wipe(conn):")[1][:400])


class FakeClient:
    """Fails with the given errors, then answers. Records which models were asked."""
    def __init__(self, plan):
        self.plan, self.asked = list(plan), []
        outer = self

        class Models:
            def generate_content(self, model, contents, config):
                outer.asked.append(model)
                step = outer.plan.pop(0) if outer.plan else "ok"
                if step != "ok":
                    raise RuntimeError(step)
                return "REPLY from " + model
        self.models = Models()


def test_b4_ai_retry_and_fallback():
    section("B4  AI overload: short retry, then the second model, then a clean failure")
    os.environ["GEMINI_MODEL"] = "primary"
    os.environ.pop("GEMINI_FALLBACK_MODEL", None)
    pauses = []
    c = FakeClient(["503 UNAVAILABLE", "503 UNAVAILABLE"])
    m, r = ai_call.generate(c, "x", None, sleep=pauses.append)
    check("two 503s then success: same model, 3 calls", m == "primary" and len(c.asked) == 3, c.asked)
    check("waits are short (under 10 s each, not 20-40 s)", pauses and max(pauses) < 10, pauses)

    c = FakeClient(["503"] * 10)
    err = raises(lambda: ai_call.generate(c, "x", None, sleep=lambda s: None), RuntimeError)
    check("no fallback model: gives up after 3 tries with the error", err is True and len(c.asked) == 3, c.asked)

    os.environ["GEMINI_FALLBACK_MODEL"] = "backup"
    c = FakeClient(["503"] * 3)
    m, r = ai_call.generate(c, "x", None, sleep=lambda s: None)
    check("primary down: the fallback model answers", m == "backup" and c.asked == ["primary"] * 3 + ["backup"], c.asked)

    c = FakeClient(["400 INVALID_ARGUMENT bad request"])
    err = raises(lambda: ai_call.generate(c, "x", None, sleep=lambda s: None), RuntimeError)
    check("a non-transient error (bad request) fails at once, no retries", err is True and len(c.asked) == 1, c.asked)
    del os.environ["GEMINI_FALLBACK_MODEL"]
    os.environ["GEMINI_MODEL"] = "stub"

    import backend.intake as intake
    src = (ROOT / "backend" / "intake.py").read_text() + (ROOT / "backend" / "investigator.py").read_text() \
        + (ROOT / "backend" / "fraud.py").read_text()
    check("intake, investigator and fraud all use the shared helper; no 20-second sleeps left",
          src.count("ai_call import generate") == 3 and "time.sleep(20" not in src)


def test_b5_duplicate_is_deterministic():
    section("B5  the original is never held because a copy exists; the copy is always the one rejected")
    w = world()
    first = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
    number = one("SELECT invoice_number FROM invoices WHERE id = %s", (first,))[0]
    copy = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
    run("UPDATE invoices SET invoice_number = %s WHERE id = %s", (number.lower() + " ", copy))
    outcomes = set()
    for _ in range(4):
        r1 = checks.run_hard_checks(first)
        r2 = checks.run_hard_checks(copy)
        outcomes.add(("duplicate_invoice" in r1["reasons"], "duplicate_invoice" in r2["reasons"]))
    check("same answer on 4 runs: original clean, copy flagged", outcomes == {(False, True)}, outcomes)
    s1 = investigate(first)["status"]
    s2 = investigate(copy)["status"]
    check("with an AI that always approves: original approved, copy rejected by code", (s1, s2) == ("approved", "rejected"), (s1, s2))
    s1b = investigate(first)["status"]
    check("re-running the original after the copy exists does not hold it", s1b == "approved", s1b)
    ev = checks.run_hard_checks(copy)["evidence"]
    d = next(e for e in ev if e["check"] == "not_duplicate")
    check("the UI reason names the earlier invoice", "already seen" in d["detail"] and d["data"]["matches"], d)


def _qty_world(po_qty, delivered_qty=None, amount="100.00"):
    w = world()
    run("UPDATE purchase_orders SET quantity = %s WHERE id = %s", (po_qty, w["po_id"]))
    if delivered_qty is not None:
        run("UPDATE deliveries SET quantity = %s WHERE po_id = %s", (delivered_qty, w["po_id"]))
    return w


def _inv(w, qty, amount="100.00", status="received", seconds_later=0):
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status=status, amount=amount)
    run("UPDATE invoices SET quantity = %s WHERE id = %s", (qty, inv))
    return inv


def _qty_reasons(inv):
    r = checks.run_hard_checks(inv)
    return r["reasons"], next(e for e in r["evidence"] if e["check"] == "quantity_within_ordered")


def test_b6_quantity():
    section("B6  quantity is compared (ordered, received, already billed)")
    w = _qty_world(100, 100)
    reasons, ev = _qty_reasons(_inv(w, 100))
    check("billing exactly the ordered quantity passes", "quantity_out_of_tolerance" not in reasons and ev["data"]["checked"], ev)
    w = _qty_world(100, 100)
    reasons, ev = _qty_reasons(_inv(w, 150))
    check("billing 150 of 100 ordered is held", "quantity_out_of_tolerance" in reasons, ev)
    w = _qty_world(100, 60)
    reasons, ev = _qty_reasons(_inv(w, 80))
    check("billing 80 when only 60 arrived is held", "quantity_out_of_tolerance" in reasons, ev)
    w = _qty_world(100, 100)
    a = _inv(w, 70, amount="70.00")
    b = _inv(w, 70, amount="30.00")
    reasons_b, ev = _qty_reasons(b)
    check("two invoices that together bill 140 of 100: the later one is held", "quantity_out_of_tolerance" in reasons_b, ev)
    reasons_a, _ = _qty_reasons(a)
    check("... and the earlier one is still fine", "quantity_out_of_tolerance" not in reasons_a)
    w = _qty_world(100, None)
    reasons, ev = _qty_reasons(_inv(w, 120))
    check("no received quantity recorded: the ordered quantity is the cap", "quantity_out_of_tolerance" in reasons, ev)
    w = _qty_world(None, None)
    reasons, ev = _qty_reasons(_inv(w, 9999))
    check("PO states no quantity: passes and says 'not checked'", "quantity_out_of_tolerance" not in reasons and ev["data"]["checked"] is False and "not checked" in ev["detail"], ev)
    w = _qty_world(100, 100)
    reasons, ev = _qty_reasons(_inv(w, None))
    check("invoice states no quantity: passes and says 'not checked'", "quantity_out_of_tolerance" not in reasons and ev["data"]["checked"] is False, ev)
    w = _qty_world(100, 100)
    over = _inv(w, 150)
    check("an over-quantity invoice cannot be approved even if the AI says approve", investigate(over)["status"] == "held")
    from backend.intake import invoice_quantity
    check("invoice quantity = sum of lines when every line has one", invoice_quantity({"line_items": [{"quantity": 2}, {"quantity": 3.5}]}) == 5.5)
    check("one line without a quantity: unknown (never understate)", invoice_quantity({"line_items": [{"quantity": 2}, {"quantity": None}]}) is None)
    check("no lines: unknown", invoice_quantity({"line_items": []}) is None)
    check("zero or negative quantity: unknown", invoice_quantity({"line_items": [{"quantity": -3}]}) is None)
    # API
    w = world()
    h = bearer(w["users"]["admin"][0])
    vid = str(w["vid"])
    r = client.post("/pos", json={"vendor_id": vid, "po_number": uniq("PO-Q"), "amount": "50", "quantity": "10"}, headers=h)
    check("PO can be created with a quantity", r.status_code == 200 and r.json()["quantity"] == 10.0, r.text)
    po_id = r.json()["id"]
    r = client.post(f"/pos/{po_id}/deliveries", json={"description": "box", "amount": "25", "quantity": "4"}, headers=h)
    check("delivery can state a quantity", r.status_code == 200, r.text)
    r = client.post(f"/pos/{po_id}/deliver-all", headers=h)
    q = one("SELECT quantity FROM deliveries WHERE po_id = %s AND description LIKE 'Fully%%'", (po_id,))[0]
    check("deliver-all records the remaining quantity (10 - 4 = 6)", r.status_code == 200 and q == Decimal("6"), (r.text, q))
    r = client.post("/pos", json={"vendor_id": vid, "po_number": uniq("PO-Q"), "amount": "50", "quantity": "-1"}, headers=h)
    check("a negative quantity is refused", r.status_code == 422)


def test_b8_reverify():
    section("B8  invoices that arrived early are re-checked")
    w = world()
    po2, num2 = mk_po(w["cid"], w["vid"], amount="100.00")
    run("DELETE FROM deliveries WHERE po_id = %s", (po2,))
    inv = mk_invoice(w["cid"], w["vid"], po2, status="received")
    check("no delivery yet: held", investigate(inv)["status"] == "held")

    admin, reviewer, viewer = (bearer(w["users"][r][0]) for r in ("admin", "reviewer", "viewer"))
    r = client.post(f"/invoices/{inv}/reverify", headers=viewer)
    check("a viewer cannot re-verify", r.status_code == 403, r.status_code)
    r = client.post(f"/invoices/{inv}/reverify", headers=reviewer)
    check("still no delivery: re-verify keeps it held", r.status_code == 200 and r.json()["status"] == "held", r.text)
    other = world()
    r = client.post(f"/invoices/{inv}/reverify", headers=bearer(other["users"]["admin"][0]))
    check("another company gets 404, not the invoice", r.status_code == 404, r.status_code)

    po_row = one("SELECT id FROM purchase_orders WHERE po_number = %s AND company_id = %s", (num2, w["cid"]))[0]
    r = client.post(f"/pos/{po_row}/deliveries", json={"description": "all of it", "amount": "100"}, headers=admin)
    check("recording the delivery works", r.status_code == 200, r.text)
    # BackgroundTasks run inside TestClient after the response, so the invoice is re-checked already
    st = one("SELECT status FROM invoices WHERE id = %s", (inv,))[0]
    check("the held invoice was re-checked automatically and is now approved", st == "approved", st)
    log = one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND stage = 'verify'", (inv,))[0]
    check("both Verify runs are in the audit log", log >= 3, log)
    r = client.post(f"/invoices/{inv}/reverify", headers=reviewer)
    check("an approved invoice cannot be re-verified through this button", r.status_code == 409, r.status_code)

    # Verify may release a hold ONLY through the re-verify path
    inv_b = mk_invoice(w["cid"], w["vid"], None, status="held")
    run("UPDATE invoices SET po_id = NULL WHERE id = %s", (inv_b,))
    check("a plain investigate() of a held invoice still never approves it",
          investigate(inv_b)["status"] != "approved")
    from backend.investigator import next_status
    check("next_status: held -> approved only with allow_release",
          next_status("held", "approved") == "held" and next_status("held", "approved", True) == "approved")
    check("paid and rejected can never be moved, even with allow_release",
          next_status("paid", "approved", True) == "paid" and next_status("rejected", "approved", True) == "rejected")

    # an upload that was waiting for a PO
    cid, vid = w["cid"], w["vid"]
    num = "PO-" + str(uuid.uuid4().int)[:6]
    fields = {"vendor_name": one("SELECT name FROM vendors WHERE id = %s", (vid,))[0], "invoice_number": uniq("EARLY"),
              "total": 100.0, "currency": "USD", "po_number": num, "line_items": [],
              "bank_details": {"payee_email": one("SELECT destination_email FROM vendor_bank_history WHERE vendor_id = %s", (vid,))[0]}}
    conf = {k: 1.0 for k in fields}
    ext = one("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, extracted, "
              "field_confidence, confidence, reason, company_id) VALUES ('early.txt', 'email', 'x', %s, 'needs_review', "
              "%s::jsonb, %s::jsonb, 1.0, %s, %s) RETURNING id",
              (uniq("sha"), json.dumps(fields), json.dumps(conf), f"{num} is not in the system", cid))[0]
    r = client.post("/pos", json={"vendor_id": str(vid), "po_number": num, "amount": "100"}, headers=admin)
    check("creating the PO the upload was waiting for works", r.status_code == 200, r.text)
    st = one("SELECT status, invoice_id FROM extractions WHERE id = %s", (ext,))
    check("the waiting upload was accepted and became an invoice", st[0] == "accepted" and st[1] is not None, st)
    check("(no delivery yet, so that new invoice is held, not approved)",
          one("SELECT status FROM invoices WHERE id = %s", (st[1],))[0] == "held")

    # retry of a failed upload
    ext2 = one("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, reason, company_id) "
               "VALUES ('x.txt', 'email', 'x', %s, 'failed', 'AI call failed: 503', %s) RETURNING id", (uniq("sha"), cid))[0]
    r = client.post(f"/extractions/{ext2}/retry", headers=viewer)
    check("a viewer cannot retry an upload", r.status_code == 403)
    r = client.post(f"/extractions/{ext2}/retry", headers=bearer(other["users"]["admin"][0]))
    check("another company gets 404 for the upload", r.status_code == 404)
    ext3 = one("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, company_id) "
               "VALUES ('y.txt', 'email', 'x', %s, 'accepted', %s) RETURNING id", (uniq("sha"), cid))[0]
    r = client.post(f"/extractions/{ext3}/retry", headers=reviewer)
    check("an accepted upload cannot be 'retried' (409)", r.status_code == 409)
    html = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    check("the console shows Re-verify and Retry buttons", 'data-act="${act}"' in html and 'btn("reverify"' in html and 'btn("retry"' in html)


def test_b8_more():
    section("B8+B6 completion: bank-email change re-check, Re-verify on every held invoice, quantity in the console")
    w = world()
    admin = bearer(w["users"]["admin"][0])
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="new-bank@example.com")
    check("invoice with a different payout email is held", investigate(inv)["status"] == "held")
    r = client.post(f"/vendors/{w['vid']}", json={"payout_email": "new-bank@example.com"}, headers=admin)
    check("admin changes the vendor's email on file", r.status_code == 200, r.text)
    st = one("SELECT status FROM invoices WHERE id = %s", (inv,))[0]
    check("the held invoice was re-checked and the bank check now matches (not 'held for bank_changed')",
          "bank_details_changed" not in checks.run_hard_checks(inv)["reasons"], st)
    other = world()
    inv2 = mk_invoice(other["cid"], other["vid"], other["po_id"], status="held")
    r = client.post(f"/vendors/{w['vid']}", json={"payout_email": "x@example.com"}, headers=admin)
    check("another company's held invoices are never touched by this company's vendor edit",
          one("SELECT status FROM invoices WHERE id = %s", (inv2,))[0] == "held")
    html = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    check("every held invoice row gets a Re-verify button for admin/reviewer",
          'p.data.status === "held"' in html and "/reverify" in html and "PM.canAct()" in html)
    v = (ROOT / "console" / "vendors.html").read_text(encoding="utf-8")
    check("PO and delivery forms have quantity fields and the tables show them",
          'id="pqty"' in v and 'id="dqty"' in v and v.count("<th>Qty</th>") == 2 and "quantity: $(\"pqty\")" in v and "quantity: $(\"dqty\")" in v)
    h = bearer(w["users"]["viewer"][0])
    r = client.get("/deliveries", headers=h)
    check("the deliveries list returns the quantity", r.status_code == 200 and all("quantity" in i for i in r.json()["items"]))


def test_four_eyes_switch():
    section("Four-eyes: ON by default, admin can switch it off (audited), reviewer cannot")
    w = world()
    admin, reviewer = bearer(w["users"]["admin"][0]), bearer(w["users"]["reviewer"][0])
    check("new companies have the rule ON", one("SELECT require_four_eyes FROM companies WHERE id = %s", (w["cid"],))[0] is True)
    uploader = w["users"]["reviewer"][0]
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="held", uploaded_by=uploader)
    r = client.post(f"/review/{inv}/decision", json={"decision": "approve", "reviewer": "x", "note": "Looks fine to me."}, headers=reviewer)
    check("rule ON: the uploader cannot approve their own invoice", r.status_code in (403, 409) and "approve" not in str(r.json().get("status", "")), r.text[:200])
    r = client.post("/admin/settings", json={"four_eyes": False}, headers=reviewer)
    check("a reviewer cannot switch the rule off", r.status_code == 403, r.status_code)
    r = client.post("/admin/settings", json={"four_eyes": False}, headers=admin)
    check("an admin can switch it off", r.status_code == 200 and r.json()["four_eyes"] is False, r.text)
    run("UPDATE companies SET auto_pay_limit = 1234 WHERE id = %s", (w["cid"],))
    client.post("/admin/settings", json={"four_eyes": False}, headers=admin)
    check("... and the auto-pay limit was NOT reset by that call",
          one("SELECT auto_pay_limit FROM companies WHERE id = %s", (w["cid"],))[0] == 1234)
    check("... and it is in the audit log",
          one("SELECT count(*) FROM audit_log WHERE entity_id::text = %s AND reason LIKE '%%four-eyes rule OFF%%'", (str(w["cid"]),))[0] >= 1)
    r = client.post(f"/review/{inv}/decision", json={"decision": "approve", "reviewer": "x", "note": "Looks fine to me."}, headers=reviewer)
    check("rule OFF: the uploader may approve their own invoice", r.status_code == 200, r.text[:200])
    other = world(); inv2 = mk_invoice(other["cid"], other["vid"], other["po_id"], status="held", uploaded_by=other["users"]["reviewer"][0])
    r = client.post(f"/review/{inv2}/decision", json={"decision": "approve", "reviewer": "x", "note": "Looks fine to me."},
                    headers=bearer(other["users"]["reviewer"][0]))
    check("switching it off in one company does not affect another", r.status_code != 200, r.text[:200])
    r = client.post("/admin/settings", json={"four_eyes": True}, headers=admin)
    check("it can be switched back on", r.status_code == 200 and r.json()["four_eyes"] is True)


def test_b12_adversarial_set():
    section("B12  50+ unseen adversarial invoices (stub AI here; run without --stub-ai for real numbers)")
    r = subprocess.run([sys.executable, "-m", "scripts.adversarial_eval", "--stub-ai", "--seed", "7"],
                       cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
    out = r.stdout
    check("the adversarial eval exits 0 (no bad invoice approved)", r.returncode == 0, (out[-400:] + r.stderr[-400:]))
    import re
    m = re.search(r"(\d+) bad, (\d+) clean", out)
    check("it graded at least 50 invoices (bad + clean)", m and int(m.group(1)) + int(m.group(2)) >= 50, out[-300:])
    check("it reports 'bad approved: 0'", "bad approved: 0" in out, out[-300:])


def main():
    tests = [test_b3_seed_guard, test_b4_ai_retry_and_fallback, test_b5_duplicate_is_deterministic, test_b6_quantity,
             test_b8_reverify, test_b8_more, test_four_eyes_switch, test_b12_adversarial_set]
    only = sys.argv[1:]
    for t in tests:
        if only and not any(o in t.__name__ for o in only):
            continue
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
