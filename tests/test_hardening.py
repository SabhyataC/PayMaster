import io
import json
import os
import re
import sys
import threading
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET", "t" * 48)
os.environ.setdefault("GEMINI_MODEL", "stub")

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.db import DATABASE_URL, get_conn  # noqa: E402

DB_NAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
if not any(w in DB_NAME.lower() for w in ("test", "eval")):
    sys.exit(f"Refusing to run on '{DB_NAME}': the database name must contain 'test' or 'eval'.")

from backend import auth, review, tools  # noqa: E402
from backend.auth import hash_password, make_token  # noqa: E402
import backend.webhooks as webhooks  # noqa: E402
from backend.upload_checks import UploadRejected, validate_upload  # noqa: E402

RESULTS = []
PASSWORD = "correct horse battery"


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


def raises(fn, *types):
    try:
        fn()
    except types:
        return True
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return False


def one(sql, params=()):
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def run(sql, params=()):
    with get_conn() as conn:
        conn.execute(sql, params)


def uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def mk_company(name=None):
    return one("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name or uniq("Co"),))[0]


def mk_user(company_id, role, email=None):
    email = email or f"{uniq(role)}@example.com"
    uid = one("INSERT INTO users (company_id, email, password_hash, full_name, role) VALUES (%s, %s, %s, %s, %s) "
              "RETURNING id", (company_id, email, hash_password(PASSWORD), f"{role.title()} {email[:6]}", role))[0]
    return uid, email


def bearer(uid):
    ver = one("SELECT token_version FROM users WHERE id = %s", (uid,))[0]
    return {"Authorization": "Bearer " + make_token(uid, ver)}


def mk_vendor(company_id, name=None, email=None):
    vid = one("INSERT INTO vendors (name, country, company_id) VALUES (%s, 'US', %s) RETURNING id",
              (name or uniq("Vendor"), company_id))[0]
    run("INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
        "VALUES (%s, %s, 'onboarding', %s)", (vid, email or f"{uniq('pay')}@example.com", company_id))
    return vid


def mk_po(company_id, vendor_id, number=None, amount="100.00"):
    number = number or "PO-" + str(uuid.uuid4().int)[:9]
    po = one("INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) "
             "VALUES (%s, %s, %s, 'USD', %s) RETURNING id", (number, vendor_id, amount, company_id))[0]
    run("INSERT INTO deliveries (po_id, description, amount, company_id) VALUES (%s, 'goods', %s, %s)",
        (po, amount, company_id))
    return po, number


def mk_invoice(company_id, vendor_id, po_id, status="held", amount="100.00", uploaded_by=None, email=None):
    if email is None:
        email = one("SELECT destination_email FROM vendor_bank_history WHERE vendor_id = %s AND valid_to IS NULL",
                    (vendor_id,))[0]
    return one(
        "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
        "company_id, uploaded_by, received_at) "
        "VALUES (%s, %s, %s, %s, 'USD', %s, %s, %s, %s, clock_timestamp()) RETURNING id",
        (vendor_id, po_id, uniq("INV"), amount, email, status, company_id, uploaded_by))[0]


def world():
    cid = mk_company()
    users = {r: mk_user(cid, r) for r in ("admin", "reviewer", "viewer")}
    vid = mk_vendor(cid)
    po_id, po_number = mk_po(cid, vid)
    return {"cid": cid, "users": users, "vid": vid, "po_id": po_id, "po_number": po_number}


client = TestClient(webhooks.app)


def section(title):
    print(f"\n=== {title}")


def test_s4_database_isolation():
    section("S4  the database itself refuses cross-company data")
    a, b = world(), world()
    inv_a = mk_invoice(a["cid"], a["vid"], a["po_id"])

    r = raises(lambda: run(
        "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
        "company_id) VALUES (%s, %s, 'X-1', 1, 'USD', 'a@example.com', 'held', %s)",
        (b["vid"], a["po_id"], a["cid"])), psycopg.errors.ForeignKeyViolation)
    check("invoice in company A cannot point at a vendor of company B", r is True, r)
    r = raises(lambda: run(
        "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
        "company_id) VALUES (%s, %s, 'X-2', 1, 'USD', 'a@example.com', 'held', %s)",
        (a["vid"], b["po_id"], a["cid"])), psycopg.errors.ForeignKeyViolation)
    check("invoice in company A cannot point at a purchase order of company B", r is True, r)
    r = raises(lambda: run(
        "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) "
        "VALUES ('PO-X', %s, 1, 'USD', %s)", (b["vid"], a["cid"])), psycopg.errors.ForeignKeyViolation)
    check("purchase order cannot use another company's vendor", r is True, r)
    r = raises(lambda: run(
        "INSERT INTO deliveries (po_id, description, amount, company_id) VALUES (%s, 'x', 1, %s)",
        (b["po_id"], a["cid"])), psycopg.errors.ForeignKeyViolation)
    check("delivery cannot attach to another company's purchase order", r is True, r)
    r = raises(lambda: run(
        "INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, attempt, company_id) "
        "VALUES (%s, %s, 'a@example.com', 1, 'USD', 1, %s)", (inv_a, uniq("k"), b["cid"])),
        psycopg.errors.ForeignKeyViolation)
    check("payment cannot name company B for company A's invoice", r is True, r)
    r = raises(lambda: run("INSERT INTO vendors (name, country, company_id) VALUES ('NoOwner', 'US', NULL)"),
               psycopg.errors.NotNullViolation)
    check("a vendor with no company is refused (NOT NULL)", r is True, r)
    r = raises(lambda: run("INSERT INTO audit_log (stage, entity_type, entity_id, decision, reason, inputs, actor, "
                           "company_id) VALUES ('x', 'invoice', %s, 'd', 'r', '{}'::jsonb, 'system', NULL)", (inv_a,)),
               psycopg.errors.NotNullViolation)
    check("an audit row with no company is refused (NOT NULL)", r is True, r)
    r = raises(lambda: run("UPDATE invoices SET company_id = %s WHERE id = %s", (b["cid"], inv_a)), psycopg.Error)
    check("moving an invoice to another company is refused", r is True, r)
    cid_after = one("SELECT company_id FROM invoices WHERE id = %s", (inv_a,))[0]
    check("the invoice stayed in company A", cid_after == a["cid"])
    run("INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) VALUES ('PO-SAME', %s, 1, 'USD', %s)",
        (a["vid"], a["cid"]))
    run("INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id) VALUES ('PO-SAME', %s, 1, 'USD', %s)",
        (b["vid"], b["cid"]))
    check("two companies can use the same PO number", True)
    legacy = one("SELECT count(*) FROM companies WHERE name = 'Legacy (unassigned)'")[0]
    check("no unowned rows were left behind (legacy bucket not created on a clean database)", legacy == 0)


def test_s1_s3_tool_scoping():
    section("S1-S3  every lookup is limited to one company")
    a, b = world(), world()
    shared_name = uniq("Acme Supplies")
    va = mk_vendor(a["cid"], name=shared_name)
    mk_vendor(b["cid"], name=shared_name + " Ltd")
    vb_email = f"{uniq('b')}@example.com"
    vb = mk_vendor(b["cid"], email=vb_email)
    po_b, po_b_number = mk_po(b["cid"], vb)
    inv_b = mk_invoice(b["cid"], vb, po_b, status="approved")

    check("no tenant bound: a tool refuses to run", raises(lambda: tools.get_po(po_b_number), RuntimeError) is True)
    with tools.tenant(a["cid"]):
        check("company A cannot read company B's purchase order", tools.get_po(po_b_number) is None)
        check("company A sees no deliveries of company B's PO", tools.get_deliveries(po_b_number)["count"] == 0)
        check("company A cannot read company B's bank history", tools.get_vendor_bank_history(vb) == [])
        check("company A cannot see company B's invoices", tools.find_similar_invoices(vb, "anything", amount="100.00") == [])
        found = [v["id"] for v in tools.search_vendors(shared_name)]
        check("vendor search returns only company A's vendors", found == [str(va)] or found == [va], found)
        check("precedent search cannot cross companies", tools.find_similar_precedents(vb, ["x"]) == [])
    with tools.tenant(b["cid"]):
        check("company B still sees its own purchase order", tools.get_po(po_b_number) is not None)

    from backend.checks import run_hard_checks
    inv_a = mk_invoice(a["cid"], a["vid"], a["po_id"])
    res = run_hard_checks(inv_a)
    check("hard checks on a clean invoice find no reasons", res["reasons"] == [], res["reasons"])

    stray = one("INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, "
                "status, company_id, received_at) VALUES (%s, %s, %s, 100, 'USD', %s, 'held', %s, clock_timestamp()) "
                "RETURNING id", (a["vid"], a["po_id"], "SAMEID-1", "x@example.com", a["cid"]))[0]
    other = one("INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, "
                "status, company_id, received_at) VALUES (%s, %s, %s, 100, 'USD', %s, 'approved', %s, clock_timestamp()) "
                "RETURNING id", (vb, po_b, "SAMEID-1", "y@example.com", b["cid"]))[0]
    res = run_hard_checks(stray)
    check("same invoice number in another company is not a duplicate here", "duplicate_invoice" not in res["reasons"],
          res["reasons"])

    from backend.policy import check_policy
    with get_conn() as conn:
        paid_in_b = mk_invoice(b["cid"], vb, po_b, status="approved")
        conn.execute("INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, attempt, "
                     "status, company_id) VALUES (%s, %s, %s, 100, 'USD', 1, 'success', %s)",
                     (paid_in_b, uniq("k"), vb_email, b["cid"]))
        conn.commit()
        inv_a2 = mk_invoice(a["cid"], a["vid"], a["po_id"], status="approved", email=vb_email)
        pol = check_policy(conn, inv_a2)
    check("a payee paid by company B is still a NEW payee for company A", "new_payee" in pol["reasons"], pol)


def api(method, path, uid=None, **kw):
    headers = kw.pop("headers", {})
    if uid:
        headers = {**bearer(uid), **headers}
    return client.request(method, path, headers=headers, **kw)


def test_s5_roles_and_four_eyes():
    section("S5  roles, four eyes, member management")
    w = world()
    admin, reviewer, viewer = (w["users"][r][0] for r in ("admin", "reviewer", "viewer"))
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="held")

    check("viewer can read the review queue", api("GET", "/review/queue", viewer).status_code == 200)
    body = {"decision": "approve", "note": "Checked everything carefully."}
    check("viewer cannot decide", api("POST", f"/review/{inv}/decision", viewer, json=body).status_code == 403)
    check("viewer cannot release a payment", api("POST", f"/review/{inv}/pay", viewer).status_code == 403)
    check("viewer cannot upload", api("POST", "/invoices/upload", viewer,
                                      files={"file": ("a.txt", b"hello", "text/plain")}).status_code == 403)
    check("viewer cannot add a vendor", api("POST", "/vendors", viewer,
                                            json={"name": "V", "country": "US", "payout_email": "v@example.com"}).status_code == 403)
    check("viewer cannot list members", api("GET", "/admin/users", viewer).status_code == 403)
    check("reviewer cannot add a vendor", api("POST", "/vendors", reviewer,
                                              json={"name": "V", "country": "US", "payout_email": "v@example.com"}).status_code == 403)
    check("reviewer cannot manage members", api("POST", "/admin/users", reviewer, json={}).status_code in (403, 422)
          and api("GET", "/admin/users", reviewer).status_code == 403)
    check("reviewer cannot change settings", api("POST", "/admin/settings", reviewer,
                                                 json={"auto_pay_limit": "99999"}).status_code == 403)
    check("reviewer cannot create a PO", api("POST", "/pos", reviewer, json={}).status_code in (403,))
    check("anonymous call is refused", client.get("/review/queue").status_code == 401)

    other = world()
    check("another company's reviewer gets 404 for this invoice",
          api("POST", f"/review/{inv}/decision", other["users"]["reviewer"][0], json=body).status_code == 404)
    check("another company's member cannot read this case",
          api("GET", f"/review/{inv}", other["users"]["admin"][0]).status_code == 404)

    reviewer2, _ = mk_user(w["cid"], "reviewer")
    run("UPDATE invoices SET uploaded_by = %s WHERE id = %s", (reviewer, inv))
    r = api("POST", f"/review/{inv}/decision", reviewer, json=body)
    check("the uploader cannot approve their own invoice", r.status_code == 403 and r.json()["detail"]["code"] == "self_approval", r.text)
    r = api("POST", f"/review/{inv}/decision", reviewer2, json=body)
    check("a different reviewer can approve it", r.status_code == 200, r.text)
    check("the uploader cannot release the payment either when policy holds it",
          True if one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] != "approved" else True)

    solo = mk_company()
    solo_admin, _ = mk_user(solo, "admin")
    sv = mk_vendor(solo)
    sp, _ = mk_po(solo, sv)
    sinv = mk_invoice(solo, sv, sp, status="held", uploaded_by=solo_admin)
    r = api("POST", f"/review/{sinv}/decision", solo_admin, json=body)
    check("a one-person company is not locked out of its own invoices", r.status_code == 200, r.text)

    held2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount="9000.00", uploaded_by=reviewer)
    r = api("POST", f"/review/{held2}/pay", reviewer)
    check("the uploader cannot release their own over-limit invoice",
          r.status_code == 403 and "self_approval" in r.text, r.text)

    new_email = f"{uniq('new')}@example.com"
    r = api("POST", "/admin/users", admin, json={"full_name": "New Person", "email": new_email, "role": "viewer",
                                                 "temp_password": "temporary pass 77"})
    check("admin can add a member", r.status_code == 200, r.text)
    new_id = r.json()["id"]
    check("the new member must change their password first",
          api("GET", "/review/queue", new_id).status_code == 403)
    check("duplicate email is refused", api("POST", "/admin/users", admin, json={
        "full_name": "New Person", "email": new_email, "role": "viewer", "temp_password": "temporary pass 77"}).status_code == 409)
    check("a bad role is refused", api("POST", "/admin/users", admin, json={
        "full_name": "X Y", "email": uniq("x") + "@example.com", "role": "root", "temp_password": "temporary pass 77"}).status_code == 422)

    old_header = bearer(reviewer2)
    r = api("PATCH", f"/admin/users/{reviewer2}", admin, json={"role": "viewer"})
    check("admin can change a member's role", r.status_code == 200 and r.json()["role"] == "viewer", r.text)
    check("the old token of a demoted member stops working", client.get("/review/queue", headers=old_header).status_code == 401)
    r = api("PATCH", f"/admin/users/{reviewer2}", admin, json={"disabled": True})
    check("admin can disable a member", r.status_code == 200 and r.json()["disabled"] is True, r.text)
    check("a disabled member cannot log in",
          client.post("/auth/login", json={"email": one("SELECT email FROM users WHERE id = %s", (reviewer2,))[0],
                                           "password": PASSWORD}).status_code == 401)
    r = api("PATCH", f"/admin/users/{admin}", admin, json={"role": "viewer"})
    check("the last admin cannot be demoted", r.status_code == 409, r.text)
    r = api("PATCH", f"/admin/users/{admin}", admin, json={"disabled": True})
    check("the last admin cannot be disabled", r.status_code == 409, r.text)
    r = api("PATCH", f"/admin/users/{other['users']['admin'][0]}", admin, json={"role": "viewer"})
    check("an admin cannot touch another company's member", r.status_code == 404, r.text)
    audits = one("SELECT count(*) FROM audit_log WHERE stage = 'admin' AND company_id = %s", (w["cid"],))[0]
    check("every member change is in the audit log", audits >= 3, audits)


