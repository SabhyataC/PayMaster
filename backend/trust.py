"""Trust Graph: the evidence chain behind one invoice / payment.

    Vendor > Invoice > PO > Delivery > Approval > Payout > Webhook > Ledger > Reconcile

Read-only. Every node says what it found, whether that is OK, a PROBLEM (missing or mismatching) or
still PENDING (not reached yet), the record ids behind it, and whether it rests on a verified fact
(rows in our own database, checked by code) or an AI suggestion (Gemini's reading or opinion).
Nothing here changes data and nothing here asks an AI anything: it only reads what was recorded.
"""
from decimal import Decimal

ORDER = ["vendor", "invoice", "po", "delivery", "approval", "payout", "webhook", "ledger", "reconcile"]
TITLES = {"vendor": "Vendor", "invoice": "Invoice", "po": "Purchase order", "delivery": "Delivery",
          "approval": "Approval", "payout": "Payout", "webhook": "PayPal webhook", "ledger": "Ledger",
          "reconcile": "Reconcile"}


def _node(key, state, summary, kind="fact", problems=None, facts=None, records=None, ai=None):
    return {"key": key, "title": TITLES[key], "state": state, "kind": kind, "summary": summary,
            "problems": problems or [], "facts": [{"label": k, "value": str(v)} for k, v in (facts or [])],
            "records": [{"type": t, "id": str(i)} for t, i in (records or []) if i is not None],
            "ai": ai}


def _money(v):
    return None if v is None else Decimal(str(v))


def _evidence(verify):
    ev = (((verify or {}).get("inputs") or {}).get("verdict") or {}).get("evidence") or []
    return {e["check"]: e for e in ev if isinstance(e, dict) and "check" in e}


def _latest_audit(conn, invoice_id, stage):
    row = conn.execute(
        "SELECT id, decision, reason, inputs, actor, created_at FROM audit_log "
        "WHERE entity_id = %s AND stage = %s ORDER BY id DESC LIMIT 1", (invoice_id, stage)).fetchone()
    if not row:
        return None
    return {"id": row[0], "decision": row[1], "reason": row[2], "inputs": row[3] or {}, "actor": row[4],
            "at": row[5].isoformat()}


