"""Hard checks: plain code, no AI. They produce facts the agent cannot argue with.

Quantity: the deliveries table has no quantity column, so quantity is not checked.
Only amounts are compared.
"""
from datetime import datetime
from decimal import Decimal

from backend.tools import (
    _query, find_similar_invoices, get_deliveries, get_po, get_vendor_bank_history,
    normalize_invoice_number,
)
from backend.verdict import (
    REASON_ALL_PASSED, REASON_AMOUNT_OUT_OF_TOLERANCE, REASON_BANK_CHANGED,
    REASON_DELIVERY_NOT_RECEIVED, REASON_DUPLICATE, REASON_PO_MISSING,
    REASON_PO_WRONG_VENDOR, make_evidence, make_verdict,
)

AMOUNT_TOLERANCE = Decimal("0.02")  # placeholder: invoice may be 2% above what the PO/delivery allows

# What each failed check leads to. Change these two sets if you disagree with my defaults.
REJECT_REASONS = {REASON_PO_MISSING, REASON_PO_WRONG_VENDOR, REASON_DUPLICATE}
HOLD_REASONS = {REASON_DELIVERY_NOT_RECEIVED, REASON_AMOUNT_OUT_OF_TOLERANCE, REASON_BANK_CHANGED}


def _load_invoice(invoice_id):
    rows = _query(
        """SELECT i.id, i.vendor_id, i.invoice_number, i.amount, i.currency,
                  i.destination_email, i.status, i.received_at, po.po_number
           FROM invoices i
           LEFT JOIN purchase_orders po ON po.id = i.po_id
           WHERE i.id = %s""",
        (invoice_id,),
    )
    if not rows:
        raise ValueError(f"invoice {invoice_id} not found")
    return rows[0]