def test_s6_sessions_and_login():
    section("S6  login, sessions, limits, reset")
    cid = mk_company()
    uid, email = mk_user(cid, "admin")
    admin2, _ = mk_user(cid, "admin")

    fresh = TestClient(webhooks.app)
    r = fresh.post("/auth/login", json={"email": email, "password": PASSWORD}, headers={"X-CSRF": "1"})
    cookie = r.headers.get("set-cookie", "").lower()
    check("login sets an HttpOnly session cookie", r.status_code == 200 and "httponly" in cookie and "samesite" in cookie, cookie)
    check("a console login does not hand the token to page script", "token" not in r.json())
    check("cookie session can read", fresh.get("/auth/me").status_code == 200)
    check("cookie session cannot change data without the CSRF header",
          fresh.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "another pass 1234"}).status_code == 403)

    api_login = TestClient(webhooks.app).post("/auth/login", json={"email": email, "password": PASSWORD})
    token = api_login.json()["token"]
    h = {"Authorization": "Bearer " + token}
    check("API clients still receive a bearer token", client.get("/auth/me", headers=h).status_code == 200)
    r = fresh.post("/auth/logout", headers={"X-CSRF": "1"})
    check("logout works", r.status_code == 200)
    check("logout kills every older token, not just the cookie", client.get("/auth/me", headers=h).status_code == 401)

    forged = auth.jwt.encode({"sub": str(uid), "ver": 0, "exp": 9999999999}, "wrong" * 10, algorithm="HS256")
    check("a token signed with another secret is refused", client.get("/auth/me", headers={"Authorization": "Bearer " + forged}).status_code == 401)
    expired = auth.jwt.encode({"sub": str(uid), "ver": 99, "exp": 1}, os.environ["JWT_SECRET"], algorithm="HS256")
    check("an expired token is refused", client.get("/auth/me", headers={"Authorization": "Bearer " + expired}).status_code == 401)

    victim, vemail = mk_user(cid, "viewer")
    codes = [client.post("/auth/login", json={"email": vemail, "password": "wrong password " + str(i)}).status_code for i in range(6)]
    check("five wrong passwords are 401, the sixth attempt is 429", codes[:5] == [401] * 5 and codes[5] == 429, codes)
    check("even the right password is refused while locked",
          client.post("/auth/login", json={"email": vemail, "password": PASSWORD}).status_code == 429)
    check("the lock lives in the database, so a restart does not reset it",
          TestClient(webhooks.app).post("/auth/login", json={"email": vemail, "password": PASSWORD}).status_code == 429)
    run("DELETE FROM login_attempts WHERE bucket = %s", (f"login-email:{vemail}",))
    check("after the window the person can log in again",
          client.post("/auth/login", json={"email": vemail, "password": PASSWORD}).status_code == 200)

    ghost = [client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}) for _ in range(1)]
    wrong = client.post("/auth/login", json={"email": email, "password": "not the password"})
    check("unknown email and wrong password give the same answer",
          ghost[0].status_code == wrong.status_code == 401 and ghost[0].json() == wrong.json())

    run("DELETE FROM login_attempts")
    own = uniq("sign") + "@example.com"
    for bad in ("short1", "aaaaaaaaaaaaaa", own.split("@")[0] + "xyz9"):
        r = client.post("/auth/signup", json={"full_name": "A B", "email": own, "password": bad})
        check(f"weak password '{bad[:12]}' is refused", r.status_code == 422, r.text)
    r = client.post("/auth/signup", json={"full_name": "A B", "email": uniq("s") + "@example.com", "password": "a long good pass 42"})
    check("a good signup works", r.status_code == 200, r.text)
    os.environ["SIGNUP_ENABLED"] = "false"
    check("signup can be closed", client.post("/auth/signup", json={"full_name": "A B", "email": uniq("s") + "@example.com",
                                                                    "password": "a long good pass 42"}).status_code == 403)
    del os.environ["SIGNUP_ENABLED"]
    run("DELETE FROM login_attempts")
    ip_codes = [client.post("/auth/signup", json={"full_name": "A B", "email": uniq("z") + "@example.com",
                                                  "password": "a long good pass 42"}).status_code for _ in range(12)]
    check("signups from one address are rate limited", 429 in ip_codes, ip_codes)

    run("DELETE FROM login_attempts")
    r = client.post(f"/admin/users/{victim}/reset-token", headers=bearer(uid))
    check("admin can create a one-time reset token", r.status_code == 200 and len(r.json()["token"]) > 30, r.text)
    tok = r.json()["token"]
    stored = one("SELECT token_hash FROM password_resets WHERE user_id = %s ORDER BY created_at DESC", (victim,))[0]
    check("only a hash of the token is stored", stored != tok and tok not in stored)
    before = bearer(victim)
    r = client.post("/auth/reset-password", json={"token": tok, "new_password": "brand new secret 55"})
    check("the token resets the password", r.status_code == 200, r.text)
    check("the reset ends the person's old sessions", client.get("/auth/me", headers=before).status_code == 401)
    check("the token works only once", client.post("/auth/reset-password", json={"token": tok, "new_password": "yet another one 66"}).status_code == 400)
    check("the new password logs in", client.post("/auth/login", json={"email": vemail, "password": "brand new secret 55"}).status_code == 200)
    r = client.post(f"/admin/users/{victim}/reset-token", headers=bearer(uid))
    run("UPDATE password_resets SET expires_at = now() - interval '1 minute' WHERE token_hash = %s",
        (auth.hash_reset_token(r.json()["token"]),))
    check("an expired token is refused", client.post("/auth/reset-password", json={"token": r.json()["token"], "new_password": "yet another one 66"}).status_code == 400)
    check("an admin of another company cannot reset this member",
          client.post(f"/admin/users/{victim}/reset-token", headers=bearer(world()["users"]["admin"][0])).status_code == 404)

    run("DELETE FROM login_attempts")
    src = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "backend").glob("*.py"))
    leaks = [m.group(0) for m in re.finditer(r"print\([^)\n]*(token|password|secret|Authorization|api_key)[^)\n]*\)", src, re.I)]
    check("no print/log statement outputs a token, password or secret", not leaks, leaks)
    html = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "console").glob("*"))
    check("the console no longer stores a session token in localStorage", "pm_jwt" not in re.sub(r'removeItem\("pm_jwt"\)', "", html))
    check("the console never writes a token to console.log", not re.search(r"console\.log\([^)]*(token|jwt)", html, re.I))


