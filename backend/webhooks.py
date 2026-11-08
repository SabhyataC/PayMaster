import json
import os
from contextlib import contextmanager

import requests
from fastapi import APIRouter, FastAPI, HTTPException, Request
from backend.exceptions_api import router as exceptions_router, run_router
from backend.db import get_conn
from backend.settle import sync_payment
from backend.corrections_api import router as corrections_router
from fastapi import Depends
from backend.auth import current_member

from backend.paypal_client import base_url, get_token  # also loads .env

MAX_WEBHOOK_BYTES = 256 * 1024


def _webhook_id() -> str:
    return os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()

# PayPal puts these in the request headers of every webhook.
HEADER_FIELDS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def verify_signature(headers, raw_body: str) -> bool:
    """Ask PayPal whether this webhook is genuine. With no PAYPAL_WEBHOOK_ID set nothing is genuine."""
    webhook_id = _webhook_id()
    if not webhook_id:
        return False
    parts = {}
    for field, header in HEADER_FIELDS.items():
        value = headers.get(header)
        if not value:
            return False
        parts[field] = value
    parts["webhook_id"] = webhook_id

    # Build the JSON by hand so the event goes to PayPal exactly as we received it.
    # (Re-encoding it can change the text and break the signature match.)
    prefix = json.dumps(parts)[:-1]
    payload = f'{prefix}, "webhook_event": {raw_body}}}'

    try:
        r = requests.post(
            f"{base_url()}/v1/notifications/verify-webhook-signature",
            data=payload,
            headers={
                "Authorization": f"Bearer {get_token()}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except Exception:
        return False
    return r.ok and r.json().get("verification_status") == "SUCCESS"


router = APIRouter()


@router.get("/")
def health():
    return {"status": "ok"}


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    if not _webhook_id():
        raise HTTPException(status_code=503, detail="webhook is not configured")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    raw = body.decode("utf-8", errors="replace")

    if not verify_signature(request.headers, raw):
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        event = json.loads(raw)
        resource = event.get("resource") or {}
        event_id = str(event.get("id") or request.headers.get("paypal-transmission-id") or "")
        event_type = str(event.get("event_type") or "")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid body")
    if not event_id:
        raise HTTPException(status_code=400, detail="event has no id")
    batch_id = resource.get("payout_batch_id") or (resource.get("batch_header") or {}).get("payout_batch_id")

    result = record_and_process(event_id, event_type, batch_id, event)
    return {"received": True, **result}


def record_and_process(event_id, event_type, batch_id, event):
    """Each PayPal event is processed once. A replay of one already processed is acknowledged and
    ignored; one that failed before is tried again (PayPal re-sends on a non-2xx answer)."""
    from psycopg.types.json import Jsonb

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO webhook_events (event_id, event_type, batch_id, payload)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (event_id) DO UPDATE SET attempts = webhook_events.attempts + 1""",
            (event_id, event_type, batch_id, Jsonb(event)),
        )

    with _event_lock(event_id):
        with get_conn() as conn:
            done = conn.execute("SELECT processed_at FROM webhook_events WHERE event_id = %s",
                                (event_id,)).fetchone()[0]
        if done is not None:
            return {"action": "duplicate"}

        if not batch_id:
            _mark_processed(event_id, "ignored")
            return {"action": "ignored"}

        try:
            with get_conn() as conn:
                result = sync_payment(conn, batch_id)
        except Exception as e:
            print("SYNC FAILED:", type(e).__name__)
            raise HTTPException(status_code=500, detail="sync failed")

        _mark_processed(event_id, result.get("result", "done"))
        return result


@contextmanager
def _event_lock(event_id):
    """One process at a time per event id. A second copy of the same event waits here, then sees the
    first one's outcome. The lock belongs to this connection, so it is released even if we crash."""
    import psycopg
    from backend.db import DATABASE_URL
    with psycopg.connect(DATABASE_URL, autocommit=True) as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (event_id,))
        try:
            yield
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (event_id,))


def _mark_processed(event_id, outcome):
    with get_conn() as conn:
        conn.execute("UPDATE webhook_events SET processed_at = now(), result = %s WHERE event_id = %s",
                     (outcome, event_id))


from backend.review_api import router as review_router
from backend.exceptions_api import router as exceptions_router
from fastapi.middleware.cors import CORSMiddleware
from backend.console_api import router as console_router
from backend.audit_api import router as audit_router 
from backend.chat_api import router as chat_router
from backend.advise_api import router as advise_router  
from backend.auth_api import router as auth_router
from backend.vendors_api import router as vendors_router
from backend.po_api import router as po_router
from backend.upload_api import router as upload_router
from backend.attention_api import router as attention_router
from backend.changes_api import router as changes_router
from backend.trust_api import router as trust_router
from backend.report_api import router as report_router
from backend.analytics_api import router as analytics_router
from backend.notify_api import router as notify_router
from backend.insights_api import router as insights_router

app = FastAPI()
def console_origins():
    raw = os.environ.get("CONSOLE_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    if not origins or any(o == "*" or "*" in o for o in origins):
        raise RuntimeError("CONSOLE_ORIGINS must list exact origins; '*' is not allowed")
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=console_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF"],
)


@app.middleware("http")
async def limit_upload_size(request, call_next):
    if request.method == "POST" and request.url.path == "/invoices/upload":
        from backend.upload_checks import MAX_BYTES
        from fastapi.responses import JSONResponse
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return JSONResponse({"detail": "Content-Length is required"}, status_code=411)
        if int(length) > MAX_BYTES + 64 * 1024:
            return JSONResponse({"detail": "File is larger than 10 MB"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response
guard = [Depends(current_member)]
app.include_router(router)   # PayPal webhooks: stay open
app.include_router(auth_router)   # signup/login: stay open
from backend.admin_api import router as admin_router
app.include_router(admin_router)   # admin-only: each route checks the admin role
app.include_router(review_router, dependencies=guard)
app.include_router(exceptions_router, dependencies=guard)
app.include_router(run_router, dependencies=guard)
app.include_router(corrections_router, dependencies=guard)
app.include_router(console_router, dependencies=guard)
app.include_router(audit_router, dependencies=guard)
app.include_router(chat_router, dependencies=guard)
app.include_router(advise_router, dependencies=guard)
app.include_router(vendors_router, dependencies=guard)
app.include_router(po_router, dependencies=guard)
app.include_router(upload_router, dependencies=guard)
app.include_router(attention_router, dependencies=guard)
app.include_router(changes_router, dependencies=guard)
app.include_router(trust_router, dependencies=guard)
app.include_router(report_router, dependencies=guard)
app.include_router(analytics_router, dependencies=guard)
app.include_router(notify_router)
app.include_router(insights_router, dependencies=guard)


@app.on_event("startup")
def _start_alert_worker():
    from backend.notify import start_worker
    start_worker()
    if os.environ.get("RECOVER_WORKER", "1") != "0":
        import threading, time as _t

        def loop():
            from backend.reconcile import recover_unknown
            while True:
                _t.sleep(60)
                try:
                    with get_conn() as conn:
                        recover_unknown(conn)
                        conn.commit()
                except Exception:
                    pass
        threading.Thread(target=loop, name="recover-unknown", daemon=True).start()
