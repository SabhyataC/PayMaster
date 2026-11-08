"""Alert tests (email + WhatsApp) with fake senders. Run on a TEST database:  python -m tests.test_notify"""
import sys

from tests.test_hardening import (  # noqa: F401
    RESULTS, bearer, check, client, mk_invoice, mk_po, mk_vendor, one, run, section, uniq, world,
)
from scripts.eval import install_hostile_stub

install_hostile_stub()
from backend import notify  # noqa: E402
from backend.db import get_conn  # noqa: E402
from backend.investigator import investigate  # noqa: E402

SENT = []
FAIL = {"whatsapp": 0}


def fake_email(to, subject, body):
    SENT.append(("email", to, subject, body))


def fake_wa(to, subject, body):
    if FAIL["whatsapp"] > 0:
        FAIL["whatsapp"] -= 1
        raise RuntimeError("provider down token=SECRET123")
    SENT.append(("whatsapp", to, subject, body))


notify.TRANSPORTS["email"] = fake_email
notify.TRANSPORTS["whatsapp"] = fake_wa


def turn_on(w, emails=("ops@acme.test",), phones=("+919876543210",), events=None):
    r = client.post("/admin/notifications", json={"enabled": True, "email_to": list(emails), "whatsapp_to": list(phones),
                                                   "events": events or notify.DEFAULT_EVENTS}, headers=bearer(w["users"]["admin"][0]))
    return r


def test_settings():
    section("Alerts: settings are validated, admin-only, audited")
    w = world()
    a, v = bearer(w["users"]["admin"][0]), bearer(w["users"]["viewer"][0])
    check("viewer cannot read", client.get("/admin/notifications", headers=v).status_code == 403)
    check("viewer cannot change", client.post("/admin/notifications", json={"enabled": False}, headers=v).status_code == 403)
    check("no login refused", client.get("/admin/notifications").status_code in (401, 403))
    g = client.get("/admin/notifications", headers=a).json()
    check("default is off", g["settings"]["enabled"] is False and "server" in g)
    check("bad email refused", client.post("/admin/notifications", json={"enabled": False, "email_to": ["not-an-email"]}, headers=a).status_code == 422)
    check("email with CRLF refused", client.post("/admin/notifications", json={"enabled": False, "email_to": ["a@b.com\\r\\nBcc: x@y.com"]}, headers=a).status_code == 422)
    check("bad phone refused", client.post("/admin/notifications", json={"enabled": False, "whatsapp_to": ["98765"]}, headers=a).status_code == 422)
    check("turning on with nobody refused", client.post("/admin/notifications", json={"enabled": True}, headers=a).status_code == 422)
    check("unknown event refused", client.post("/admin/notifications", json={"enabled": False, "events": ["nope"]}, headers=a).status_code == 422)
    check("more than 5 refused", client.post("/admin/notifications", json={"enabled": False, "email_to": [f"u{i}@x.com" for i in range(6)]}, headers=a).status_code == 422)
    ok = turn_on(w, emails=("Ops@Acme.test", "ops@acme.test"))
    check("valid save works and de-duplicates", ok.status_code == 200 and ok.json()["settings"]["email_to"] == ["Ops@Acme.test"], ok.text[:200])
    n = one("SELECT count(*) FROM audit_log WHERE decision = 'alerts_changed' AND entity_id = %s", (w["cid"],))[0]
    check("change is in the audit log", n >= 1)
    o = world()
    check("other company sees its own (off)", client.get("/admin/notifications", headers=bearer(o["users"]["admin"][0])).json()["settings"]["enabled"] is False)


def test_hold_alert():
    section("Alerts: a held invoice sends one message per recipient, once, with no secrets")
    SENT.clear()
    w = world()
    turn_on(w)
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil@other-domain.example.net")
    investigate(inv)
    check("outbox row written", one("SELECT count(*) FROM notification_outbox WHERE company_id = %s AND event = 'invoice_held'", (w["cid"],))[0] == 1)
    check("nothing sent before dispatch", not SENT)
    notify.dispatch()
    kinds = sorted(s[0] for s in SENT)
    check("one email and one WhatsApp", kinds == ["email", "whatsapp"], str(kinds))
    body = SENT[0][3]
    check("says the invoice is on hold", "on hold" in body and "payout details changed" in body, body)
    check("no email address or bank detail in message", "evil@" not in body and "@" not in body)
    notify.dispatch()
    check("second dispatch sends nothing more", len(SENT) == 2)
    check("outbox marked sent", one("SELECT status FROM notification_outbox WHERE company_id = %s AND event = 'invoice_held'", (w["cid"],))[0] == "sent")
    investigate(inv)
    notify.dispatch()
    check("already-held invoice does not alert again", len(SENT) == 2)