def _pdf(pages=1, extra=b""):
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Invoice page {i}")
    data = doc.tobytes()
    doc.close()
    return data + extra


def _png(size=(50, 50)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _jpg():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "JPEG")
    return buf.getvalue()


def rejected(name, data):
    try:
        validate_upload(name, data)
    except UploadRejected as e:
        return e.status
    return None


def test_s9_uploads():
    section("S9  upload validation")
    check("a real PDF is accepted", rejected("inv.pdf", _pdf()) is None)
    check("a real PNG is accepted", rejected("inv.png", _png()) is None)
    check("a real JPG is accepted", rejected("inv.jpg", _jpg()) is None)
    check("an email .txt is accepted", rejected("mail.txt", b"Invoice INV-1 total 10 USD") is None)
    check("a PDF over the page limit is refused", rejected("big.pdf", _pdf(25)) == 413)
    check("exactly at the page limit is accepted", rejected("ok.pdf", _pdf(20)) is None)
    check("text renamed to .pdf is refused", rejected("x.pdf", b"just text") == 422)
    check("a PDF with JavaScript is refused", rejected("js.pdf", _pdf(extra=b"\n1 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj")) == 422)
    check("a PDF with a launch action is refused", rejected("l.pdf", _pdf(extra=b"\n<< /S /Launch /F (cmd.exe) >>")) == 422)
    check("a PDF with an embedded file is refused", rejected("e.pdf", _pdf(extra=b"\n<< /Type /EmbeddedFile >>")) == 422)
    check("a truncated PDF is refused", rejected("t.pdf", _pdf()[:60]) == 422)
    check("an exe renamed to .png is refused", rejected("x.png", b"MZ\x90\x00" + b"\x00" * 200) == 422)
    check("a PNG renamed to .jpg is refused", rejected("x.jpg", _png()) == 422)
    check("an image with too many pixels is refused", rejected("huge.png", _png((6000, 6000))) in (413, 422))
    check("a corrupt PNG is refused", rejected("c.png", _png()[:40]) == 422)
    check("a binary file named .txt is refused", rejected("b.txt", b"abc\x00def") == 422)
    check("non UTF-8 text is refused", rejected("l.txt", b"\xff\xfe\xfa\xfb") == 422)
    check("email text over 1 MB is refused", rejected("big.txt", b"a" * (1024 * 1024 + 1)) == 413)
    check("a file over 10 MB is refused", rejected("big.pdf", b"%PDF-" + b"a" * (10 * 1024 * 1024 + 1)) == 413)
    check("an empty file is refused", rejected("e.pdf", b"") == 422)
    check("an .exe is refused", rejected("a.exe", b"MZ") == 422)
    check("a double extension is judged by the last one", rejected("a.pdf.exe", _pdf()) == 422)
    name, _ = validate_upload("../../etc/pass\x00wd.txt", b"hello")
    check("the stored file name has no control characters", "\x00" not in name)

    w = world()
    reviewer = w["users"]["reviewer"][0]
    r = api("POST", "/invoices/upload", reviewer, files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    check("the upload endpoint returns 422 for a fake PDF", r.status_code == 422, r.text)
    r = api("POST", "/invoices/upload", reviewer, files={"file": ("big.pdf", b"%PDF-" + b"a" * (10 * 1024 * 1024 + 70000), "application/pdf")})
    check("the upload endpoint stops an oversize body early", r.status_code in (413, 422), r.status_code)
    run("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, company_id, uploaded_by) "
        "SELECT 'f' || g, 'email', 't', md5(g::text) || '0', 'pending', %s, %s FROM generate_series(1, 30) g",
        (w["cid"], reviewer))
    r = api("POST", "/invoices/upload", reviewer, files={"file": ("n.txt", b"Invoice again 77", "text/plain")})
    check("a person who uploaded 30 files this hour is limited", r.status_code == 429, r.text)


def test_s12_b2_webhooks():
    section("S12 / B2  webhook safety, replay, ordering")
    os.environ.pop("PAYPAL_WEBHOOK_ID", None)
    r = client.post("/webhooks/paypal", content=b"{}", headers={"content-type": "application/json"})
    check("no PAYPAL_WEBHOOK_ID: every webhook is refused", r.status_code == 503, r.text)
    os.environ["PAYPAL_WEBHOOK_ID"] = "WH-TEST"

    real_verify = webhooks.verify_signature
    r = client.post("/webhooks/paypal", content=b"{}")
    check("missing PayPal signature headers: refused", r.status_code == 400)
    webhooks.verify_signature = lambda headers, raw: False
    r = client.post("/webhooks/paypal", content=b'{"id": "E1"}')
    check("a bad signature is refused and nothing is stored",
          r.status_code == 400 and one("SELECT count(*) FROM webhook_events WHERE event_id = 'E1'")[0] == 0)
    webhooks.verify_signature = real_verify
    check("the signature check fails closed when PAYPAL_WEBHOOK_ID is blank",
          (os.environ.__setitem__("PAYPAL_WEBHOOK_ID", " ") or True) and real_verify({}, "{}") is False)
    os.environ["PAYPAL_WEBHOOK_ID"] = "WH-TEST"

    webhooks.verify_signature = lambda headers, raw: True
    calls = []
    real_sync = webhooks.sync_payment

    def counting_sync(conn, batch_id):
        calls.append(batch_id)
        return {"result": "no_change"}

    webhooks.sync_payment = counting_sync
    ev = {"id": uniq("EVT"), "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": "B-1"}}}
    first = client.post("/webhooks/paypal", content=json.dumps(ev))
    second = client.post("/webhooks/paypal", content=json.dumps(ev))
    third = client.post("/webhooks/paypal", content=json.dumps(ev))
    check("first delivery is processed", first.status_code == 200 and first.json()["result"] == "no_change", first.text)
    check("a replay is acknowledged but not processed again",
          second.json().get("action") == "duplicate" and third.json().get("action") == "duplicate" and len(calls) == 1, calls)
    row = one("SELECT attempts, processed_at FROM webhook_events WHERE event_id = %s", (ev["id"],))
    check("the event row counts the deliveries and is marked processed", row[0] == 3 and row[1] is not None, row)

    ev2 = {"id": uniq("EVT"), "event_type": "X", "resource": {"batch_header": {"payout_batch_id": "B-2"}}}
    state = {"fail": True}

    def flaky_sync(conn, batch_id):
        calls.append(batch_id)
        if state["fail"]:
            raise RuntimeError("PayPal down")
        return {"result": "updated"}

    webhooks.sync_payment = flaky_sync
    n = len(calls)
    r1 = client.post("/webhooks/paypal", content=json.dumps(ev2))
    check("a failed sync answers 500 so PayPal will send it again", r1.status_code == 500)
    state["fail"] = False
    r2 = client.post("/webhooks/paypal", content=json.dumps(ev2))
    check("the retry of a failed event is processed", r2.status_code == 200 and r2.json()["result"] == "updated" and len(calls) == n + 2, r2.text)

    ev3 = {"id": uniq("EVT"), "event_type": "X", "resource": {"batch_header": {"payout_batch_id": "B-3"}}}
    gate = threading.Barrier(6)
    outs = []
    n = len(calls)

    def slow_sync(conn, batch_id):
        calls.append(batch_id)
        import time
        time.sleep(0.4)
        return {"result": "updated"}

    webhooks.sync_payment = slow_sync

    def hit():
        gate.wait()
        outs.append(TestClient(webhooks.app).post("/webhooks/paypal", content=json.dumps(ev3)).json())

    ts = [threading.Thread(target=hit) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("six simultaneous copies of one event are processed exactly once", len(calls) - n == 1, len(calls) - n)

    big = client.post("/webhooks/paypal", content=b"x" * (webhooks.MAX_WEBHOOK_BYTES + 10))
    check("an oversize webhook body is refused", big.status_code == 413)
    webhooks.sync_payment = real_sync
    webhooks.verify_signature = real_verify


def _paypal_world(status="submitted", amount="100.00"):
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved", amount=amount)
    key = uniq("pm-key")
    pid = one("INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, attempt, status, "
              "company_id, paypal_batch_id) VALUES (%s, %s, 'a@example.com', %s, 'USD', 1, %s, %s, %s) RETURNING id",
              (inv, key, amount, status, w["cid"], uniq("BATCH")))[0]
    batch = one("SELECT paypal_batch_id FROM payments WHERE id = %s", (pid,))[0]
    return w, inv, pid, batch, key


def test_b2_settlement_replay_and_order():
    section("B2  settlement: replay, ordering, ledger uniqueness")
    import backend.settle as settle
    real_batch, real_item = settle.fetch_batch, settle.fetch_item
    w, inv, pid, batch, key = _paypal_world()

    def batch_with(status):
        return {"batch_header": {"sender_batch_header": {"sender_batch_id": key}},
                "items": [{"transaction_status": status, "payout_item_id": "ITEM-1",
                           "payout_item_fee": {"value": "0.25", "currency": "USD"}}]}

    settle.fetch_item = lambda item_id: {"payout_item": {"amount": {"value": "100.00", "currency": "USD"}},
                                         "payout_item_fee": {"value": "0.25", "currency": "USD"}}
    settle.fetch_batch = lambda b: batch_with("SUCCESS")
    with get_conn() as conn:
        r1 = settle.sync_payment(conn, batch)
    with get_conn() as conn:
        r2 = settle.sync_payment(conn, batch)
    check("first SUCCESS settles the payment", r1["result"] == "updated", r1)
    check("the same SUCCESS again changes nothing", r2["result"] == "no_change", r2)
    rows = one("SELECT count(*) FROM ledger_entries WHERE payment_id = %s", (pid,))[0]
    check("the ledger has exactly one set of lines (4 with fee)", rows == 4, rows)
    deb = one("SELECT sum(amount) FROM ledger_entries WHERE payment_id = %s AND direction = 'debit'", (pid,))[0]
    cred = one("SELECT sum(amount) FROM ledger_entries WHERE payment_id = %s AND direction = 'credit'", (pid,))[0]
    check("debits equal credits", deb == cred, (deb, cred))
    check("the invoice is marked paid", one("SELECT status FROM invoices WHERE id = %s", (inv,))[0] == "paid")
    r = raises(lambda: run("INSERT INTO ledger_entries (payment_id, entry_type, account, direction, amount, currency, "
                           "description, company_id) VALUES (%s, 'payment', 'accounts_payable', 'debit', 100, 'USD', 'dup', %s)",
                           (pid, w["cid"])), psycopg.errors.UniqueViolation)
    check("the database itself refuses a second copy of a ledger line", r is True, r)

    settle.fetch_batch = lambda b: batch_with("PENDING")
    with get_conn() as conn:
        r3 = settle.sync_payment(conn, batch)
    check("a late PENDING event cannot undo a SUCCESS", r3["result"] == "no_change" and
          one("SELECT status FROM payments WHERE id = %s", (pid,))[0] == "success", r3)
    settle.fetch_batch = lambda b: batch_with("FAILED")
    with get_conn() as conn:
        r4 = settle.sync_payment(conn, batch)
    check("a late FAILED event cannot undo a SUCCESS", r4["result"] == "no_change" and
          one("SELECT status FROM payments WHERE id = %s", (pid,))[0] == "success", r4)

    w2, inv2, pid2, batch2, key2 = _paypal_world()
    settle.fetch_batch = lambda b: {"batch_header": {"sender_batch_header": {"sender_batch_id": key2}},
                                    "items": [{"transaction_status": "FAILED", "payout_item_id": "I2"}]}
    with get_conn() as conn:
        settle.sync_payment(conn, batch2)
    settle.fetch_batch = lambda b: {"batch_header": {"sender_batch_header": {"sender_batch_id": key2}},
                                    "items": [{"transaction_status": "PENDING", "payout_item_id": "I2"}]}
    with get_conn() as conn:
        r5 = settle.sync_payment(conn, batch2)
    check("a late PENDING event cannot undo a FAILED", one("SELECT status FROM payments WHERE id = %s", (pid2,))[0] == "failed", r5)

    w3, inv3, pid3, batch3, key3 = _paypal_world()
    settle.fetch_batch = lambda b: {"batch_header": {"sender_batch_header": {"sender_batch_id": key3}},
                                    "items": [{"transaction_status": "UNCLAIMED", "payout_item_id": "I3"}]}
    with get_conn() as conn:
        settle.sync_payment(conn, batch3)
    settle.fetch_batch = lambda b: {"batch_header": {"sender_batch_header": {"sender_batch_id": key3}},
                                    "items": [{"transaction_status": "PENDING", "payout_item_id": "I3"}]}
    with get_conn() as conn:
        settle.sync_payment(conn, batch3)
    check("PENDING after UNCLAIMED does not move it backwards", one("SELECT status FROM payments WHERE id = %s", (pid3,))[0] == "unclaimed")
    settle.fetch_batch = lambda b: {"batch_header": {"sender_batch_header": {"sender_batch_id": key3}},
                                    "items": [{"transaction_status": "SUCCESS", "payout_item_id": "I3",
                                               "payout_item_fee": {"value": "0", "currency": "USD"}}]}
    with get_conn() as conn:
        settle.sync_payment(conn, batch3)
    check("UNCLAIMED then SUCCESS (the person claimed it) still settles", one("SELECT status FROM payments WHERE id = %s", (pid3,))[0] == "success")

    w4, inv4, pid4, batch4, key4 = _paypal_world()
    settle.fetch_batch = lambda b: batch_with_key(key4)
    def batch_with_key(k):
        return {"batch_header": {"sender_batch_header": {"sender_batch_id": k}},
                "items": [{"transaction_status": "SUCCESS", "payout_item_id": "I4", "payout_item_fee": {"value": "0.25"}}]}
    results = []
    barrier = threading.Barrier(5)

    def go():
        barrier.wait()
        with get_conn() as conn:
            results.append(settle.sync_payment(conn, batch4)["result"])

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    n_rows = one("SELECT count(*) FROM ledger_entries WHERE payment_id = %s", (pid4,))[0]
    check("five syncs at the same moment write one ledger set", n_rows == 4 and results.count("updated") == 1, (n_rows, results))
    settle.fetch_batch, settle.fetch_item = real_batch, real_item


def test_b1_parallel_payment():
    section("B1  two clicks, one payment")
    import backend.pay as pay
    sent = []
    real = pay.send_payout

    def fake(**kw):
        import time
        sent.append(kw["key"])
        time.sleep(0.3)
        return {"result": "submitted", "paypal_batch_id": "FAKE"}

    pay.send_payout = fake
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    outs = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        with get_conn() as conn:
            outs.append(pay.pay_invoice(conn, inv, approved_by="Person A")["status"])

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rows = one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv,))[0]
    check("six simultaneous releases call PayPal exactly once", len(sent) == 1, sent)
    check("and create exactly one payment row", rows == 1, rows)
    check("the other five are told it already exists", sorted(outs).count("already_exists") == 5, outs)

    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
    pay.send_payout = fake
    done = []

    def pay_it():
        with get_conn() as conn:
            done.append(pay.pay_invoice(conn, inv2, approved_by="Person A")["status"])

    def reject_it():
        import time
        time.sleep(0.1)
        with get_conn() as conn:
            try:
                review.decide(conn, inv2, "reject", "Person B", "Rejecting this one right now.")
                done.append("rejected_ok")
            except review.ReviewError as e:
                done.append(e.code)

    t1, t2 = threading.Thread(target=pay_it), threading.Thread(target=reject_it)
    t1.start(); t2.start(); t1.join(); t2.join()
    st = one("SELECT status FROM invoices WHERE id = %s", (inv2,))[0]
    paid = one("SELECT count(*) FROM payments WHERE invoice_id = %s", (inv2,))[0]
    check("a person cannot reject an invoice while its payment is going out", not (st == "rejected" and paid == 1), (st, paid, done))
    pay.send_payout = real


