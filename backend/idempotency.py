import uuid

# Only a returned payout (money came back to us) may be paid again automatically.
# 'failed' is left out on purpose: a person should look at why PayPal refused it.
REPAYABLE = ("returned",)


def make_key(invoice_id, attempt=1) -> str:
    """Same invoice and attempt always give the same key. This is what we send to PayPal.
    Attempt 1 keeps the old format, so existing payments are unchanged."""
    base = f"pm-{uuid.UUID(str(invoice_id)).hex}"
    return base if attempt == 1 else f"{base}-{attempt}"


def reserve_payment(conn, *, invoice_id, destination_email, amount, currency="USD"):
    """
    Create the payment row for this invoice, or return the one that already exists.
    Safe to call any number of times. A new attempt is created only when the latest
    payment for the invoice was returned.
    """
    latest = conn.execute(
        """
        SELECT id, status, attempt, idempotency_key
        FROM payments WHERE invoice_id = %s
        ORDER BY attempt DESC LIMIT 1
        """,
        (invoice_id,),
    ).fetchone()

    if latest is None:
        attempt = 1
    elif latest[1] in REPAYABLE:
        attempt = latest[2] + 1
    else:  # in progress or finished: never create a second payment
        return {"payment_id": latest[0], "key": latest[3],
                "status": latest[1], "is_new": False}

    key = make_key(invoice_id, attempt)

    row = conn.execute(
        """
        INSERT INTO payments
            (invoice_id, idempotency_key, destination_email, amount, currency, attempt)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id, status
        """,
        (invoice_id, key, destination_email, amount, currency, attempt),
    ).fetchone()

    if row:  # we just created it
        return {"payment_id": row[0], "key": key, "status": row[1], "is_new": True}

    # another request created it first, so fetch it instead
    row = conn.execute(
        "SELECT id, status FROM payments WHERE idempotency_key = %s", (key,)
    ).fetchone()
    return {"payment_id": row[0], "key": key, "status": row[1], "is_new": False}