def build_chain(conn, invoice_id, company_id, payment_id=None):
    """Returns the chain for an invoice of this company, or None if it is not found.
    payment_id picks one payment; default is the most recent one."""
    inv = conn.execute(
        """SELECT i.id, i.invoice_number, i.amount, i.currency, i.destination_email, i.status, i.vendor_id,
                  i.po_id, i.quantity, i.received_at, v.name, v.deactivated_at
           FROM invoices i JOIN vendors v ON v.id = i.vendor_id AND v.company_id = i.company_id
           WHERE i.id = %s AND i.company_id = %s""", (invoice_id, company_id)).fetchone()
    if not inv:
        return None
    (iid, number, amount, currency, inv_email, status, vendor_id, po_id, inv_qty, received_at,
     vendor_name, vendor_deactivated) = inv
    amount = _money(amount)
    verify = _latest_audit(conn, iid, "verify")
    ev = _evidence(verify)
    nodes = {}

    # ---- Vendor ------------------------------------------------------------------------------
    cur = conn.execute("SELECT id, destination_email, valid_from, change_source FROM vendor_bank_history "
                       "WHERE vendor_id = %s AND valid_to IS NULL", (vendor_id,)).fetchone()
    n_hist = conn.execute("SELECT count(*) FROM vendor_bank_history WHERE vendor_id = %s", (vendor_id,)).fetchone()[0]
    problems = []
    if vendor_deactivated:
        problems.append("This vendor has been deactivated")
    if not cur:
        problems.append("No payout email on file for this vendor")
    nodes["vendor"] = _node(
        "vendor", "problem" if problems else "ok",
        f"{vendor_name}: payout email on file" if not problems else "; ".join(problems), problems=problems,
        facts=[("Vendor", vendor_name), ("Payout email on file", cur[1] if cur else "none"),
               ("Source of that email", cur[3] if cur else "-"), ("Changes on record", max(n_hist - 1, 0))],
        records=[("vendor", vendor_id), ("bank_history", cur[0] if cur else None)])

    # ---- Invoice (read by AI, then checked by code) ---------------------------------------------
    ext = conn.execute("SELECT id, confidence, source_name FROM extractions WHERE invoice_id = %s "
                       "ORDER BY created_at DESC LIMIT 1", (iid,)).fetchone() \
        if _has_col(conn, "extractions", "created_at") else \
        conn.execute("SELECT id, confidence, source_name FROM extractions WHERE invoice_id = %s LIMIT 1", (iid,)).fetchone()
    problems = []
    if "not_duplicate" in ev and not ev["not_duplicate"]["passed"]:
        problems.append(ev["not_duplicate"]["detail"])
    if "bank_unchanged" in ev and not ev["bank_unchanged"]["passed"]:
        problems.append(ev["bank_unchanged"]["detail"])
    if cur and inv_email and inv_email.strip().lower() != cur[1].strip().lower() \
            and not any("bank" in p.lower() or "email" in p.lower() for p in problems):
        problems.append(f"Invoice payout email {inv_email} differs from the one on file ({cur[1]})")
    state = "problem" if problems else ("ok" if verify else "pending")
    nodes["invoice"] = _node(
        "invoice", state,
        (problems[0] if problems else (f"Invoice {number} for {amount} {currency}, checked against our records"
                                       if verify else "Not verified yet")),
        kind="ai" if ext else "fact", problems=problems,
        facts=[("Invoice number", number), ("Amount", f"{amount} {currency}"),
               ("Payout email on the invoice", inv_email or "none"), ("Status", status)]
              + ([("Read by AI from", ext[2]), ("AI reading confidence", ext[1])] if ext else []),
        records=[("invoice", iid), ("extraction", ext[0] if ext else None),
                 ("audit_log", verify["id"] if verify else None)])

    # ---- PO ------------------------------------------------------------------------------------
    po = conn.execute("SELECT id, po_number, amount, currency, status, quantity FROM purchase_orders "
                      "WHERE id = %s AND company_id = %s", (po_id, company_id)).fetchone() if po_id else None
    problems = []
    if not po:
        problems.append("The invoice does not point at any purchase order")
    else:
        if po[4] == "cancelled":
            problems.append("The purchase order was cancelled")
        for name in ("po_exists", "po_vendor_match", "amount_within_tolerance"):
            if name in ev and not ev[name]["passed"]:
                problems.append(ev[name]["detail"])
        if _money(po[2]) is not None and amount is not None and amount > _money(po[2]) * Decimal("1.02") and not problems:
            problems.append(f"Invoice {amount} is above the PO amount {po[2]}")
    nodes["po"] = _node(
        "po", "problem" if problems else "ok",
        problems[0] if problems else f"PO {po[1]}: {po[2]} {po[3]} for this vendor",
        problems=problems,
        facts=([("PO", po[1]), ("PO amount", f"{po[2]} {po[3]}"), ("PO quantity", po[5] if po[5] is not None else "not stated")]
               if po else []),
        records=[("purchase_order", po[0] if po else None)])

    # ---- Delivery --------------------------------------------------------------------------------
    problems, facts, recs = [], [], []
    if not po:
        state, summary = "problem", "No PO, so there is nothing to compare a delivery with"
        problems.append(summary)
    else:
        drows = conn.execute("SELECT id, amount, quantity FROM deliveries WHERE po_id = %s AND company_id = %s",
                             (po[0], company_id)).fetchall()
        delivered = sum((_money(r[1]) for r in drows), Decimal("0"))
        dq = [_money(r[2]) for r in drows if r[2] is not None]
        recs = [("delivery", r[0]) for r in drows]
        facts = [("Deliveries recorded", len(drows)), ("Value delivered", delivered),
                 ("Quantity delivered", sum(dq, Decimal("0")) if dq else "not stated")]
        if not drows:
            problems.append("No delivery has been recorded for this PO")
        elif delivered < amount:
            problems.append(f"Only {delivered} delivered, but the invoice is {amount}")
        for name in ("delivery_exists", "quantity_within_ordered"):
            e = ev.get(name)
            if e and not e["passed"] and e["detail"] not in problems:
                problems.append(e["detail"])
        state = "problem" if problems else "ok"
        summary = problems[0] if problems else f"{delivered} delivered against an invoice of {amount}"
    nodes["delivery"] = _node("delivery", state, summary, problems=problems, facts=facts, records=recs)

    # ---- Approval ----------------------------------------------------------------------------------
    review = conn.execute(
        "SELECT id, reviewer, human_decision, note, confirmed_with_vendor, status_before, status_after, created_at "
        "FROM reviews WHERE invoice_id = %s ORDER BY created_at DESC LIMIT 1", (iid,)).fetchone()
    vin = (verify or {}).get("inputs") or {}
    ai = None
    if verify:
        ai = {"suggestion": vin.get("agent_decision") or (vin.get("verdict") or {}).get("decision"),
              "steps": vin.get("steps"), "model": vin.get("model"),
              "note": "The AI only suggests. Code checks and people decide."}
    fired = [s for s in (vin.get("fraud") or {}).get("signals", [])
             if isinstance(s, dict) and s.get("fired")] if isinstance(vin.get("fraud"), dict) else []
    facts = [("Verify decision", (verify or {}).get("decision", "not run")),
             ("Reasons", (verify or {}).get("reason", "-"))]
    problems = []
    if review:
        who = f"{review[1]}: {review[3]}"
        if review[2] == "approve":
            state, summary = "ok", f"Approved by {review[1]}"
        else:
            state, summary = "problem", f"Rejected by {review[1]}"
            problems.append(summary)
        facts += [("Person", review[1]), ("Their note", review[3]), ("Confirmed with vendor", review[4])]
    elif not verify:
        state, summary = "pending", "Not verified yet"
    elif status == "approved":
        state, summary = "ok", "Approved: every hard check passed (a code decision, not the AI's)"
    elif status == "rejected":
        state, summary = "problem", f"Rejected by the checks: {verify['reason']}"
        problems.append(summary)
    elif status == "held":
        state, summary = "pending", f"Held for a person: {verify['reason']}"
    else:
        state, summary = "ok", f"Status {status}"
    nodes["approval"] = _node("approval", state, summary, problems=problems, facts=facts, ai=ai,
                              records=[("review", review[0] if review else None),
                                       ("audit_log", verify["id"] if verify else None)])

    # ---- Payout ----------------------------------------------------------------------------------------
    if payment_id:
        pay = conn.execute("SELECT id, status, amount, currency, destination_email, paypal_batch_id, created_at "
                           "FROM payments WHERE id::text = %s AND invoice_id = %s", (str(payment_id), iid)).fetchone()
    else:
        pay = conn.execute("SELECT id, status, amount, currency, destination_email, paypal_batch_id, created_at "
                           "FROM payments WHERE invoice_id = %s ORDER BY created_at DESC LIMIT 1", (iid,)).fetchone()
    if payment_id and not pay:
        return None
    problems = []
    if not pay:
        nodes["payout"] = _node("payout", "pending", "Not paid yet")
    else:
        pid, pstatus, pamount, pcur, pemail, batch, pcreated = pay
        pamount = _money(pamount)
        valid = conn.execute(
            "SELECT 1 FROM vendor_bank_history WHERE vendor_id = %s AND lower(destination_email) = lower(%s) "
            "AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)", (vendor_id, pemail, pcreated, pcreated)).fetchone()
        if not valid:
            problems.append(f"The payout went to {pemail}, which was not the vendor's email on file at that time")
        if pamount != amount or pcur != currency:
            problems.append(f"Paid {pamount} {pcur}, but the invoice says {amount} {currency}")
        if pstatus in ("failed", "unknown", "unclaimed"):
            problems.append(f"PayPal status: {pstatus}")
        pending = pstatus in ("created", "submitted", "pending")
        state = "problem" if problems else ("pending" if pending else "ok")
        nodes["payout"] = _node(
            "payout", state,
            problems[0] if problems else (f"{pamount} {pcur} to {pemail}: {pstatus}"),
            problems=problems,
            facts=[("Amount", f"{pamount} {pcur}"), ("Sent to", pemail), ("Status", pstatus),
                   ("PayPal batch", batch or "none yet")],
            records=[("payment", pid)])

    # ---- Webhook ----------------------------------------------------------------------------------------
    if not pay:
        nodes["webhook"] = _node("webhook", "pending", "No payout yet")
    elif not pay[5]:
        nodes["webhook"] = _node("webhook", "pending", "PayPal has not given a batch id yet")
    else:
        evs = conn.execute("SELECT event_id, event_type, attempts, processed_at, result FROM webhook_events "
                           "WHERE batch_id = %s ORDER BY received_at", (pay[5],)).fetchall()
        age_min = conn.execute("SELECT extract(epoch FROM now() - %s) / 60", (pay[6],)).fetchone()[0]
        if not evs:
            old = age_min is not None and age_min > 10 and pay[1] in ("success", "unclaimed", "failed")
            nodes["webhook"] = _node(
                "webhook", "problem" if old else "pending",
                "No webhook from PayPal was recorded for this payout" if old else "Waiting for PayPal's webhook",
                problems=["No webhook was recorded"] if old else [], facts=[("Batch", pay[5])])
        else:
            unprocessed = [e for e in evs if e[3] is None]
            nodes["webhook"] = _node(
                "webhook", "problem" if unprocessed else "ok",
                f"{len(evs)} webhook event(s) recorded and processed" if not unprocessed
                else "A webhook arrived but was not processed",
                problems=["A webhook was recorded but never processed"] if unprocessed else [],
                facts=[("Events", ", ".join(e[1] for e in evs)),
                       ("Delivered more than once (replays ignored)", sum(max(e[2] - 1, 0) for e in evs))],
                records=[("webhook_event", e[0]) for e in evs])

    # ---- Ledger ---------------------------------------------------------------------------------------
    if not pay:
        nodes["ledger"] = _node("ledger", "pending", "No payout yet")
    else:
        rows = conn.execute("SELECT id, entry_type, account, direction, amount, currency FROM ledger_entries "
                            "WHERE payment_id = %s ORDER BY created_at, id", (pay[0],)).fetchall()
        if not rows:
            settled = pay[1] == "success"
            nodes["ledger"] = _node("ledger", "problem" if settled else "pending",
                                    "The payout succeeded but no ledger entries exist" if settled
                                    else "Ledger entries are written once PayPal confirms the payout",
                                    problems=["No ledger entries for a successful payout"] if settled else [])
        else:
            tot = {}
            for _, _, _, d, a, c in rows:
                tot[c] = tot.get(c, Decimal("0")) + (_money(a) if d == "debit" else -_money(a))
            unbalanced = {c: v for c, v in tot.items() if v != 0}
            paid = sum((_money(a) for _, t, ac, d, a, c in rows if t == "payment" and d == "debit"), Decimal("0"))
            problems = []
            if unbalanced:
                problems.append(f"Debits and credits do not match: {({c: str(v) for c, v in unbalanced.items()})}")
            if paid != _money(pay[2]):
                problems.append(f"The ledger records {paid} paid, but the payout was {pay[2]}")
            nodes["ledger"] = _node(
                "ledger", "problem" if problems else "ok",
                problems[0] if problems else f"{len(rows)} entries, debits equal credits, amount matches the payout",
                problems=problems, facts=[("Entries", len(rows)), ("Recorded as paid", paid)],
                records=[("ledger_entry", r[0]) for r in rows])

    # ---- Reconcile ----------------------------------------------------------------------------------------
    if not pay:
        nodes["reconcile"] = _node("reconcile", "pending", "No payout yet")
    else:
        st = conn.execute("SELECT id, amount_sent, currency_sent, amount_received, fee_amount FROM settlements "
                          "WHERE payment_id = %s", (pay[0],)).fetchone()
        exc = conn.execute("SELECT id, type, status FROM exceptions WHERE payment_id = %s ORDER BY id", (pay[0],)).fetchall()
        open_exc = [e for e in exc if e[2] == "open"]
        problems = [f"Open exception: {e[1]}" for e in open_exc]
        if open_exc:
            state, summary = "problem", problems[0]
        elif st:
            state = "ok"
            summary = (f"PayPal settled {st[3]} to the vendor, fee {st[4] or 0}; "
                       + (f"{len(exc)} past exception(s) resolved" if exc else "no exceptions"))
        else:
            state, summary = "pending", "Waiting for PayPal's settlement details"
        nodes["reconcile"] = _node(
            "reconcile", state, summary, problems=problems,
            facts=[("Settlement", f"{st[1]} {st[2]} sent, {st[3]} received" if st else "none yet"),
                   ("Exceptions", ", ".join(f"{e[1]} ({e[2]})" for e in exc) or "none")],
            records=[("settlement", st[0] if st else None)] + [("exception", e[0]) for e in exc])

    chain = [nodes[k] for k in ORDER]
    counts = {s: sum(1 for n in chain if n["state"] == s) for s in ("ok", "problem", "pending")}
    verdict = "broken" if counts["problem"] else ("complete" if not counts["pending"] else "incomplete")
    return {"invoice_id": str(iid), "invoice_number": number, "vendor": vendor_name, "amount": str(amount),
            "currency": currency, "status": status, "payment_id": str(pay[0]) if pay else None,
            "verdict": verdict, "counts": counts, "chain": chain,
            "legend": {"fact": "Verified fact: rows in our database, checked by code",
                       "ai": "AI suggestion: read or suggested by Gemini, then checked by code"}}


_col_cache = {}


def _has_col(conn, table, col):
    key = (table, col)
    if key not in _col_cache:
        _col_cache[key] = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            (table, col)).fetchone() is not None
    return _col_cache[key]
