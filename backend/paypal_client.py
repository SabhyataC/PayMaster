import os
import random
import time
from decimal import Decimal

import requests
from dotenv import load_dotenv

load_dotenv()



def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def base_url() -> str:
    return _env("PAYPAL_BASE_URL").rstrip("/")

# Error codes from your sandbox findings that mean "outcome unknown".
UNKNOWN_OUTCOME_CODES = ("ERRPYO036",)

_token = {"value": None, "expires_at": 0.0}

# Retries are safe for a payout because every try carries the same PayPal-Request-Id: PayPal returns the first
# result instead of paying twice. We retry only what may be temporary (no answer, 408, 429, 5xx), never a 4xx.
MAX_TRIES = int(os.environ.get("PAYPAL_MAX_TRIES", "3"))
RETRY_DEADLINE_SECONDS = 70      # stop retrying after this long so the payment row is not locked for minutes
_sleep = time.sleep              # tests replace this


def _backoff(attempt):
    """1s, 2s, 4s... plus up to 0.5s of jitter."""
    return (2 ** attempt) + random.random() * 0.5


def _post_with_retry(url, **kw):
    """POST with retries on temporary trouble. Returns (response_or_None, last_error_text, tries)."""
    start, err = time.time(), ""
    for attempt in range(MAX_TRIES):
        try:
            r = requests.post(url, **kw)
        except requests.RequestException as e:
            r, err = None, f"{type(e).__name__}: {e}"
        else:
            if r.status_code not in (408, 429) and r.status_code < 500:
                return r, "", attempt + 1
            err = f"http {r.status_code}"
        if attempt + 1 >= MAX_TRIES or time.time() - start > RETRY_DEADLINE_SECONDS:
            return r, err, attempt + 1
        _sleep(_backoff(attempt))
    return None, err, MAX_TRIES


def get_token() -> str:
    """Get a PayPal access token, reusing it until shortly before it expires."""
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    r = requests.post(
        f"{base_url()}/v1/oauth2/token",
        auth=(_env("PAYPAL_CLIENT_ID"), _env("PAYPAL_CLIENT_SECRET")),
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
    token, terr = None, ""
    for attempt in range(MAX_TRIES):
        try:
            token = get_token()
            break
        except requests.RequestException as e:   # no payout has been sent yet, so retrying is always safe
            terr = f"token_request_failed: {e}"
            if attempt + 1 < MAX_TRIES:
                _sleep(_backoff(attempt))
    if token is None:
        return {"result": "rejected", "error": terr}

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

    r, err, tries = _post_with_retry(
        f"{base_url()}/v1/payments/payouts", json=body, headers=headers, timeout=(5, 30))
    if r is None:
        # no answer after every try: we cannot know if PayPal got it
        return {"result": "unknown", "error": err, "tries": tries}

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