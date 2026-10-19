import uuid


def make_key(invoice_id) -> str:
    """Same invoice always gives the same key. This is what we send to PayPal."""
    return f"pm-{uuid.UUID(str(invoice_id)).hex}"


def reserve_payment(conn, *, invoice_id, destination_email, amount, currency="USD"):
    """
    Create the payment row for this invoice, or return the one that already exists.
    Safe to call any number of times: it never creates a second payment.
    """
    key = make_key(invoice_id)

    row = conn.execute(
        """
        INSERT INTO payments
            (invoice_id, idempotency_key, destination_email, amount, currency)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id, status
        """,
        (invoice_id, key, destination_email, amount, currency),
    ).fetchone()

    if row:  # we just created it
        return {"payment_id": row[0], "key": key, "status": row[1], "is_new": True}

    # a row with this key was already there, so fetch it instead
    row = conn.execute(
        "SELECT id, status FROM payments WHERE idempotency_key = %s", (key,)
    ).fetchone()
    return {"payment_id": row[0], "key": key, "status": row[1], "is_new": False}