def run_hard_checks(invoice_id):
    """Run every check. Returns {'invoice': ..., 'evidence': [...], 'reasons': [...]}."""
    inv = _load_invoice(invoice_id)
    evidence, reasons = [], []

    def record(ev, reason):
        evidence.append(ev)
        if not ev["passed"]:
            reasons.append(reason)

    # 1. PO exists
    po = get_po(inv["po_number"]) if inv["po_number"] else None
    record(
        make_evidence("po_exists", po is not None,
                      f"PO {po['po_number']} exists" if po else "Invoice has no purchase order",
                      {"po_number": inv["po_number"]}),
        REASON_PO_MISSING,
    )

    if po:
        # 2. PO belongs to this vendor
        same_vendor = po["vendor_id"] == inv["vendor_id"]
        record(
            make_evidence("po_vendor_match", same_vendor,
                          "PO belongs to the invoicing vendor" if same_vendor
                          else f"PO belongs to {po['vendor_name']}, not the invoicing vendor",
                          {"po_vendor_id": po["vendor_id"], "invoice_vendor_id": inv["vendor_id"]}),
            REASON_PO_WRONG_VENDOR,
        )

        # 3. Delivery exists (missing = HOLD, never reject)
        deliv = get_deliveries(po["po_number"])
        has_delivery = deliv["count"] > 0
        record(
            make_evidence("delivery_exists", has_delivery,
                          f"{deliv['count']} delivery(ies) recorded" if has_delivery
                          else "No delivery recorded yet (not received yet)",
                          {"count": deliv["count"], "total_delivered": deliv["total_delivered"]}),
            REASON_DELIVERY_NOT_RECEIVED,
        )

        # 4. Amount within tolerance. A PO's budget is shared by all its invoices, so
        #    what EARLIER invoices already billed on this PO is subtracted first.
        #    Same-number copies are skipped (the duplicate check handles those), and
        #    rejected invoices are skipped (they will never be paid).
        this_time = datetime.fromisoformat(inv["received_at"])
        others = _query(
            """SELECT invoice_number, amount, received_at FROM invoices
               WHERE po_id = %s AND id <> %s AND status <> 'rejected'""",
            (po["id"], inv["id"]),
        )
        earlier = [
            o for o in others
            if datetime.fromisoformat(o["received_at"]) < this_time
            and normalize_invoice_number(o["invoice_number"])
            != normalize_invoice_number(inv["invoice_number"])
        ]
        already_billed = sum((Decimal(o["amount"]) for o in earlier), Decimal("0"))

        inv_amt, po_amt = Decimal(inv["amount"]), Decimal(po["amount"])
        limit = po_amt
        delivered = Decimal(deliv["total_delivered"]) if has_delivery else None
        if delivered is not None:
            limit = min(limit, delivered)
        allowed = limit * (1 + AMOUNT_TOLERANCE) - already_billed
        currency_ok = inv["currency"] == po["currency"]
        amount_ok = currency_ok and inv_amt <= allowed
        if not currency_ok:
            detail = f"Invoice currency {inv['currency']} differs from PO currency {po['currency']}"
        elif amount_ok:
            detail = f"Invoice {inv_amt} fits within the allowed {allowed:.2f}"
        else:
            detail = (f"Invoice {inv_amt} is above the allowed {allowed:.2f} "
                      f"(already billed on this PO: {already_billed})")
        record(
            make_evidence("amount_within_tolerance", amount_ok, detail,
                          {"invoice_amount": str(inv_amt), "po_amount": str(po_amt),
                           "delivered_total": None if delivered is None else str(delivered),
                           "already_billed": str(already_billed),
                           "allowed_max": f"{allowed:.2f}", "tolerance": str(AMOUNT_TOLERANCE)}),
            REASON_AMOUNT_OUT_OF_TOLERANCE,
        )
    # 5. Duplicate: same normalized number from the same vendor that arrived EARLIER
    #    (or is already approved/paid). Without the "earlier" rule the original would be
    #    flagged as a duplicate of its own copy.
    this_time = datetime.fromisoformat(inv["received_at"])
    dupes = [
        m for m in find_similar_invoices(inv["vendor_id"], inv["invoice_number"],
                                         exclude_invoice_id=inv["id"])
        if "same_number" in m["match_reasons"]
        and (datetime.fromisoformat(m["received_at"]) < this_time
             or m["status"] in ("approved", "paid"))
    ]
    record(
        make_evidence("not_duplicate", not dupes,
                      "No earlier invoice with this number" if not dupes
                      else f"Same invoice number already seen: {dupes[0]['invoice_number']}",
                      {"matches": [{"id": d["id"], "invoice_number": d["invoice_number"],
                                    "status": d["status"]} for d in dupes]}),
        REASON_DUPLICATE,
    )

    # 6. Bank details unchanged: invoice's payout email vs the vendor's current one on file
    history = get_vendor_bank_history(inv["vendor_id"])
    current = next((h for h in history if h["valid_to"] is None), None)
    stated = inv["destination_email"]
    if current is None:
        bank_ok, detail = False, "No payout destination on file for this vendor"
    elif stated is None:
        bank_ok, detail = True, "Invoice states no payout email; the one on file will be used"
    elif stated.strip().lower() == current["destination_email"].strip().lower():
        bank_ok, detail = True, "Payout email matches the one on file"
    else:
        bank_ok, detail = False, (f"Invoice payout email {stated} differs from "
                                  f"the one on file {current['destination_email']}")
    record(
        make_evidence("bank_unchanged", bank_ok, detail,
                      {"stated": stated, "on_file": current["destination_email"] if current else None}),
        REASON_BANK_CHANGED,
    )

    return {"invoice": inv, "evidence": evidence, "reasons": reasons}


def hard_check_verdict(invoice_id):
    """Turn the check results into one verdict in the shared format."""
    result = run_hard_checks(invoice_id)
    reasons, evidence = result["reasons"], result["evidence"]
    if any(r in REJECT_REASONS for r in reasons):
        return make_verdict("reject", reasons, evidence, 1.0)   # decided by facts
    if reasons:
        return make_verdict("hold", reasons, evidence, 1.0)
    # Nothing failed, but the agent and fraud signals still have their say later.
    return make_verdict("approve", [REASON_ALL_PASSED], evidence, 0.8)  # 0.8 = placeholder