def test_off_and_event_filter():
    section("Alerts: off means nothing is queued; event filter respected")
    SENT.clear()
    w = world()
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil@other-domain.example.net")
    investigate(inv)
    check("alerts never turned on: nothing queued", one("SELECT count(*) FROM notification_outbox WHERE company_id = %s", (w["cid"],))[0] == 0)
    turn_on(w, events=["payment_problem"])
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil2@other-domain.example.net")
    investigate(inv2)
    check("held alert not queued when event is unchecked", one("SELECT count(*) FROM notification_outbox WHERE company_id = %s", (w["cid"],))[0] == 0)
    client.post("/admin/notifications", json={"enabled": False, "email_to": ["ops@acme.test"], "events": notify.DEFAULT_EVENTS}, headers=bearer(w["users"]["admin"][0]))
    inv3 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil3@other-domain.example.net")
    investigate(inv3)
    check("turned off: nothing queued", one("SELECT count(*) FROM notification_outbox WHERE company_id = %s", (w["cid"],))[0] == 0)


def test_failures_and_retry():
    section("Alerts: a failing provider is retried, logged without secrets, and never breaks Verify")
    SENT.clear()
    w = world()
    turn_on(w)
    FAIL["whatsapp"] = 1
    inv = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil@other-domain.example.net")
    r = investigate(inv)
    check("Verify still finished and held it", r["status"] == "held")
    import os
    os.environ["TWILIO_AUTH_TOKEN"] = "SECRET123"
    notify.dispatch()
    row = one("SELECT status, attempts, last_error FROM notification_outbox WHERE company_id = %s", (w["cid"],))
    check("first attempt: still pending, email went out", row[0] == "pending" and row[1] == 1 and [s[0] for s in SENT] == ["email"], str(row))
    check("secret is scrubbed from the stored error", "SECRET123" not in (row[2] or ""), row[2])
    notify.dispatch()
    row = one("SELECT status, attempts FROM notification_outbox WHERE company_id = %s", (w["cid"],))
    check("retry delivers WhatsApp only (email not repeated)", row[0] == "sent" and sorted(s[0] for s in SENT) == ["email", "whatsapp"], str(row))
    os.environ.pop("TWILIO_AUTH_TOKEN", None)
    FAIL["whatsapp"] = 99
    inv2 = mk_invoice(w["cid"], w["vid"], w["po_id"], status="received", email="evil9@other-domain.example.net")
    investigate(inv2)
    for _ in range(4):
        notify.dispatch()
    st = one("SELECT status, attempts FROM notification_outbox WHERE company_id = %s ORDER BY id DESC LIMIT 1", (w["cid"],))
    check("gives up after 3 attempts", st[0] == "failed" and st[1] == 3, str(st))
    FAIL["whatsapp"] = 0
    log = client.get("/admin/notifications", headers=bearer(w["users"]["admin"][0])).json()["log"]
    check("log shows the failure", any(x["status"] == "failed" for x in log))


def test_test_message_and_isolation():
    section("Alerts: test message works, is rate-limited, and other companies never get ours")
    SENT.clear()
    w, o = world(), world()
    turn_on(w); turn_on(o, emails=("other@else.test",), phones=())
    a = bearer(w["users"]["admin"][0])
    r = client.post("/admin/notifications/test", headers=a)
    check("test sends to every recipient", r.status_code == 200 and r.json()["ok"] and len(r.json()["results"]) == 2, r.text[:200])
    check("test went only to our recipients", all(s[1] in ("ops@acme.test", "+919876543210") for s in SENT))
    for _ in range(5):
        r = client.post("/admin/notifications/test", headers=a)
    check("rate limited after 5 tests", r.status_code == 429)
    inv = mk_invoice(o["cid"], o["vid"], o["po_id"], status="received", email="evil@other-domain.example.net")
    SENT.clear(); investigate(inv); notify.dispatch()
    check("other company's alert goes to its own address only", [s[1] for s in SENT] == ["other@else.test"], str(SENT))
    nolog = client.post("/admin/notifications/test", headers=bearer(world()["users"]["admin"][0]))
    check("test with no recipients saved: clear error", nolog.status_code == 422)


def test_text_safety():
    section("Alerts: text from vendors cannot inject lines or links")
    check("newlines and control chars removed", "\n" not in notify.clean("Acme\nBcc: x@y.com\x00  Ltd"))
    check("long names shortened", len(notify.clean("A" * 500)) <= 60)


def main():
    for t in (test_settings, test_hold_alert, test_off_and_event_filter, test_failures_and_retry, test_test_message_and_isolation, test_text_safety):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
