"""Alerts by email and WhatsApp.

emit()      called inside the transaction that creates the event; writes one outbox row (never sends).
dispatch()  sends pending outbox rows. Run by a small worker thread, or called directly in tests.
Nothing here can block or break payments: a failed send is logged and retried, never raised to the caller.

Messages never contain bank details or any text from an invoice body. Only our own fixed wording, the invoice
number, the vendor name (cleaned) and the amount.

Server settings (environment, never stored in the database):
  Email:    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (defaults to SMTP_USER)
  WhatsApp: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (like whatsapp:+14155238886)
  Links:    CONSOLE_URL (like https://paymaster.example.com/console), optional
"""
import os
import re
import smtplib
import threading
import time
from email.message import EmailMessage

import httpx

from backend.db import get_conn

EVENTS = {
    "invoice_held": "An invoice is on hold and needs a person",
    "payment_problem": "A payment has a problem (failed, stuck or unknown)",
    "document_needs_fix": "A document could not be read safely",
    "payment_sent": "A payment was confirmed by PayPal",
}
DEFAULT_EVENTS = ["invoice_held", "payment_problem", "document_needs_fix"]
MAX_RECIPIENTS = 5
MAX_ATTEMPTS = 3
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']{1,64}@[^@\s,;<>\"']{1,200}\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")

REASON_WORDS = {
    "bank_details_changed": "payout details changed", "po_not_found": "no matching purchase order",
    "po_belongs_to_other_vendor": "PO belongs to another vendor", "delivery_not_received_yet": "goods not received yet",
    "amount_out_of_tolerance": "amount over the PO", "quantity_out_of_tolerance": "quantity over the PO",
    "duplicate_invoice": "possible duplicate", "investigator_failed": "the AI check could not finish",
}
PROBLEM_WORDS = {
    "payment_failed": "PayPal reported the payout failed", "unknown_outcome": "the payout outcome is unknown",
    "pending_too_long": "the payout has been pending too long", "unclaimed_too_long": "the vendor has not claimed the payout",
    "created_too_long": "the payout was created but never sent", "unknown_payout": "PayPal shows a payout we have no record of",
    "payment_returned": "the payout was returned",
}


# ---------- writing events ----------
def clean(text, limit=60):
    """Text that came from a person or a document: one line, no control characters, shortened."""
    t = re.sub(r"[\x00-\x1f\x7f  ]+", " ", str(text or "")).strip()
    return (t[: limit - 1] + "…") if len(t) > limit else t


def _link(path):
    base = os.environ.get("CONSOLE_URL", "").rstrip("/")
    return f"\nOpen: {base}/{path}" if base else ""


def emit(conn, company_id, event, subject, body, dedupe_key):
    """Queue one alert, in the caller's transaction. Does nothing if the company has alerts off."""
    if company_id is None or event not in EVENTS:
        return False
    row = conn.execute("SELECT enabled, events FROM notification_settings WHERE company_id = %s", (company_id,)).fetchone()
    if not row or not row[0] or event not in (row[1] or []):
        return False
    cur = conn.execute(
        "INSERT INTO notification_outbox (company_id, event, subject, body, dedupe_key) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (company_id, dedupe_key) DO NOTHING", (company_id, event, subject[:150], body[:900], dedupe_key))
    return cur.rowcount == 1


def emit_invoice_held(conn, invoice_id, reasons):
    r = conn.execute("SELECT i.company_id, i.invoice_number, v.name, i.amount, i.currency FROM invoices i "
                     "JOIN vendors v ON v.id = i.vendor_id WHERE i.id = %s", (invoice_id,)).fetchone()
    if not r:
        return False
    cid, num, vendor, amount, cur = r
    why = ", ".join(REASON_WORDS.get(x, "a check failed") for x in (reasons or [])[:3]) or "a check failed"
    return emit(conn, cid, "invoice_held", f"Invoice {clean(num, 30)} is on hold",
                f"Invoice {clean(num, 30)} from {clean(vendor)} ({amount} {cur}) is on hold: {why}. "
                f"Nothing will be paid until a person decides.{_link('trust.html?invoice=' + clean(num, 30))}",
                f"held:{invoice_id}:{int(time.time()) // 120}")


def emit_payment_problem(conn, payment_id, kind):
    r = conn.execute("SELECT i.company_id, i.invoice_number, v.name, p.amount, p.currency FROM payments p "
                     "JOIN invoices i ON i.id = p.invoice_id JOIN vendors v ON v.id = i.vendor_id WHERE p.id = %s",
                     (payment_id,)).fetchone()
    if not r:
        return False
    cid, num, vendor, amount, cur = r
    return emit(conn, cid, "payment_problem", f"Payment problem on invoice {clean(num, 30)}",
                f"Payment for invoice {clean(num, 30)} to {clean(vendor)} ({amount} {cur}): "
                f"{PROBLEM_WORDS.get(kind, 'something needs a person')}.{_link('index.html#exceptions')}",
                f"pay:{payment_id}:{kind}")


