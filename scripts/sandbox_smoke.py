"""Real PayPal SANDBOX smoke test: sends one real sandbox payout and checks the Trust Graph.
Run on your machine with sandbox keys, a TEST/dev database and the server running with a public webhook
(or press Run reconcile after a minute, which syncs from PayPal without a webhook):

    python -m scripts.sandbox_smoke

Needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox) and PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com.
Nothing is inserted by hand except the vendor/PO/invoice the payout is for."""
import os
import sys
import time
import uuid

if "sandbox" not in os.environ.get("PAYPAL_BASE_URL", ""):
    sys.exit("Refusing to run: PAYPAL_BASE_URL is not a sandbox URL.")

from backend.db import get_conn  # noqa: E402
from backend import pay, reconcile  # noqa: E402
from backend.settle import sync_payment  # noqa: E402
from backend.trust import build_chain  # noqa: E402

EMAIL = os.environ.get("SANDBOX_RECEIVER_EMAIL")
if not EMAIL:
    sys.exit("Set SANDBOX_RECEIVER_EMAIL to a sandbox personal account email.")


def main():
    with get_conn() as conn:
        cid = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (f"smoke-{uuid.uuid4().hex[:6]}",)).fetchone()[0]
        vid = conn.execute("INSERT INTO vendors (name, country, company_id) VALUES ('Smoke Vendor', 'US', %s) RETURNING id", (cid,)).fetchone()[0]
        conn.execute("INSERT INTO vendor_bank_history (vendor_id, destination_email, valid_from) VALUES (%s, %s, now())", (vid, EMAIL))
        inv = conn.execute("INSERT INTO invoices (vendor_id, invoice_number, amount, currency, destination_email, status, company_id, received_at) "
                           "VALUES (%s, %s, 1.00, 'USD', %s, 'approved', %s, now()) RETURNING id",
                           (vid, "SMOKE-" + uuid.uuid4().hex[:6], EMAIL, cid)).fetchone()[0]
        conn.commit()
    with get_conn() as conn:
        r = pay.pay_invoice(conn, inv, approved_by="Smoke test", confirm_missing_delivery=True)
    print("pay:", r)
    for i in range(12):
        with get_conn() as conn:
            batch = conn.execute("SELECT paypal_batch_id FROM payments WHERE invoice_id = %s ORDER BY attempt DESC LIMIT 1", (inv,)).fetchone()[0]
            if batch:
                print("sync:", sync_payment(conn, batch))
            reconcile.run_reconcile(conn, company_id=cid, commit=True)
            c = build_chain(conn, inv, cid)
        print(f"[{i}] trust verdict:", c["verdict"], {n["key"]: n["state"] for n in c["chain"]})
        if c["verdict"] == "complete":
            print("OK: real sandbox payout completed end to end.")
            return
        time.sleep(10)
    print("Not complete yet: sandbox payouts can take a minute; rerun sync/reconcile. Last verdict:", c["verdict"])
    sys.exit(1)


if __name__ == "__main__":
    main()
