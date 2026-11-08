"""Alert settings (admin only) and a test message."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import notify
from backend.audit import log_decision
from backend.auth import admin_only
from backend.db import get_conn

router = APIRouter(prefix="/admin", tags=["alerts"])


class AlertsIn(BaseModel):
    enabled: bool
    email_to: list[str] = []
    whatsapp_to: list[str] = []
    events: list[str] = notify.DEFAULT_EVENTS


def _norm_list(items, pattern, what):
    out = []
    for x in items:
        x = str(x).strip()
        if not x:
            continue
        if not pattern.match(x):
            raise HTTPException(422, f"'{x[:40]}' is not a valid {what}")
        if x.lower() not in [o.lower() for o in out]:
            out.append(x)
    if len(out) > notify.MAX_RECIPIENTS:
        raise HTTPException(422, f"at most {notify.MAX_RECIPIENTS} {what}s")
    return out


def _view(conn, cid):
    row = conn.execute("SELECT enabled, email_to, whatsapp_to, events FROM notification_settings WHERE company_id = %s", (cid,)).fetchone()
    s = {"enabled": row[0], "email_to": row[1], "whatsapp_to": row[2], "events": row[3]} if row else \
        {"enabled": False, "email_to": [], "whatsapp_to": [], "events": notify.DEFAULT_EVENTS}
    log = [{"at": t.isoformat(), "event": e, "channel": c, "to": r, "status": st, "error": err}
           for t, e, c, r, st, err in conn.execute(
               "SELECT l.created_at, o.event, l.channel, l.recipient, l.status, l.error FROM notification_log l "
               "JOIN notification_outbox o ON o.id = l.outbox_id WHERE o.company_id = %s ORDER BY l.id DESC LIMIT 15", (cid,)).fetchall()]
    return {"settings": s, "server": notify.configured(), "event_names": notify.EVENTS, "log": log}


@router.get("/notifications")
def get_alerts(user: dict = Depends(admin_only)):
    with get_conn() as conn:
        return _view(conn, user["company_id"])


@router.post("/notifications")
def set_alerts(body: AlertsIn, user: dict = Depends(admin_only)):
    emails = _norm_list(body.email_to, notify.EMAIL_RE, "email address")
    phones = _norm_list(body.whatsapp_to, notify.PHONE_RE, "WhatsApp number (use +countrycode…)")
    bad = [e for e in body.events if e not in notify.EVENTS]
    if bad:
        raise HTTPException(422, f"unknown event: {bad[0][:40]}")
    events = list(dict.fromkeys(body.events))
    if body.enabled and not (emails or phones):
        raise HTTPException(422, "add at least one email address or WhatsApp number before turning alerts on")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notification_settings (company_id, enabled, email_to, whatsapp_to, events, updated_at) VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (company_id) DO UPDATE SET enabled = EXCLUDED.enabled, email_to = EXCLUDED.email_to, "
            "whatsapp_to = EXCLUDED.whatsapp_to, events = EXCLUDED.events, updated_at = now()",
            (user["company_id"], body.enabled, emails, phones, events))
        log_decision(conn, stage="admin", entity_type="company", entity_id=user["company_id"], decision="alerts_changed",
                     reason=f"{user['full_name']} turned alerts {'ON' if body.enabled else 'OFF'} ({len(emails)} email, {len(phones)} WhatsApp)",
                     inputs={"by": user["id"], "enabled": body.enabled, "emails": len(emails), "whatsapp": len(phones), "events": events},
                     actor="human")
        conn.commit()
        return _view(conn, user["company_id"])


@router.post("/notifications/test")
def test_alert(user: dict = Depends(admin_only)):
    cid = user["company_id"]
    with get_conn() as conn:
        row = conn.execute("SELECT email_to, whatsapp_to FROM notification_settings WHERE company_id = %s", (cid,)).fetchone()
        if not row or not (row[0] or row[1]):
            raise HTTPException(422, "save at least one email address or WhatsApp number first")
        recent = conn.execute("SELECT count(*) FROM notification_outbox WHERE company_id = %s AND event = 'test' "
                              "AND created_at > now() - interval '10 minutes'", (cid,)).fetchone()[0]
        if recent >= 5:
            raise HTTPException(429, "too many test messages, try again in a few minutes")
        oid = conn.execute(
            "INSERT INTO notification_outbox (company_id, event, subject, body, dedupe_key, status) VALUES (%s, 'test', %s, %s, %s, 'sent') RETURNING id",
            (cid, "Paymaster test alert", f"This is a test from Paymaster, sent by {notify.clean(user['full_name'])}. Alerts are working.",
             f"test:{cid}:{recent}:{__import__('time').time_ns()}")).fetchone()[0]
        ok, results = notify._deliver(conn, oid, cid, "Paymaster test alert",
                                      f"This is a test from Paymaster, sent by {notify.clean(user['full_name'])}. Alerts are working.")
        conn.commit()
    return {"ok": ok, "results": results}
