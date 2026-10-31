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

def fetch_item(item_id: str) -> dict:
    """Ask PayPal for one payout item. Its details include the currency conversion, if any."""
    r = requests.get(
        f"{BASE}/v1/payments/payouts-item/{item_id}",
        headers={"Authorization": f"Bearer {get_token()}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _money(block):
    """PayPal money block {"value": "10.00", "currency": "EUR"} -> (Decimal, currency)."""
    if not block or block.get("value") is None:
        return None, None
    return Decimal(str(block["value"])), block.get("currency")


def settlement_from_item(detail: dict) -> dict:
    """Read the money facts out of PayPal's item. We store what PayPal says, never our own rate."""
    received, received_cur = _money((detail.get("payout_item") or {}).get("amount"))
    fee, fee_cur = _money(detail.get("payout_item_fee"))
    conv = detail.get("currency_conversion")
    if conv:
        sent, sent_cur = _money(conv.get("from_amount"))
        converted, _ = _money(conv.get("to_amount"))
        rate = conv.get("exchange_rate")
        rate = Decimal(str(rate)) if rate is not None else None
    else:
        # No conversion: our account paid the payout plus the fee, all in one currency.
        sent = None if received is None else received + (fee or Decimal("0"))
        sent_cur, rate = received_cur, None
        converted = None
    if sent is None or received is None:
        raise ValueError("PayPal item has no amounts; cannot build a settlement")
    return {
        "amount_sent": sent, "currency_sent": sent_cur,
        "amount_received": received, "currency_received": received_cur,
        "fx_rate": rate, "fee_amount": fee, "fee_currency": fee_cur,"amount_converted": converted,
    }

def ledger_rows(amount, currency, s):
    """Rows for a SUCCESS payout, following docs/reconcile.md. s is the settlement dict."""
    fee = s["fee_amount"] or Decimal("0")
    fee_cur = s["fee_currency"] or currency
    if s["currency_sent"] == s["currency_received"]:      # no conversion
        rows = [("payment", "accounts_payable", "debit", amount, currency, "Vendor payment"),
                ("payment", "paypal_balance", "credit", amount, currency, "Vendor payment")]
        if fee > 0:
            rows += [("fee", "fees_expense", "debit", fee, fee_cur, "PayPal payout fee"),
                     ("fee", "paypal_balance", "credit", fee, fee_cur, "PayPal payout fee")]
        return rows
    # converted: USD side and EUR/GBP side are joined by fx_clearing, with PayPal's own numbers
    rows = [("fx", "fx_clearing", "debit", s["amount_sent"], s["currency_sent"], "Currency conversion (PayPal)"),
            ("fx", "paypal_balance", "credit", s["amount_sent"], s["currency_sent"], "Currency conversion (PayPal)"),
            ("payment", "accounts_payable", "debit", amount, currency, "Vendor payment")]
    if fee > 0:
        rows.append(("fee", "fees_expense", "debit", fee, fee_cur, "PayPal payout fee"))
    rows.append(("fx", "fx_clearing", "credit", s["amount_converted"], s["currency_received"],
                 "Currency conversion (PayPal)"))
    return rows


def check_balanced(rows):
    """Debits must equal credits inside each currency, or we refuse to write anything."""
    totals = {}
    for _, _, direction, value, cur, _ in rows:
        totals[cur] = totals.get(cur, Decimal("0")) + (value if direction == "debit" else -value)
    bad = {c: str(v) for c, v in totals.items() if v != 0}
    if bad:
        raise ValueError(f"Ledger rows do not balance per currency: {bad}")

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
    settlement = None
    if new_status == "success":
        # Network call before we lock anything. If it fails, nothing is written and PayPal's
        # webhook retry (or our next sync) tries again, so a success never lacks a settlement.
        settlement = settlement_from_item(fetch_item(item["payout_item_id"]))

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
        company_id = conn.execute(
            "SELECT company_id FROM payments WHERE id = %s", (payment_id,)
        ).fetchone()[0]
        already = conn.execute(
            "SELECT 1 FROM ledger_entries WHERE payment_id = %s AND entry_type = 'payment'",
            (payment_id,),
        ).fetchone()
        if not already:
            rows = ledger_rows(amount, currency, settlement)
            check_balanced(rows)   # raises before anything is committed
            for entry_type, account, direction, value, row_currency, desc in rows:
                conn.execute(
                    """
                    INSERT INTO ledger_entries
                        (payment_id, entry_type, account, direction, amount, currency, description, company_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (payment_id, entry_type, account, direction, value, row_currency, desc, company_id),
                )
        conn.execute(
            """
            INSERT INTO settlements
                (payment_id, amount_sent, currency_sent, amount_received,
                 currency_received, fx_rate, fee_amount, fee_currency)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (payment_id) DO NOTHING
            """,
            (payment_id, settlement["amount_sent"], settlement["currency_sent"],
             settlement["amount_received"], settlement["currency_received"],
             settlement["fx_rate"], settlement["fee_amount"], settlement["fee_currency"]),
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