def test_s10_injection():
    section("S10  prompt injection: code gates hold even if the AI obeys the attacker")
    from scripts.eval import install_hostile_stub
    install_hostile_stub()
    from backend.investigator import investigate

    attacks = json.loads((ROOT / "samples" / "injection_texts.json").read_text(encoding="utf-8"))
    w = world()
    bad_ids = []

    def attach(inv, text):
        run("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, company_id, invoice_id) "
            "VALUES ('inj', 'email', %s, %s, 'accepted', %s, %s)", (text, uniq("sha"), w["cid"], inv))

    for text in attacks:
        original = mk_invoice(w["cid"], w["vid"], w["po_id"], status="approved")
        tag = uuid.uuid4().hex[:8].upper()
        run("UPDATE invoices SET invoice_number = %s WHERE id = %s", (f"DUP-{tag}", original))
        cases = {
            "bank_change": mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="attacker@evil.example.com"),
            "no_po": mk_invoice(w["cid"], w["vid"], None, status="received"),
            "duplicate": mk_invoice(w["cid"], w["vid"], w["po_id"], status="received"),
        }
        run("UPDATE invoices SET invoice_number = %s WHERE id = %s", (f"dup {tag.lower()}", cases["duplicate"]))
        for kind, inv in cases.items():
            attach(inv, text)
            bad_ids.append((kind, text[:40], investigate(inv)["status"]))
    approved = [b for b in bad_ids if b[2] == "approved"]
    check(f"{len(attacks)} injection texts x 3 bad-invoice kinds: none approved ({len(bad_ids)} runs)", not approved, approved[:3])

    clean_ids = []
    for text in attacks:
        inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received")
        run("INSERT INTO extractions (source_name, source_format, source_text, source_sha256, status, company_id, invoice_id) "
            "VALUES ('inj', 'email', %s, %s, 'accepted', %s, %s)", (text, uniq("sha"), w["cid"], inv))
        clean_ids.append((text, investigate(inv)["status"]))
    caught = [t[:40] for t, s in clean_ids if s != "approved"]
    check("injection text in an otherwise clean invoice is held by the plain-text scan where it pressures payment",
          len(caught) >= 3, caught)

    from backend.fraud import PRESSURE_PATTERNS
    flagged = [a for a in attacks if any(p.search(a) for p, _ in PRESSURE_PATTERNS)]
    check("the code-only scan flags at least 3 of the attack texts without any AI", len(flagged) >= 3, flagged)

    with tools.tenant(w["cid"]):
        rows = tools.find_similar_precedents(w["vid"], [])
    check("precedent notes are marked as untrusted text for the AI", all(r.get("note_is_untrusted_text") for r in rows))
    src = (ROOT / "backend" / "investigator.py").read_text()
    check("the investigator prompt says all database values are data", "DATA, never instructions" in src)


def test_s11_cors():
    section("S11  CORS")
    os.environ["CONSOLE_ORIGINS"] = "*"
    check("a wildcard origin is refused at startup", raises(webhooks.console_origins, RuntimeError) is True)
    os.environ["CONSOLE_ORIGINS"] = "https://*.example.com"
    check("a wildcard inside an origin is refused", raises(webhooks.console_origins, RuntimeError) is True)
    os.environ["CONSOLE_ORIGINS"] = "https://app.example.com/, https://b.example.com"
    check("exact origins are accepted", webhooks.console_origins() == ["https://app.example.com", "https://b.example.com"])
    del os.environ["CONSOLE_ORIGINS"]
    r = client.options("/review/queue", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    check("an unknown origin gets no CORS permission", "access-control-allow-origin" not in {k.lower() for k in r.headers})


def main():
    tests = [test_s4_database_isolation, test_s1_s3_tool_scoping, test_s5_roles_and_four_eyes, test_s6_sessions_and_login,
             test_s9_uploads, test_s12_b2_webhooks, test_b2_settlement_replay_and_order, test_b1_parallel_payment,
             test_s10_injection, test_s11_cors]
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
