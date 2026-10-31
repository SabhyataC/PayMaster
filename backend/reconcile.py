"""Reconcile: compare our payment records with PayPal and log exceptions."""
from backend.audit import log_decision
from backend.paypal_client import BASE, get_token
from backend.settle import fetch_batch, fetch_item, settlement_from_item

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import json
import requests

UNKNOWN_LIMIT = timedelta(minutes=30)
PENDING_LIMIT = timedelta(hours=1)
UNCLAIMED_LIMIT = timedelta(days=7)
PENDING_STATUSES = ("submitted", "pending")

# PayPal's Transaction Search can lag, so we don't judge payouts younger than this.
MIN_AGE_HOURS = 3


def add_exception(conn, payment_id, type_, detail):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO exceptions (payment_id, type, detail, company_id)
               VALUES (%s, %s, %s::jsonb, (SELECT company_id FROM payments WHERE id = %s))
               ON CONFLICT (payment_id, type) WHERE status = 'open' DO NOTHING
               RETURNING id""",
            (payment_id, type_, json.dumps(detail), payment_id),
        )
        return cur.fetchone() is not None

def detect_stuck(conn):
    """Payments stuck in one status for longer than its limit.
    Returns {exception_type: new_count}."""
    rules = [
        ("unknown_outcome", ("unknown",), UNKNOWN_LIMIT),
        ("pending_too_long", PENDING_STATUSES, PENDING_LIMIT),
        ("unclaimed_too_long", ("unclaimed",), UNCLAIMED_LIMIT),
    ]
    new_counts = {}
    for type_, statuses, limit in rules:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, invoice_id, status, paypal_batch_id,
                          EXTRACT(EPOCH FROM (now() - updated_at)) / 3600
                   FROM payments
                   WHERE status = ANY(%s) AND now() - updated_at > %s""",
                (list(statuses), limit),
            )
            rows = cur.fetchall()
        for pid, invoice_id, status, batch_id, age_hours in rows:
            detail = {
                "status": status,
                "age_hours": round(float(age_hours), 1),
                "invoice_id": str(invoice_id),
                "paypal_batch_id": batch_id,
            }
            if add_exception(conn, pid, type_, detail):
                new_counts[type_] = new_counts.get(type_, 0) + 1
    return new_counts

def detect_paypal_disagreements(conn):
    """For old 'success' payments, ask PayPal again and compare.
    Returns {exception_type: new_count}."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.id, p.invoice_id, p.paypal_item_id, p.amount, p.currency,
                      s.amount_sent, s.currency_sent,
                      s.amount_received, s.currency_received
               FROM payments p
               LEFT JOIN settlements s ON s.payment_id = p.id
               WHERE p.status = 'success'
                 AND p.paypal_item_id IS NOT NULL
                 AND now() - p.created_at > %s""",
            (timedelta(hours=MIN_AGE_HOURS),),
        )
        rows = cur.fetchall()

    new_counts = {}
    for (pid, invoice_id, item_id, amount, currency,
         sent, sent_cur, recv, recv_cur) in rows:
        try:
            detail = fetch_item(item_id)
        except Exception as e:
            print(f"reconcile: could not reach PayPal for payment {pid}: {e}")
            continue

        # 1) We say success. Does PayPal agree?
        paypal_status = detail.get("transaction_status")
        if paypal_status != "SUCCESS":
            info = {"our_status": "success", "paypal_status": paypal_status,
                    "paypal_item_id": item_id, "invoice_id": str(invoice_id)}
            if add_exception(conn, pid, "success_mismatch", info):
                new_counts["success_mismatch"] = new_counts.get("success_mismatch", 0) + 1
            continue  # status is wrong, so comparing amounts adds nothing

        # 2) Same status. Do the money facts agree with our settlement?
        theirs = settlement_from_item(detail)
        ours = {"amount_sent": sent, "currency_sent": sent_cur,
                "amount_received": recv, "currency_received": recv_cur}
        diffs = {}
        if sent is None:
            diffs["settlement"] = {"ours": "missing"}
        else:
            for k, our_v in ours.items():
                if our_v != theirs[k]:
                    diffs[k] = {"ours": str(our_v), "paypal": str(theirs[k])}
        # The payment itself should equal what the vendor received.
        if (amount, currency) != (theirs["amount_received"], theirs["currency_received"]):
            diffs["payment_vs_received"] = {
                "payment": f"{amount} {currency}",
                "paypal_received": f"{theirs['amount_received']} {theirs['currency_received']}"}
        if diffs:
            info = {"diffs": diffs, "paypal_item_id": item_id,
                    "invoice_id": str(invoice_id)}
            if add_exception(conn, pid, "amount_mismatch", info):
                new_counts["amount_mismatch"] = new_counts.get("amount_mismatch", 0) + 1
    return new_counts


PAYOUT_EVENT_CODES = ("T0001",)  # T0001 = the payout itself (T1503/T1105 are holds)

