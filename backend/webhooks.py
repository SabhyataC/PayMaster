import json
import os

import requests
from fastapi import APIRouter, FastAPI, HTTPException, Request

from backend.db import get_conn
from backend.settle import sync_payment

from backend.paypal_client import BASE, get_token  # also loads .env

WEBHOOK_ID = os.environ["PAYPAL_WEBHOOK_ID"]

# PayPal puts these in the request headers of every webhook.
HEADER_FIELDS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def verify_signature(headers, raw_body: str) -> bool:
    """Ask PayPal whether this webhook is genuine."""
    parts = {}
    for field, header in HEADER_FIELDS.items():
        value = headers.get(header)
        if not value:
            return False
        parts[field] = value
    parts["webhook_id"] = WEBHOOK_ID

    # Build the JSON by hand so the event goes to PayPal exactly as we received it.
    # (Re-encoding it can change the text and break the signature match.)
    prefix = json.dumps(parts)[:-1]
    payload = f'{prefix}, "webhook_event": {raw_body}}}'

    try:
        r = requests.post(
            f"{BASE}/v1/notifications/verify-webhook-signature",
            data=payload,
            headers={
                "Authorization": f"Bearer {get_token()}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.RequestException:
        return False
    return r.ok and r.json().get("verification_status") == "SUCCESS"


router = APIRouter()


@router.get("/")
def health():
    return {"status": "ok"}


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw = (await request.body()).decode("utf-8")

    if not verify_signature(request.headers, raw):
        raise HTTPException(status_code=400, detail="invalid signature")

    event = json.loads(raw)
    resource = event.get("resource", {})
    batch_id = resource.get("payout_batch_id") or resource.get(
        "batch_header", {}
    ).get("payout_batch_id")
    print("VERIFIED:", event.get("event_type"), batch_id)

    if not batch_id:
        return {"received": True, "action": "ignored"}

    try:
        with get_conn() as conn:
            result = sync_payment(conn, batch_id)
    except Exception as e:
        print("SYNC FAILED:", e)
        # A non-2xx answer makes PayPal send the event again later. That is what we want.
        raise HTTPException(status_code=500, detail="sync failed")

    print("SYNC:", result)
    return {"received": True, **result}

from backend.review_api import router as review_router
from backend.exceptions_api import router as exceptions_router
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Review-Token", "Content-Type"],
)
app.include_router(router)
app.include_router(review_router)
app.include_router(exceptions_router)