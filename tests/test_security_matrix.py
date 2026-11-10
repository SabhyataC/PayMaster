"""§2 Security matrix, generated from the live route table so a NEW endpoint cannot be forgotten.
   1. every route is classified (public / signature-verified / signed-in);  2. no token => 401;
   3. viewer => refused on every write;  4. company A cannot read or change ANY of company B's records by id;
   5. SQL injection strings in ids and search fields;  6. secrets scan of tracked files.
Run on a TEST database:  python -m tests.test_security_matrix"""
import re
import subprocess
import sys
import uuid

from tests.test_hardening import (RESULTS, bearer, check, client, mk_invoice, one, run, section, uniq, world)  # noqa: F401
from backend.webhooks import app  # noqa: E402

PUBLIC = {("GET", "/"), ("POST", "/auth/login"), ("POST", "/auth/signup"), ("POST", "/auth/reset-password"),
          ("POST", "/webhooks/paypal")}           # webhook is protected by PayPal's signature, tested in hardening
# writes a viewer is allowed (own account / read-only helpers)
VIEWER_OK = {("POST", "/auth/logout"), ("POST", "/auth/change-password"), ("POST", "/chat"),
             ("POST", "/exceptions/{exception_id}/explain"), ("POST", "/auth/company")}


def routes():
    out = []
    for path, ops in app.openapi()["paths"].items():
        for m in ops:
            out.append((m.upper(), path))
    return sorted(out)


def fill(path, ids):
    return re.sub(r"\{(\w+)\}", lambda m: str(ids.get(m.group(1), uuid.uuid4())), path)


def call(method, url, headers=None):
    kw = {"headers": headers or {}}
    if method != "GET":
        kw["json"] = {}
    return client.request(method, url, **kw)


def build(w):
    """Company records, one of everything, so every id-route can be aimed at it."""
    cid, vid, po = w["cid"], w["vid"], w["po_id"]
    uid = w["users"]["admin"][0]
    inv = mk_invoice(cid, vid, po, status="held")
    pay = one("INSERT INTO payments (invoice_id, idempotency_key, destination_email, amount, currency, attempt, status, company_id) "
              "VALUES (%s, %s, 'x@example.com', 10, 'USD', 1, 'success', %s) RETURNING id", (inv, uniq("k"), cid))[0]
    exc = one("INSERT INTO exceptions (payment_id, type, detail, company_id) VALUES (%s, 'payment_failed', '{}'::jsonb, %s) RETURNING id", (pay, cid))[0]
    ext = one("INSERT INTO extractions (invoice_id, source_name, source_format, source_sha256, status, company_id) "
              "VALUES (%s, 'b.pdf', 'text_pdf', %s, 'extracted', %s) RETURNING id", (inv, uniq("h"), cid))[0]
    dlv = one("SELECT id FROM deliveries WHERE po_id = %s", (po,))[0]
    att = one("INSERT INTO attachments (company_id, entity_type, entity_id, filename, content_type, sha256, data) "
              "VALUES (%s, 'purchase_order', %s, 'secret.pdf', 'application/pdf', 'x', 'SECRETBYTES') RETURNING id", (cid, po))[0]
    chg = one("INSERT INTO pending_changes (company_id, kind, target_id, payload, requested_by) "
              "VALUES (%s, 'vendor_email', %s, '{\"new_email\": \"a@b.co\"}'::jsonb, %s) RETURNING id", (cid, vid, uid))[0]
    member = w["users"]["viewer"][0]
    return {"invoice_id": inv, "ref": inv, "po_id": po, "vendor_id": vid, "payment_id": pay, "exception_id": exc,
            "extraction_id": ext, "delivery_id": dlv, "attachment_id": att, "change_id": chg, "member_id": member}