def emit_payment_sent(conn, payment_id):
    r = conn.execute("SELECT i.company_id, i.invoice_number, v.name, p.amount, p.currency FROM payments p "
                     "JOIN invoices i ON i.id = p.invoice_id JOIN vendors v ON v.id = i.vendor_id WHERE p.id = %s",
                     (payment_id,)).fetchone()
    if not r:
        return False
    cid, num, vendor, amount, cur = r
    return emit(conn, cid, "payment_sent", f"Paid: invoice {clean(num, 30)}",
                f"PayPal confirmed {amount} {cur} to {clean(vendor)} for invoice {clean(num, 30)}.",
                f"sent:{payment_id}")


def emit_document_needs_fix(conn, extraction_id):
    r = conn.execute("SELECT company_id FROM extractions WHERE id = %s", (extraction_id,)).fetchone()
    if not r:
        return False
    return emit(conn, r[0], "document_needs_fix", "A document could not be read safely",
                f"A document could not be read safely and needs a person to fix it.{_link('index.html#invoices')}",
                f"doc:{extraction_id}")


# ---------- sending ----------
def configured():
    return {"email": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD")),
            "whatsapp": bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN")
                             and os.environ.get("TWILIO_WHATSAPP_FROM"))}


def send_email(to, subject, body):
    host, user, pw = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not (host and user and pw):
        raise RuntimeError("email is not configured on this server")
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or user
    msg["To"] = to
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:150]
    msg.set_content(body + "\n\n-- Paymaster. The AI only suggests; code checks and people decide.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)


def send_whatsapp(to, subject, body):
    sid, tok, frm = (os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"),
                     os.environ.get("TWILIO_WHATSAPP_FROM"))
    if not (sid and tok and frm):
        raise RuntimeError("WhatsApp is not configured on this server")
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok), timeout=15,
                   data={"From": frm if frm.startswith("whatsapp:") else f"whatsapp:{frm}",
                         "To": f"whatsapp:{to}", "Body": f"*{subject}*\n{body}"[:1500]})
    if r.status_code >= 300:
        try:
            msg = (r.json() or {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"WhatsApp provider said {r.status_code}: {msg[:120]}")


TRANSPORTS = {"email": send_email, "whatsapp": send_whatsapp}


def _safe_error(e):
    """Short text for the log. Never includes secrets: only our own wording or the provider's message."""
    s = str(e) or e.__class__.__name__
    for key in ("SMTP_PASSWORD", "TWILIO_AUTH_TOKEN"):
        v = os.environ.get(key)
        if v:
            s = s.replace(v, "***")
    return s[:200]


def _deliver(conn, outbox_id, company_id, subject, body, only_channel=None):
    """Send one outbox row to every recipient not already delivered. Returns (all_ok, results)."""
    row = conn.execute("SELECT email_to, whatsapp_to FROM notification_settings WHERE company_id = %s", (company_id,)).fetchone()
    targets = [("email", a) for a in (row[0] if row else [])] + [("whatsapp", p) for p in (row[1] if row else [])]
    done = {(c, r) for c, r in conn.execute("SELECT channel, recipient FROM notification_log WHERE outbox_id = %s AND status = 'sent'", (outbox_id,)).fetchall()}
    results, ok = [], True
    for channel, to in targets:
        if (channel, to) in done or (only_channel and channel != only_channel):
            continue
        try:
            TRANSPORTS[channel](to, subject, body)
            conn.execute("INSERT INTO notification_log (outbox_id, channel, recipient, status) VALUES (%s, %s, %s, 'sent')", (outbox_id, channel, to))
            results.append({"channel": channel, "to": to, "ok": True})
        except Exception as e:  # a failed send is recorded and retried, never raised
            err = _safe_error(e)
            conn.execute("INSERT INTO notification_log (outbox_id, channel, recipient, status, error) VALUES (%s, %s, %s, 'failed', %s)", (outbox_id, channel, to, err))
            results.append({"channel": channel, "to": to, "ok": False, "error": err})
            ok = False
    return ok, results


def dispatch(limit=20):
    """Send pending alerts. Returns how many rows were finished."""
    finished = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, company_id, subject, body, attempts FROM notification_outbox "
                            "WHERE status = 'pending' ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,)).fetchall()
        for oid, cid, subject, body, attempts in rows:
            s = conn.execute("SELECT enabled FROM notification_settings WHERE company_id = %s", (cid,)).fetchone()
            if not s or not s[0]:
                conn.execute("UPDATE notification_outbox SET status = 'skipped', done_at = now() WHERE id = %s", (oid,))
                finished += 1
                continue
            ok, results = _deliver(conn, oid, cid, subject, body)
            if ok:
                conn.execute("UPDATE notification_outbox SET status = 'sent', attempts = %s, done_at = now() WHERE id = %s", (attempts + 1, oid))
                finished += 1
            else:
                err = next((r["error"] for r in results if not r["ok"]), "")
                status = "failed" if attempts + 1 >= MAX_ATTEMPTS else "pending"
                conn.execute("UPDATE notification_outbox SET status = %s, attempts = %s, last_error = %s, done_at = CASE WHEN %s = 'failed' THEN now() END WHERE id = %s",
                             (status, attempts + 1, err, status, oid))
                finished += status == "failed"
            conn.commit()
    return finished


def run_forever(interval=10):
    while True:
        try:
            dispatch()
        except Exception:
            pass
        time.sleep(interval)


def start_worker():
    if os.environ.get("NOTIFY_WORKER", "1") == "0":
        return None
    t = threading.Thread(target=run_forever, name="notify-worker", daemon=True)
    t.start()
    return t
