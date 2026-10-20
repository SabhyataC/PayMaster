from decimal import Decimal

import requests

from backend.audit import log_decision
from backend.paypal_client import BASE, get_token

# PayPal item status -> our payment status. Anything not listed is logged, not applied.
ITEM_TO_PAYMENT_STATUS = {
    "SUCCESS": "success",
    "UNCLAIMED": "unclaimed",
    "PENDING": "pending",
    "ONHOLD": "pending",
    "FAILED": "failed",
    "BLOCKED": "failed",
    "DENIED": "failed",
}


def fetch_batch(batch_id: str) -> dict:
    """Ask PayPal for the real state of a payout batch."""
    r = requests.get(
        f"{BASE}/v1/payments/payouts/{batch_id}",
        headers={"Authorization": f"Bearer {get_token()}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sync_payment(conn, batch_id: str) -> dict:
    """
    Bring our payment up to date with what PayPal says about this batch.
    Safe to call any number of times for the same batch.
    """
    batch = fetch_batch(batch_id)  # network call first, before we lock anything
    header = batch.get("batch_header", {})
    key = header.get("sender_batch_header", {}).get("sender_batch_id")
    items = batch.get("items", [])
    if not items:
        return {"result": "no_items"}

    item = items[0]  # one item per payment in our design
    item_status = item.get("transaction_status")
    new_status = ITEM_TO_PAYMENT_STATUS.get(item_status)

    row = conn.execute(
        """
        SELECT id, status, amount, currency, invoice_id
        FROM payments
        WHERE paypal_batch_id = %s OR idempotency_key = %s
        FOR UPDATE
        """,
        (batch_id, key),
    ).fetchone()
    if row is None:
        conn.rollback()
        return {"result": "unmatched"}  # e.g. a payout sent by a scratch script

    payment_id, status, amount, currency, invoice_id = row

    if new_status is None:
        log_decision(
            conn, stage="pay", entity_type="payment", entity_id=payment_id,
            decision="unmapped_status",
            reason=f"PayPal item status '{item_status}' not handled; payment left as '{status}'",
            inputs={"batch_id": batch_id, "item_status": item_status},
        )
        conn.commit()
        return {"result": "unmapped_status", "item_status": item_status}

    if status == "success" or status == new_status:
        conn.rollback()
        return {"result": "no_change", "payment_status": status}

    conn.execute(
        """
        UPDATE payments
        SET status = %s, paypal_batch_id = %s, paypal_item_id = %s, updated_at = now()
        WHERE id = %s
        """,
        (new_status, batch_id, item.get("payout_item_id"), payment_id),
    )

    fee = Decimal("0")
    if new_status == "success":
        fee = Decimal(str(item.get("payout_item_fee", {}).get("value", "0")))
        already = conn.execute(
            "SELECT 1 FROM ledger_entries WHERE payment_id = %s AND entry_type = 'payment'",
            (payment_id,),
        ).fetchone()
        if not already:
            rows = [
                ("payment", "accounts_payable", "debit", amount, "Vendor payment"),
                ("payment", "paypal_balance", "credit", amount, "Vendor payment"),
            ]
            if fee > 0:
                rows += [
                    ("fee", "fees_expense", "debit", fee, "PayPal payout fee"),
                    ("fee", "paypal_balance", "credit", fee, "PayPal payout fee"),
                ]
            for entry_type, account, direction, value, desc in rows:
                conn.execute(
                    """
                    INSERT INTO ledger_entries
                        (payment_id, entry_type, account, direction, amount, currency, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (payment_id, entry_type, account, direction, value, currency, desc),
                )
        conn.execute("UPDATE invoices SET status = 'paid' WHERE id = %s", (invoice_id,))

    log_decision(
        conn, stage="pay", entity_type="payment", entity_id=payment_id,
        decision=new_status,
        reason=f"PayPal item status '{item_status}', payment moved '{status}' -> '{new_status}'",
        inputs={"batch_id": batch_id, "item_status": item_status, "fee": str(fee)},
    )
    conn.commit()
    return {"result": "updated", "payment_status": new_status}