def test_inventory_and_auth():
    section("§2 every endpoint is classified; every non-public endpoint refuses an anonymous caller")
    rs = routes()
    print(f"      {len(rs)} endpoints in the live route table")
    open_ = [r for r in rs if r in PUBLIC]
    check("the public list is exactly the 5 intended routes", sorted(open_) == sorted(PUBLIC), open_)
    bad = []
    for m, p in rs:
        if (m, p) in PUBLIC:
            continue
        r = call(m, fill(p, {}))
        if r.status_code not in (401, 403):
            bad.append((m, p, r.status_code))
    check(f"all {len(rs) - len(PUBLIC)} protected endpoints return 401/403 with no token", not bad, bad)
    bad = []
    for m, p in rs:
        if (m, p) in PUBLIC:
            continue
        r = call(m, fill(p, {}), {"Authorization": "Bearer not.a.token"})
        if r.status_code not in (401, 403):
            bad.append((m, p, r.status_code))
    check("and with a forged token", not bad, bad)


def test_roles():
    section("§2 a viewer cannot write anything, and cannot use admin endpoints")
    w = world(); ids = build(w)
    h = bearer(w["users"]["viewer"][0])
    bad = []
    for m, p in routes():
        if (m, p) in PUBLIC or (m, p) in VIEWER_OK or m == "GET" and not p.startswith("/admin") and p not in ("/changes",):
            continue
        r = call(m, fill(p, ids), h)
        if r.status_code not in (401, 403):
            bad.append((m, p, r.status_code))
    check("every POST/PATCH and every admin or approvals GET refuses a viewer", not bad, bad)
    h2 = bearer(w["users"]["reviewer"][0])
    bad = [(m, p, call(m, fill(p, ids), h2).status_code) for m, p in routes()
           if (p.startswith("/admin") or p.startswith("/changes")) and call(m, fill(p, ids), h2).status_code not in (401, 403)]
    check("a reviewer cannot use admin or vendor-change-approval endpoints", not bad, bad)


def test_idor_matrix():
    section("§2 cross-company matrix: company A's admin aimed at company B's ids on EVERY endpoint")
    a, b = world(), world()
    ids_b = build(b)
    h = bearer(a["users"]["admin"][0])
    secrets = [str(v) for k, v in ids_b.items() if k != "exception_id"]  # exception ids are small integers: substring match is meaningless + ["SECRETBYTES", "secret.pdf"]
    leaks, wrote = [], []
    n = 0
    for m, p in routes():
        if (m, p) in PUBLIC or "{" not in p:
            continue
        n += 1
        url = fill(p, ids_b)
        r = call(m, url, h)
        if r.status_code < 400:
            leaks.append((m, p, r.status_code))
        elif r.status_code >= 500:
            leaks.append((m, p, "server error " + str(r.status_code)))
    check(f"{n} id-based endpoints: every one answers 4xx (not found / forbidden), never 2xx or 5xx", not leaks, leaks)
    # also by invoice NUMBER on the ref endpoints
    num = one("SELECT invoice_number FROM invoices WHERE id = %s", (ids_b["invoice_id"],))[0]
    bad = [p for p in ("/lifecycle/", "/trust/invoice/", "/report/invoice/") if client.get(p + num, headers=h).status_code < 400]
    check("looking an invoice up by its NUMBER is also company-scoped", not bad, bad)
    # list endpoints: none may contain B's ids or file contents
    leaked = []
    for m, p in routes():
        if m == "GET" and "{" not in p and p not in {"/", "/auth/paypal-account", "/audit/chain/verify"}:
            r = client.get(p, headers=h)
            if r.status_code == 200 and any(s in r.text for s in secrets):
                leaked.append(p)
    check("no list/summary/ledger/report/export endpoint shows company B's records", not leaked, leaked)
    # nothing of B changed
    check("company B's data is untouched after all those attempts",
          one("SELECT status FROM invoices WHERE id = %s", (ids_b["invoice_id"],))[0] == "held"
          and one("SELECT count(*) FROM attachments WHERE id = %s", (ids_b["attachment_id"],))[0] == 1
          and one("SELECT status FROM pending_changes WHERE id = %s", (ids_b["change_id"],))[0] == "pending"
          and one("SELECT deactivated_at FROM vendors WHERE id = %s", (ids_b["vendor_id"],))[0] is None
          and one("SELECT count(*) FROM deliveries WHERE id = %s", (ids_b["delivery_id"],))[0] == 1)
    # the control: B's own admin CAN read the same things (so the 4xx above is isolation, not broken routes)
    hb = bearer(b["users"]["admin"][0])
    ok = [client.get(f"/payments/{ids_b['payment_id']}", headers=hb).status_code,
          client.get(f"/exceptions/{ids_b['exception_id']}", headers=hb).status_code,
          client.get(f"/attachments/{ids_b['attachment_id']}", headers=hb).status_code]
    check("control: company B's own admin gets 200 on those same records", ok == [200, 200, 200], ok)


