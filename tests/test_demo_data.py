"""Demo data button: off by default, admin only, adds to ONE company only, once, audited, and the
story comes out right. Run on a TEST database:  python -m tests.test_demo_data"""
import os
import sys

from tests.test_hardening import RESULTS, bearer, check, client, one, section, world  # noqa: F401


def invoices(cid):
    from backend.db import get_conn
    with get_conn() as conn:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT invoice_number, status, uploaded_by FROM invoices WHERE company_id = %s", (cid,)).fetchall()}


def test_demo_data():
    section("Demo data button")
    from scripts.eval import install_hostile_stub
    install_hostile_stub()                       # AI that approves everything: the code gates must still hold
    a, b = world(), world()
    ha, hb = bearer(a["users"]["admin"][0]), bearer(b["users"]["admin"][0])
    os.environ.pop("ALLOW_DEMO_DATA", None)
    r = client.get("/admin/demo-data", headers=ha)
    check("switched off by default: status says enabled=false", r.status_code == 200 and r.json()["enabled"] is False, r.text)
    r = client.post("/admin/demo-data", headers=ha)
    check("switched off: POST is refused (403) and nothing is created", r.status_code == 403 and not invoices(a["cid"]), r.text)
    before = len(invoices(a["cid"]))
    os.environ["ALLOW_DEMO_DATA"] = "1"
    try:
        for role in ("viewer", "reviewer"):
            r = client.post("/admin/demo-data", headers=bearer(a["users"][role][0]))
            check(f"a {role} cannot add demo data", r.status_code in (401, 403), r.status_code)
        check("anonymous cannot add demo data", client.post("/admin/demo-data").status_code in (401, 403))
        other_before = len(invoices(b["cid"]))
        r = client.post("/admin/demo-data", headers=ha)
        check("admin adds the demo data (200, 5 invoices listed)", r.status_code == 200 and len(r.json()["created"]) == 5, r.text)
        inv = invoices(a["cid"])
        new = {k: v for k, v in inv.items() if k in ("NW-1001", "KL-2044", "BF-310", "nw 1001.", "SM-777")}
        check("the five story invoices exist in the admin's company", len(new) == 5, list(inv))
        check("they were uploaded by the admin who clicked", all(str(v[1]) == str(a["users"]["admin"][0]) for v in new.values()))
        check("verify ran: clean approved", new["NW-1001"][0] == "approved", new["NW-1001"])
        check("verify ran: bank change held, not approved", new["KL-2044"][0] in ("held", "rejected"), new["KL-2044"])
        check("verify ran: billed before delivery is not approved", new["BF-310"][0] in ("held", "rejected"), new["BF-310"])
        check("verify ran: copy of an earlier invoice is rejected", new["nw 1001."][0] == "rejected", new["nw 1001."])
        from backend.db import get_conn
        from backend.policy import check_policy
        with get_conn() as conn:
            iid = conn.execute("SELECT id FROM invoices WHERE company_id = %s AND invoice_number = 'SM-777'", (a["cid"],)).fetchone()[0]
            pol = check_policy(conn, iid)
        check("over the limit: Verify may approve it, but the payment policy holds it for a person",
              "amount_over_limit" in str(pol), pol)
        check("another company got nothing", len(invoices(b["cid"])) == other_before)
        r = client.post("/admin/demo-data", headers=ha)
        check("a second click is refused (409) and adds nothing", r.status_code == 409 and len(invoices(a["cid"])) == len(inv), r.text)
        check("status now says loaded", client.get("/admin/demo-data", headers=ha).json()["loaded"] is True)
        n = one("SELECT count(*) FROM audit_log WHERE entity_id = %s AND decision = 'demo_data_loaded'", (a["cid"],))[0]
        check("the click is in the audit log", n == 1, n)
        check("company B's admin can still load their own", client.post("/admin/demo-data", headers=hb).status_code == 200)
        vend = one("SELECT count(*) FROM vendors WHERE company_id = %s AND name = 'Northwind Stationers'", (a["cid"],))[0]
        check("exactly one Northwind in company A", vend == 1, vend)
    finally:
        os.environ.pop("ALLOW_DEMO_DATA", None)


def main():
    try:
        test_demo_data()
    except Exception as e:
        import traceback; traceback.print_exc()
        check("test_demo_data ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
