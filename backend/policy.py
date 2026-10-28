from decimal import Decimal

# Above this amount, a human must approve. Placeholder: change it freely.
MAX_AUTO_PAY = Decimal("5000.00")

# Currencies we pay in. The auto-pay limit above is applied as a number in the invoice's own
# currency: we never convert amounts ourselves, only PayPal states real exchange rates.
ALLOWED_CURRENCIES = {"USD", "GBP", "EUR"}


def check_policy(conn, invoice_id) -> dict:
    """
    Decide if an invoice may be paid automatically.
    Returns {"outcome": "pass" | "hold" | "block", "reasons": [...], "inputs": {...}}
    """
    row = conn.execute(
        "SELECT status, amount, currency, destination_email FROM invoices WHERE id = %s",
        (invoice_id,),
    ).fetchone()

    if row is None:
        return {
            "outcome": "block",
            "reasons": ["invoice_not_found"],
            "inputs": {"invoice_id": str(invoice_id)},
        }

    status, amount, currency, email = row
    blocks, holds = [], []

    # --- block rules: never pay ---
    if status != "approved":
        blocks.append("invoice_not_approved")
    if not email:
        blocks.append("no_destination_email")
    if amount is None or amount <= 0:
        blocks.append("invalid_amount")
    if currency not in ALLOWED_CURRENCIES:
        blocks.append("currency_not_supported")

    # --- hold rules: a human looks first ---
    risk_row = conn.execute(
        "SELECT decision FROM audit_log WHERE entity_id = %s AND stage = 'fraud' "
        "AND reason LIKE 'Fraud risk %%' ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    fraud_risk = risk_row[0] if risk_row else None
    if fraud_risk == "high":
        holds.append("fraud_risk_high")

    if amount is not None and amount > MAX_AUTO_PAY:
        holds.append("amount_over_limit")

    new_payee = None
    if email:
        paid_before = conn.execute(
            """
            SELECT 1 FROM payments
            WHERE lower(destination_email) = lower(%s) AND status = 'success'
            LIMIT 1
            """,
            (email,),
        ).fetchone()
        new_payee = paid_before is None
        if new_payee:
            holds.append("new_payee")

    if blocks:
        outcome = "block"
    elif holds:
        outcome = "hold"
    else:
        outcome = "pass"

    return {
        "outcome": outcome,
        "reasons": blocks + holds,
        "inputs": {
            "invoice_id": str(invoice_id),
            "invoice_status": status,
            "amount": str(amount),
            "currency": currency,
            "destination_email": email,
            "max_auto_pay": str(MAX_AUTO_PAY),
            "new_payee": new_payee,
            "fraud_risk": fraud_risk,
        },
    }