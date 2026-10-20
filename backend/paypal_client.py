import os
import time
from decimal import Decimal

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ["PAYPAL_BASE_URL"]
CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]
SECRET = os.environ["PAYPAL_CLIENT_SECRET"]

# Error codes from your sandbox findings that mean "outcome unknown".
UNKNOWN_OUTCOME_CODES = ("ERRPYO036",)

_token = {"value": None, "expires_at": 0.0}


def get_token() -> str:
    """Get a PayPal access token, reusing it until shortly before it expires."""
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    r = requests.post(
        f"{BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _token["value"] = data["access_token"]
    _token["expires_at"] = time.time() + int(data.get("expires_in", 300))
    return _token["value"]


def send_payout(*, key, email, amount, currency="USD", note="Paymaster payment") -> dict:
    """
    Send one payout. `key` is the idempotency key from make_key().
    Returns {"result": "submitted" | "duplicate" | "rejected" | "unknown", ...}
    """
    try:
        token = get_token()
    except requests.RequestException as e:
        return {"result": "rejected", "error": f"token_request_failed: {e}"}

    body = {
        "sender_batch_header": {
            "sender_batch_id": key,
            "email_subject": "Paymaster payment",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": email,
                "amount": {"value": f"{Decimal(amount):.2f}", "currency": currency},
                "note": note,
                "sender_item_id": f"{key}-item-1",
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": key,  # same key on every retry, on purpose
    }

    try:
        r = requests.post(
            f"{BASE}/v1/payments/payouts", json=body, headers=headers, timeout=(5, 30)
        )
    except requests.RequestException as e:
        # timeout or dropped connection: we cannot know if PayPal got it
        return {"result": "unknown", "error": f"{type(e).__name__}: {e}"}

    try:
        data = r.json()
    except ValueError:
        data = {}

    if r.status_code in (200, 201):
        header = data.get("batch_header", {})
        return {
            "result": "submitted",
            "paypal_batch_id": header.get("payout_batch_id"),
            "batch_status": header.get("batch_status"),
            "http_status": r.status_code,
        }

    # PayPal already has a batch with this key: an earlier call got through.
    # This is NOT a failure and NOT a new payment.
    if isinstance(data, dict):
        for d in data.get("details", []):
            if d.get("field") == "SENDER_BATCH_ID":
                existing = None
                for link in d.get("link", []):
                    if link.get("rel") == "self":
                        existing = link.get("href", "").rstrip("/").split("/")[-1]
                return {
                    "result": "duplicate",
                    "paypal_batch_id": existing,
                    "http_status": r.status_code,
                }
            
    # Not a success. Decide between "unknown" and "rejected".
    if r.status_code >= 500 or r.status_code == 408 or any(
        code in r.text for code in UNKNOWN_OUTCOME_CODES
    ):
        return {"result": "unknown", "http_status": r.status_code, "error": data or r.text[:300]}

    return {"result": "rejected", "http_status": r.status_code, "error": data or r.text[:300]}