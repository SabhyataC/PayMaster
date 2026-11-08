"""Vendor behaviour signals. Run on a TEST database:  python -m tests.test_anomaly"""
import sys

from tests.test_hardening import (  # noqa: F401
    RESULTS, check, mk_invoice, mk_po, mk_vendor, one, run, section, world,
)
from backend.fraud import run_fraud_signals  # noqa: E402


def names(inv):
    return {s["signal"]: s["severity"] for s in run_fraud_signals(inv)["signals"]}


def history(w, amounts, days_ago_start=40):
    for i, a in enumerate(amounts):
        inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="paid", amount=str(a))
        run("UPDATE invoices SET received_at = now() - make_interval(days => %s) WHERE id = %s", (days_ago_start + i * 3, inv))


def test_unusual_amount():
    section("Anomaly: amount far above this vendor's normal")
    w = world()
    history(w, [100, 110, 95, 105])
    check("normal amount: no signal", "amount_unusual_for_vendor" not in names(mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="130")))
    check("3x median: medium", names(mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="320")).get("amount_unusual_for_vendor") == "medium")
    check("5x median: high", names(mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="520")).get("amount_unusual_for_vendor") == "high")
    s = [x for x in run_fraud_signals(mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="520"))["signals"] if x["signal"] == "amount_unusual_for_vendor"][0]
    check("reason names the amount, usual amount and history size", "520" in s["reason"] and "usual 102.50" in s["reason"] and "4 earlier" in s["reason"], s["reason"])
    w2 = world()
    history(w2, [100, 100])
    check("fewer than 3 earlier invoices: no signal (not enough to judge)", "amount_unusual_for_vendor" not in names(mk_invoice(w2["cid"], w2["vid"], w2["po_id"], status="received", amount="900")))
    w3 = world()
    for a in (100, 100, 100):
        mk_invoice(w3["cid"], w3["vid"], w3["po_id"], status="held", amount=str(a))
    check("held or rejected history is not trusted as 'normal'", "amount_unusual_for_vendor" not in names(mk_invoice(w3["cid"], w3["vid"], w3["po_id"], status="received", amount="900")))
    o = world()
    history(o, [100, 100, 100, 100])
    check("another company's history is not used", "amount_unusual_for_vendor" not in names(mk_invoice(w3["cid"], w3["vid"], w3["po_id"], status="received", amount="900")))


def test_burst():
    section("Anomaly: many invoices from one vendor in a day")
    w = world()
    ids = [mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="50") for _ in range(3)]
    check("3 in a day: no signal", "invoice_burst" not in names(ids[-1]))
    ids.append(mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="50"))
    check("4 in a day: medium", names(ids[-1]).get("invoice_burst") == "medium")
    ids += [mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="50") for _ in range(2)]
    check("6 in a day: high", names(ids[-1]).get("invoice_burst") == "high")
    run("UPDATE invoices SET received_at = now() - interval '3 days' WHERE id = ANY(%s)", (ids[:-1],))
    check("older invoices fall out of the window", "invoice_burst" not in names(ids[-1]))
    o = world()
    check("a different vendor is not affected", "invoice_burst" not in names(mk_invoice(o["cid"], o["vid"], o["po_id"], status="received", amount="50")))


def main():
    for t in (test_unusual_amount, test_burst):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
