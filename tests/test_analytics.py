"""Analytics endpoint tests. Run on a TEST database:  python -m tests.test_analytics"""
import sys

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, mk_po, mk_vendor, one, run, section, uniq, world,
)
from scripts.eval import install_hostile_stub

install_hostile_stub()
from backend.investigator import investigate  # noqa: E402


def test_analytics():
    section("Analytics: numbers match the data, one company only, empty company is fine")
    w, o = world(), world()
    h = bearer(w["users"]["viewer"][0])
    empty = client.get("/analytics", headers=h)
    check("empty company: 200 with zero totals", empty.status_code == 200 and empty.json()["total"] == 0 and empty.json()["kpi"]["auto_rate"] is None)
    check("timeline is zero-filled for the whole window", len(empty.json()["timeline"]) == 30)
    good = [mk_invoice(w["cid"], w["vid"], w["po_id"], status="received") for _ in range(3)]
    for i in good:
        investigate(i)
    bad = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil@other-domain.example.net")
    investigate(bad)
    mk_invoice(o["cid"], o["vid"], o["po_id"], status="held", amount="999.00")  # other company
    r = client.get("/analytics?days=30", headers=h).json()
    check("total counts only this company", r["total"] == 4, str(r["total"]))
    check("timeline sums to total", sum(d["good"] + d["stopped"] + d["other"] for d in r["timeline"]) == 4)
    check("stopped count includes the bad invoice", r["kpi"]["stopped_count"] >= 1)
    check("verified count", r["kpi"]["verified"] == 4, str(r["kpi"]))
    check("auto rate between 0 and 1", 0 <= r["kpi"]["auto_rate"] <= 1)
    check("reasons list the bank change", any(x["key"] == "bank_details_changed" for x in r["reasons"]), str(r["reasons"]))
    check("other company's amount not counted", r["kpi"]["stopped_amount"] < 999)
    check("days validated", client.get("/analytics?days=1", headers=h).status_code == 422)
    check("no login: refused", client.get("/analytics").status_code in (401, 403))
    check("90-day window has 90 points", len(client.get("/analytics?days=90", headers=h).json()["timeline"]) == 90)
    check("vendors list present", isinstance(r["vendors"], list) and r["vendors"][0]["invoices"] == 4, str(r["vendors"]))


def main():
    try:
        test_analytics()
    except Exception as e:
        import traceback; traceback.print_exc()
        check("test_analytics ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
