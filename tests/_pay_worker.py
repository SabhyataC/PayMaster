"""One separate OS process for tests/test_concurrent_processes.py. Waits for a common start time, then acts.
   python -m tests._pay_worker pay <invoice_id> <start_epoch>
   python -m tests._pay_worker reject <invoice_id> <start_epoch>"""
import json
import sys
import time

action, invoice_id, start_at = sys.argv[1], sys.argv[2], float(sys.argv[3])

from backend import pay, review  # noqa: E402  (imported before the start time so every process is ready)
from backend.db import get_conn  # noqa: E402

while time.time() < start_at:
    time.sleep(0.001)
out = {"action": action}
try:
    with get_conn() as conn:
        if action == "pay":
            r = pay.pay_invoice(conn, invoice_id, approved_by="Person " + str(int(start_at * 1000) % 7))
            out.update(status=r.get("status"), payment_id=str(r.get("payment_id")), reasons=r.get("reasons"))
        else:
            review.decide(conn, invoice_id, "reject", "Person B", "Rejecting this one right now.")
            out.update(status="rejected_ok")
except review.ReviewError as e:
    out.update(status="refused", code=e.code)
except Exception as e:
    out.update(status="crashed", error=f"{type(e).__name__}: {e}"[:300])
print(json.dumps(out))
