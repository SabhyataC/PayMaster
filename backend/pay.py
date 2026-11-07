from decimal import Decimal

from backend.audit import log_decision
from backend.idempotency import reserve_payment
from backend.paypal_client import send_payout
from backend.policy import check_policy, payout_email
from backend.reconcile import add_exception
from backend.checks import run_hard_checks
from backend.verdict import (
    REASON_DELIVERY_NOT_RECEIVED, REASON_PO_MISSING, REASON_PO_WRONG_VENDOR,
)

# What each PayPal answer means for our payment row.
RESULT_TO_STATUS = {
    "submitted": "submitted",
    "duplicate": "submitted",  # PayPal already had it: an earlier call got through
    "unknown": "unknown",      # timeout etc.: money may have moved, never treat as failed
    "rejected": "failed",      # PayPal refused: no money moved
}

# Only these states may call PayPal. Anything else already has an outcome.
CAN_SEND = ("created", "unknown")


def pay_invoice(conn, invoice_id, *, approved_by=None, confirm_missing_delivery=False) -> dict:
    """
    Try to pay one invoice. Safe to call any number of times.
    approved_by: name of the human releasing a 'hold'. A 'block' can never be released.
    """
    # 0. only invoices the investigator approved may go near PayPal. The invoice row is locked
    #    until the payment row exists, so a person cannot reject it in the middle of paying.
    row = conn.execute("SELECT status FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,)).fetchone()
    if row is None or row[0] not in ("approved", "paid"):
        current = row[0] if row else None
        if row is not None:
            log_decision(
                conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
                decision="refused", reason=f"Invoice status is '{current}', not 'approved'",
                inputs={"invoice_status": current},
            )
        conn.commit()
        return {"status": "not_approved", "invoice_status": current}
        # 0b. re-check PO and delivery now (they can change after Verify)
    if row[0] == "approved":
        hard = run_hard_checks(invoice_id)
        po_no = hard["invoice"]["po_number"]
        po_bad = [r for r in hard["reasons"] if r in (REASON_PO_MISSING, REASON_PO_WRONG_VENDOR)]
        if po_bad:
            log_decision(
                conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
                decision="blocked", reason="Blocked: " + ", ".join(po_bad),
                inputs={"po_number": po_no},
            )
            conn.commit()
            return {"status": "blocked", "reasons": po_bad}
        if REASON_DELIVERY_NOT_RECEIVED in hard["reasons"]:
            if not (confirm_missing_delivery and approved_by):
                conn.commit()
                return {"status": "needs_confirmation",
                        "reasons": [REASON_DELIVERY_NOT_RECEIVED], "po_number": po_no}
            log_decision(
                conn, stage="pay", entity_type="invoice", entity_id=invoice_id,
                decision="confirmed_no_delivery", actor="human",
                reason=f"{approved_by} chose to pay although no delivery is recorded for {po_no}",
                inputs={"po_number": po_no},
            )
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
        "SELECT amount, currency FROM invoices WHERE id = %s",
        (invoice_id,),
    ).fetchone()
    amount, currency = Decimal(inv[0]), inv[1]
    email = payout_email(conn, invoice_id)

    pay = reserve_payment(
        conn, invoice_id=invoice_id, destination_email=email,
        amount=amount, currency=currency,
        allow_retry_failed=bool(approved_by),
    )
    conn.commit()

    # 3. take the payment row's lock. Of two simultaneous calls only one sees a sendable state;
    #    the other waits here, then finds the outcome and does not send again.
    locked = conn.execute(
        "SELECT status FROM payments WHERE id = %s FOR UPDATE", (pay["payment_id"],)
    ).fetchone()
    pay["status"] = locked[0]

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

    # 4. call PayPal while holding the lock, then record the answer in the same transaction
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
    if new_status == "failed":
        err = result.get("error")
        name = err.get("name") if isinstance(err, dict) else None
        add_exception(conn, pay["payment_id"], "payment_failed",
                      {"invoice_id": str(invoice_id), "paypal_error": name or str(err)[:200]})
    conn.commit()

    return {
        "status": new_status,
        "paypal_result": result["result"],
        "payment_id": pay["payment_id"],
        "paypal_batch_id": result.get("paypal_batch_id"),
    }