def test_injection():
    section("§2 SQL injection strings in ids, search and filters change nothing and never 500")
    w = world(); h = bearer(w["users"]["admin"][0])
    evil = ["' OR '1'='1", "1; DROP TABLE invoices;--", "%' UNION SELECT password_hash FROM users--", "\" OR \"\"=\""]
    bad = []
    for e in evil:
        for p in ("/lifecycle/", "/trust/invoice/", "/report/invoice/", "/review/", "/audit/"):
            r = client.get(p + e, headers=h)
            if r.status_code >= 500 or r.status_code == 200:
                bad.append((p, e, r.status_code))
        for p, q in (("/invoices", "search"), ("/vendors", "q"), ("/pos", "q"), ("/payments", "status"), ("/ledger", "currency"),
                     ("/exceptions", "status")):
            r = client.get(p, params={q: e, "search": e}, headers=h)
            if r.status_code >= 500:
                bad.append((p, e, r.status_code))
    check("no injected id/filter reaches the database as SQL", not bad, bad)
    check("the tables are still there", one("SELECT count(*) FROM invoices")[0] >= 0)
    r = client.post("/auth/login", json={"email": "' OR 1=1 --", "password": "x"})
    check("login with an injection string is refused", r.status_code in (400, 401, 422), r.status_code)


def test_auth_transport():
    section("§2 auth model: bearer tokens (no cookies), so CSRF does not apply; expiry and revocation")
    w = world(); uid = w["users"]["admin"][0]
    r = client.get("/auth/me", headers=bearer(uid))
    check("a valid token works", r.status_code == 200)
    check("the server sets no cookie that a browser would send on its own", "set-cookie" not in {k.lower() for k in r.headers})
    run("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (uid,))
    check("re-signing with the current token version still works", client.get("/auth/me", headers=bearer(uid)).status_code == 200)
    old = {"Authorization": "Bearer " + __import__("backend.auth", fromlist=["x"]).make_token(uid, 0)}
    check("an old token version is refused", client.get("/auth/me", headers=old).status_code == 401)
    r = client.get("/auth/me", headers={"Authorization": "Bearer ", "Origin": "https://evil.example"})
    check("a request with no credentials from another origin is refused", r.status_code == 401)
    pre = client.options("/invoices", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    check("CORS does not echo an arbitrary origin with credentials", pre.headers.get("access-control-allow-origin") not in ("https://evil.example",) or
          pre.headers.get("access-control-allow-credentials") != "true", dict(pre.headers))


def test_secrets():
    section("§2 no secrets committed to git")
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    pat = re.compile(r"(AIza[0-9A-Za-z_\-]{35}|sk-[A-Za-z0-9]{20,}|-----BEGIN (RSA |EC )?PRIVATE KEY-----|"
                     r"(?i:client_secret|paypal_secret|gemini_api_key|api_key)\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}['\"]|"
                     r"postgres(ql)?://[^:\s]+:[^@\s]{6,}@(?!localhost|127\.0\.0\.1|db[:/]))")
    hits = []
    for f in out:
        if f.endswith((".png", ".jpg", ".pdf", ".ico", ".woff2")):
            continue
        try:
            t = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for m in pat.finditer(t):
            hits.append((f, m.group(0)[:25] + "..."))
    check("no API key, private key or non-local database password in any tracked file", not hits, hits)
    check(".env is git-ignored", ".env" in open(".gitignore").read() if __import__("os").path.exists(".gitignore") else False)
    tracked_env = [f for f in out if f.split("/")[-1] == ".env"]
    check("no .env file is tracked", not tracked_env, tracked_env)


def main():
    for t in (test_inventory_and_auth, test_roles, test_idor_matrix, test_injection, test_auth_transport, test_secrets):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
