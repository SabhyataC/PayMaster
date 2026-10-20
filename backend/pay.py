from decimal import Decimal

from backend.audit import log_decision
from backend.idempotency import reserve_payment
from backend.paypal_client import send_payout
from backend.policy import check_policy

# What each PayPal answer means for our payment row.
RESULT_TO_STATUS = {
    "submitted": "submitted",
    "duplicate": "submitted",  # PayPal already had it: an earlier call got through
    "unknown": "unknown",      # timeout etc.: money may have moved, never treat as failed
    "rejected": "failed",      # PayPal refused: no money moved
}

# Only these states may call PayPal. Anything else already has an outcome.
CAN_SEND = ("created", "unknown")


def pay_invoice(conn, invoice_id, *, approved_by=None) -> dict:
    """
    Try to pay one invoice. Safe to call any number of times.
    approved_by: name of the human releasing a 'hold'. A 'block' can never be released.
    """
    # 1. policy check
    policy = check_policy(conn, invoice_id)
    outcome, reasons, inputs = policy["outcome"], policy["reasons"], policy["inputs"]

    if outcome == "block":
        log_decision(
            conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
            decision="blocked", reason="Blocked by policy: " + ", ".join(reasons),
            inputs=inputs,
        )
        conn.commit()
        return {"status": "blocked", "reasons": reasons}

    if outcome == "hold" and not approved_by:
        log_decision(
            conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
            decision="held", reason="Held for a human: " + ", ".join(reasons),
            inputs=inputs,
        )
        conn.commit()
        return {"status": "held", "reasons": reasons}



    # 2. reserve the payment row and save it before talking to PayPal
    inv = conn.execute(
        "SELECT amount, currency, destination_email FROM invoices WHERE id = %s",
        (invoice_id,),
    ).fetchone()
    amount, currency, email = Decimal(inv[0]), inv[1], inv[2]

    pay = reserve_payment(
        conn, invoice_id=invoice_id, destination_email=email,
        amount=amount, currency=currency,
    )
    conn.commit()

    # 3. already has an outcome: do not send again
    if pay["status"] not in CAN_SEND:
        log_decision(
            conn, stage="pay", entity_type="payment", entity_id=pay["payment_id"],
            decision="skipped",
            reason=f"Payment already '{pay['status']}', not sending again",
            inputs={"key": pay["key"], "status": pay["status"]},
        )
        conn.commit()
        return {"status": "already_exists", "payment_status": pay["status"],
                "payment_id": pay["payment_id"]}


    if outcome == "hold":
        log_decision(
            conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
            decision="released",
            reason=f"Held for {', '.join(reasons)}; released by {approved_by}",
            inputs=inputs, actor="human",
        )
    result = send_payout(
        key=pay["key"], email=email, amount=amount, currency=currency,
        note="Paymaster payment",
    )
    new_status = RESULT_TO_STATUS[result["result"]]

    conn.execute(
        """
        UPDATE payments
        SET status = %s, paypal_batch_id = COALESCE(%s, paypal_batch_id), updated_at = now()
        WHERE id = %s
        """,
        (new_status, result.get("paypal_batch_id"), pay["payment_id"]),
    )

    # 5. audit
    log_decision(
        conn, stage="pay", entity_type="payment", entity_id=pay["payment_id"],
        decision=result["result"],
        reason=f"PayPal answered '{result['result']}', payment set to '{new_status}'",
        inputs={"key": pay["key"], "paypal": result},
    )
    conn.commit()

    return {
        "status": new_status,
        "paypal_result": result["result"],
        "payment_id": pay["payment_id"],
        "paypal_batch_id": result.get("paypal_batch_id"),
    }