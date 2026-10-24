"""Run the investigator on the 5 planted scenarios and print one summary table.
Gate: no bad invoice may end up approved."""
import time

from backend.db import get_conn
from backend.investigator import investigate



# invoice number -> (scenario, decisions that count as correct)
SCENARIOS = {
    "INV-1001": ("clean", {"approve"}),
    "INV-2001": ("clean", {"approve"}),
    "INV-3001": ("clean", {"approve"}),
    "INV-4001": ("clean", {"approve"}),
    "INV-5001": ("clean", {"approve"}),
    "INV-6001": ("changed bank", {"hold"}),
    "INV-7001": ("duplicate (original)", {"approve"}),
    "inv-7001": ("duplicate (copy)", {"hold", "reject"}),
    "INV-8001": ("no PO", {"hold", "reject"}),
    "INV-9001": ("look-alike vendor", {"hold", "reject"}),
}


def main():
    bad_approved = 0
    for number, (scenario, ok) in SCENARIOS.items():
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM invoices WHERE invoice_number = %s", (number,)
            ).fetchone()
        result = investigate(row[0])
        v = result["verdict"]
        flag = "ok" if v["decision"] in ok else "UNEXPECTED"
        if v["decision"] == "approve" and "approve" not in ok:
            flag = "BAD INVOICE APPROVED"
            bad_approved += 1
        forced = any(e["check"] in ("safety_gate", "fraud_gate", "reject_cap") for e in v["evidence"])
        print(f"{number:9} {scenario:22} -> {v['decision']:7} {flag:5} "
              f"{'(gate forced) ' if forced else ''}{', '.join(v['reasons'])}")
        time.sleep(5)  # free-tier rate limit
    print("\nGATE PASSED" if bad_approved == 0 else f"\nGATE FAILED: {bad_approved} bad invoice(s) approved")


if __name__ == "__main__":
    main()