def add_orphan_exception(conn, txn_id, detail, company_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM exceptions
               WHERE type = 'unknown_payout' AND status = 'open'
                 AND detail->>'transaction_id' = %s AND company_id = %s""",
            (txn_id, company_id),
        )
        if cur.fetchone():
            return False
        cur.execute(
            """INSERT INTO exceptions (payment_id, type, detail, company_id)
               VALUES (NULL, 'unknown_payout', %s::jsonb, %s)""",
            (json.dumps(detail), company_id),
        )
        return True


def search_transactions(start, end):
    """All Transaction Search rows between two datetimes (handles paging)."""
    fmt = "%Y-%m-%dT%H:%M:%S+0000"
    rows, page = [], 1
    while True:
        r = requests.get(
            f"{BASE}/v1/reporting/transactions",
            headers={"Authorization": f"Bearer {get_token()}"},
            params={"start_date": start.strftime(fmt), "end_date": end.strftime(fmt),
                    "fields": "transaction_info", "page_size": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        rows += data.get("transaction_details", [])
        if page >= data.get("total_pages", 1):
            return rows
        page += 1


def detect_unknown_payouts(conn):
    """PayPal shows money leaving the account that we have no payment for."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(created_at) FROM payments")
        first = cur.fetchone()[0]
        cur.execute("SELECT paypal_item_id FROM payments WHERE paypal_item_id IS NOT NULL")
        item_ids = [row[0] for row in cur.fetchall()]
    if first is None:
        return {}

    start = first - timedelta(minutes=1)
    end = datetime.now(timezone.utc) - timedelta(hours=MIN_AGE_HOURS)
    if end <= start:
        return {}

    # Every PayPal transaction id we DO know about, taken from our own payments.
    known = set()
    for item_id in item_ids:
        try:
            known.add(fetch_item(item_id).get("transaction_id"))
        except Exception as e:
            # If we can't see all our own payments, guessing would cause false alarms.
            print(f"reconcile: skipping unknown_payout check, PayPal call failed: {e}")
            return {}

    new_counts = {}
    for t in search_transactions(start, end):
        info = t.get("transaction_info", {})
        amount = info.get("transaction_amount", {})
        txn_id = info.get("transaction_id")
        if info.get("transaction_event_code") not in PAYOUT_EVENT_CODES:
            continue
        if Decimal(str(amount.get("value", "0"))) >= 0:
            continue  # money coming in, not a payout
        if txn_id in known:
            continue
        detail = {"transaction_id": txn_id,
                  "amount": amount.get("value"), "currency": amount.get("currency_code"),
                  "date": info.get("transaction_initiation_date"),
                  "paypal_status": info.get("transaction_status")}
        if add_orphan_exception(conn, txn_id, detail):
            new_counts["unknown_payout"] = new_counts.get("unknown_payout", 0) + 1
    return new_counts


INVOICE_AFTER_RETURN = "approved"  # payable again; change to "held" for human review

def cancel_item(item_id):
    """Ask PayPal to cancel an unclaimed payout item (only works once the batch is SUCCESS)."""
    r = requests.post(
        f"{BASE}/v1/payments/payouts-item/{item_id}/cancel",
        headers={"Authorization": f"Bearer {get_token()}",
                 "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def handle_unclaimed(conn):
    """Cancel old unclaimed payouts; once PayPal says RETURNED, close the payment.
    Returns {"cancel_requested": n, "returned": n}."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, invoice_id, paypal_batch_id, paypal_item_id
               FROM payments
               WHERE status = 'unclaimed' AND paypal_item_id IS NOT NULL
                 AND now() - updated_at > %s""",
            (UNCLAIMED_LIMIT,),
        )
        rows = cur.fetchall()

    counts = {}
    for pid, invoice_id, batch_id, item_id in rows:
        # An unclaimed payment should have no ledger rows. If one does, a person must look.
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ledger_entries WHERE payment_id = %s LIMIT 1", (pid,))
            if cur.fetchone():
                print(f"reconcile: payment {pid} is unclaimed but has ledger rows, skipping")
                continue
        try:
            paypal_status = fetch_item(item_id).get("transaction_status")

            if paypal_status == "UNCLAIMED":
                batch_status = fetch_batch(batch_id).get("batch_header", {}).get("batch_status")
                if batch_status != "SUCCESS":
                    continue  # cancel only works on a finished batch
                cancel_item(item_id)
                log_decision(
                    conn, stage="reconcile", entity_type="payment", entity_id=pid,
                    decision="cancel_requested",
                    reason=f"Unclaimed for more than {UNCLAIMED_LIMIT.days} days; asked PayPal to cancel",
                    inputs={"batch_id": batch_id, "item_id": item_id},
                )
                counts["cancel_requested"] = counts.get("cancel_requested", 0) + 1

            elif paypal_status == "RETURNED":
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE payments SET status = 'returned', updated_at = now() "
                        "WHERE id = %s AND status = 'unclaimed'", (pid,))
                    # Normally the invoice is still 'approved' here, so this does nothing.
                    cur.execute(
                        "UPDATE invoices SET status = %s WHERE id = %s AND status = 'paid'",
                        (INVOICE_AFTER_RETURN, invoice_id))
                    cur.execute(
                        """UPDATE exceptions SET status = 'resolved', resolved_by = 'reconcile',
                                  resolved_at = now(), note = 'PayPal returned the payout'
                           WHERE payment_id = %s AND type = 'unclaimed_too_long'
                             AND status = 'open'""", (pid,))
                log_decision(
                    conn, stage="reconcile", entity_type="payment", entity_id=pid,
                    decision="returned",
                    reason="PayPal item RETURNED after cancel; payment closed, no money booked",
                    inputs={"batch_id": batch_id, "item_id": item_id},
                )
                counts["returned"] = counts.get("returned", 0) + 1
        except Exception as e:
            print(f"reconcile: could not handle unclaimed payment {pid}: {e}")
    return counts

def run_reconcile(conn, commit=True):
    """Check every payment against PayPal. Returns {exception_type: new_count}."""
    new_counts = {}
    for k, v in detect_stuck(conn).items():
        new_counts[k] = new_counts.get(k, 0) + v
    for k, v in detect_paypal_disagreements(conn).items():
        new_counts[k] = new_counts.get(k, 0) + v
    for k, v in detect_unknown_payouts(conn).items():
        new_counts[k] = new_counts.get(k, 0) + v
    for k, v in handle_unclaimed(conn).items():
        new_counts[k] = new_counts.get(k, 0) + v
    if commit:
        conn.commit()
    return new_counts