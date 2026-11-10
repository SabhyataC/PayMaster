"""§5 The exported report (Markdown, JSON) says what the on-screen report says, numbers agree with the database,
and a report can never trigger a payment. Needs Chromium (playwright). Run on a TEST database:
   python -m tests.test_report_export"""
import json
import sys
from pathlib import Path

from tests.test_report import craft_trail  # noqa: F401  (also installs the hostile stub)
from tests.test_hardening import RESULTS, bearer, check, client, mk_invoice, one, run, section, world  # noqa: F401
from backend.investigator import investigate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_export_matches_screen():
    section("§5 Markdown / JSON export and the on-screen report carry the same facts")
    from playwright.sync_api import sync_playwright
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil@evil.example.com")
    investigate(inv)
    h = bearer(w["users"]["admin"][0])
    r = client.get(f"/report/invoice/{inv}", headers=h).json()
    js = (ROOT / "console" / "report.js").read_text(encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page()
        pg.set_content("<html><body><div id=root></div></body></html>")
        pg.add_script_tag(content=js)
        pg.evaluate("r => PMReport.render(document.getElementById('root'), r)", r)
        screen = pg.inner_text("#root")
        md = pg.evaluate("r => PMReport.markdown(r)", r)
        b.close()
    flat = lambda t: " ".join(t.split()).lower()   # the page upper-cases some labels with CSS
    S, M = flat(screen), flat(md)
    claims = [c["claim"] for c in r["claims"]]
    check("the report has claims to compare", len(claims) >= 3, len(claims))
    check("every claim appears on screen", all(flat(c) in S for c in claims), [c for c in claims if flat(c) not in S][:2])
    check("every claim appears in the Markdown export", all(flat(c.replace("|", "/")) in M for c in claims))
    ids = {x["id"] for c in r["claims"] for x in c["records"]} | {x["id"] for s in r["steps"] for x in s["records"]}
    check("every cited record id appears in the Markdown export", all(flat(i) in M for i in ids), [i for i in ids if flat(i) not in M][:2])
    check("every cited record id appears on screen", all(flat(i) in S for i in ids), [i for i in ids if flat(i) not in S][:2])
    check("status is in the export, and the screen shows its stamp (held invoice: not a green 'approved')",
          f"status: {r['status']}" in M and "approved" not in flat(screen.split("Findings")[0][:400]).replace("not approved", ""), r["status"])
    for k in ("invoice_number", "vendor", "amount", "report_id", "recommendation", "summary"):
        check(f"{k} is the same on screen and in the export", flat(str(r[k])) in S and flat(str(r[k])) in M, r[k])
    check("the JSON export is the API response itself (nothing added or dropped)", json.loads(json.dumps(r)) == r)
    check("the export states that AI only suggests", "AI only suggests" in md or "only suggests" in md)


def test_numbers_and_ids_match_database():
    section("§5 numbers in the report equal the database; no invented ids; report is read-only")
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", amount="250.00")
    investigate(inv)
    h = bearer(w["users"]["admin"][0])
    r = client.get(f"/report/invoice/{inv}", headers=h).json()
    db_amount, db_status, db_num = one("SELECT amount, status, invoice_number FROM invoices WHERE id = %s", (inv,))
    check("amount equals the invoice row", str(db_amount) == r["amount"], (db_amount, r["amount"]))
    check("status equals the invoice row", db_status == r["status"], (db_status, r["status"]))
    check("invoice number equals the invoice row", db_num == r["invoice_number"])
    check("vendor name equals the vendor row", one("SELECT name FROM vendors WHERE id = %s", (w["vid"],))[0] == r["vendor"])
    from tests.test_report import all_records_exist
    check("every record id in the report exists in the database (none invented)", all_records_exist(r, w["cid"]) == [])
    before = (one("SELECT count(*) FROM payments")[0], one("SELECT status FROM invoices WHERE id = %s", (inv,))[0],
              one("SELECT count(*) FROM audit_log")[0])
    for _ in range(3):
        client.get(f"/report/invoice/{inv}", headers=h)
    after = (one("SELECT count(*) FROM payments")[0], one("SELECT status FROM invoices WHERE id = %s", (inv,))[0],
             one("SELECT count(*) FROM audit_log")[0])
    check("reading a report 3 times creates no payment, changes no status, writes no audit row", before == after, (before, after))
    for m in ("post", "put", "patch", "delete"):
        check(f"the report endpoint accepts no {m.upper()}", getattr(client, m)(f"/report/invoice/{inv}", headers=h).status_code == 405)
    r2 = client.get(f"/report/invoice/{inv}", headers=h).json()
    strip = lambda d: {k: v for k, v in d.items() if k not in ("generated_at", "report_id")}
    check("reproducible: the same invoice gives the same report content", strip(r) == strip(r2))
    import re
    src = (ROOT / "backend" / "report.py").read_text()
    check("report.py has no INSERT/UPDATE/DELETE and never imports pay", not re.search(r"\b(INSERT|UPDATE|DELETE)\b", src) and "import pay" not in src and "send_payout" not in src)


def main():
    for t in (test_export_matches_screen, test_numbers_and_ids_match_database):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
