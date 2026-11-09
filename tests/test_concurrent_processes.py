"""Concurrent payments from SEPARATE OS PROCESSES against a fake PayPal over real HTTP.
Threads share one Python interpreter; separate processes share nothing but the database, which is how two
app servers or two browser tabs behave in production.   python -m tests.test_concurrent_processes (TEST database)"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.test_hardening import RESULTS, check, mk_invoice, one, section, uniq, world

LOCK = threading.Lock()
STATE = {"by_key": {}, "payout_calls": 0, "fail_first": 0, "seen_after_fail": 0}


class PayPalHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if self.path.endswith("/oauth2/token"):
            return self._send(200, {"access_token": "T", "expires_in": 300})
        key = self.headers.get("PayPal-Request-Id")
        with LOCK:
            STATE["payout_calls"] += 1
            if STATE["fail_first"] > 0:
                STATE["fail_first"] -= 1
                return self._send(503, {"name": "SERVICE_UNAVAILABLE"})
        time.sleep(0.4)                                   # a real round trip: this is the race window
        with LOCK:
            if key in STATE["by_key"]:
                batch = STATE["by_key"][key]["batch"]
                return self._send(400, {"name": "USER_BUSINESS_ERROR", "details": [{"field": "SENDER_BATCH_ID", "issue": "duplicate", "link": [{"rel": "self", "href": f"http://x/v1/payments/payouts/{batch}"}]}]})
            STATE["by_key"][key] = {"batch": uniq("BATCH"), "count": 1}
            return self._send(201, {"batch_header": {"payout_batch_id": STATE["by_key"][key]["batch"], "batch_status": "PENDING"}})


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), PayPalHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_workers(plan, delay=2.0):
    """plan: list of (action, invoice_id). Starts every process, then releases them all at the same moment."""
    env = dict(os.environ, PAYPAL_BASE_URL=f"http://127.0.0.1:{SERVER.server_port}", PAYPAL_CLIENT_ID="x", PAYPAL_CLIENT_SECRET="y")
    start_at = time.time() + delay
    procs = [subprocess.Popen([sys.executable, "-m", "tests._pay_worker", a, str(i), str(start_at)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for a, i in plan]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=120)
        try:
            outs.append(json.loads(o.strip().splitlines()[-1]))
        except Exception:
            outs.append({"status": "no_output", "error": (e or o)[-300:]})
    return outs


def reset():
    with LOCK:
        STATE.update(by_key={}, payout_calls=0, fail_first=0)


def test_same_invoice_many_processes():
    section("Eight separate processes release the SAME invoice at the same instant")
    reset(); w = world(); inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    outs = run_workers([("pay", inv)] * 8)
    st = sorted(o["status"] for o in outs)
    check("no process crashed", not [o for o in outs if o["status"] in ("crashed", "no_output")], outs)
    check("exactly one payment row", one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 1)
    check("PayPal received exactly one payout for it", len(STATE["by_key"]) == 1 and all(v["count"] == 1 for v in STATE["by_key"].values()), STATE["by_key"])
    check("PayPal was called once (the other seven never reached it)", STATE["payout_calls"] == 1, STATE["payout_calls"])
    check("one process paid; the other seven were told it already exists", st.count("submitted") == 1 and st.count("already_exists") == 7, st)
    check("every process that names a payment names the same one", len({o["payment_id"] for o in outs if o.get("payment_id") not in (None, "None")}) == 1, outs)
    check("payment is submitted with a batch id", one("SELECT status, paypal_batch_id IS NOT NULL FROM payments WHERE invoice_id = %s", (inv,)) == ("submitted", True))
    check("one payout on the audit trail", one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND stage = 'pay'", (inv,))[0] >= 1)


def test_different_invoices_in_parallel():
    section("Four processes on each of three invoices: three payouts, never more, never fewer")
    reset(); w = world()
    invs = [mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved") for _ in range(3)]
    outs = run_workers([("pay", i) for i in invs for _ in range(4)])
    check("no process crashed", not [o for o in outs if o["status"] in ("crashed", "no_output")], outs)
    check("PayPal got exactly three payouts, one per invoice", len(STATE["by_key"]) == 3 and STATE["payout_calls"] == 3, (len(STATE["by_key"]), STATE["payout_calls"]))
    check("each invoice has exactly one payment", all(one("SELECT count(*) FROM payments WHERE invoice_id = %s", (i,))[0] == 1 for i in invs))


def test_pay_vs_reject_across_processes():
    section("One process pays while another rejects the same invoice")
    bad = 0
    for _ in range(4):
        reset(); w = world(); inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
        outs = run_workers([("pay", inv), ("reject", inv)], delay=1.5)
        st = one("SELECT status FROM invoices WHERE id = %s", (inv,))[0]
        paid = one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0]
        crashed = [o for o in outs if o["status"] in ("crashed", "no_output")]
        if crashed or (st == "rejected" and paid) or len(STATE["by_key"]) > 1 or (paid == 0 and STATE["by_key"]):
            bad += 1; print("   inconsistent:", st, paid, outs)
    check("in 4 races, never rejected-and-paid, never an orphan payout, no crash", bad == 0, bad)


def test_flaky_paypal_under_load():
    section("PayPal answers 503 to the first requests while processes race: retries, still one payout")
    reset(); w = world(); inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    STATE["fail_first"] = 2
    outs = run_workers([("pay", inv)] * 4)
    check("no process crashed", not [o for o in outs if o["status"] in ("crashed", "no_output")], outs)
    check("the paying process retried through the 503s", STATE["payout_calls"] >= 3, STATE["payout_calls"])
    check("still exactly one payout at PayPal and one payment row", len(STATE["by_key"]) == 1 and one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0] == 1)
    check("payment ended submitted, not unknown or failed", one("SELECT status FROM payments WHERE invoice_id = %s", (inv,))[0] == "submitted")


SERVER = None


def main():
    global SERVER
    SERVER = start_server()
    for t in (test_same_invoice_many_processes, test_different_invoices_in_parallel, test_pay_vs_reject_across_processes, test_flaky_paypal_under_load):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    SERVER.shutdown()
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
