"""Structured investigation report for one invoice. Read-only, built only from what was recorded.

Every claim carries the ids of the records it rests on, and says what kind of basis it has:
  code_check   a hard check run by code        fraud_signal  a fraud signal (code or AI-assisted)
  ai           the AI's own opinion            gate          code overruled or capped the AI
  human        a named person's decision       tool          something the investigator looked up
Nothing here asks an AI anything. Text typed by people or written by the AI is passed through as
data and flagged `untrusted`, so the console shows it escaped and never treats it as instructions.
"""
from datetime import datetime, timezone
from decimal import Decimal

BASIS_LABEL = {"code_check": "Code check", "fraud_signal": "Fraud signal", "ai": "AI opinion",
               "gate": "Code overruled the AI", "human": "Person's decision", "tool": "Investigator lookup"}


def _rec(t, i):
    return {"type": t, "id": str(i)} if i is not None else None


def _recs(*items):
    out, seen = [], set()
    for r in items:
        if r and (r["type"], r["id"]) not in seen:
            seen.add((r["type"], r["id"]))
            out.append(r)
    return out


def _verify_row(conn, invoice_id):
    row = conn.execute("SELECT id, decision, reason, inputs, created_at FROM audit_log "
                       "WHERE entity_id = %s AND stage = 'verify' ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    return {"id": row[0], "decision": row[1], "reason": row[2], "inputs": row[3] or {}, "at": row[4].isoformat()} if row else None


def _unwrap(out):
    """Trail outputs are {"result": ...} or {"error": ...}."""
    if isinstance(out, dict) and "error" in out and "result" not in out:
        return None, str(out["error"])
    if isinstance(out, dict) and "result" in out:
        return out["result"], None
    return out, None


def _step(tool, args, raw):
    """One investigator lookup in plain words, with the record ids it touched."""
    recs, finding = [], ""
    out, error = _unwrap(raw)
    if error:
        return {"tool": tool, "args": {k: v for k, v in (args or {}).items() if k != "company_id"},
                "finding": f"The lookup failed: {error[:160]}", "records": []}
    if out is None:
        out = {}
    if tool == "get_po":
        if out:
            finding = f"PO {out.get('po_number')}: {out.get('amount')} {out.get('currency')} for {out.get('vendor_name')}"
            recs = [_rec("purchase_order", out.get("id")), _rec("vendor", out.get("vendor_id"))]
        else:
            finding = "No such purchase order"
    elif tool == "get_deliveries":
        ds = out.get("deliveries") or []
        finding = f"{out.get('count', 0)} delivery record(s), {out.get('total_delivered', '0')} delivered in total"
        recs = [_rec("delivery", d.get("id")) for d in ds]
    elif tool == "get_vendor_bank_history":
        rows = out if isinstance(out, list) else []
        cur = next((r for r in rows if r.get("valid_to") is None), None)
        finding = f"{len(rows)} payout destination(s) on record; current: {cur['destination_email'] if cur else 'none'}"
        recs = [_rec("bank_history", r.get("id")) for r in rows]
    elif tool == "find_similar_invoices":
        rows = out if isinstance(out, list) else []
        finding = (f"{len(rows)} similar invoice(s): "
                   + "; ".join(f"{r.get('invoice_number')} ({', '.join(r.get('match_reasons', []))}, "
                               f"{r.get('arrived', r.get('status'))})" for r in rows[:5])) if rows else "No similar invoices"
        recs = [_rec("invoice", r.get("id")) for r in rows]
    elif tool == "search_vendors":
        rows = out if isinstance(out, list) else []
        finding = (f"{len(rows)} vendor name(s) close to this one: "
                   + "; ".join(f"{r.get('name')} ({r.get('score')})" for r in rows[:5])) if rows else "No look-alike vendor names"
        recs = [_rec("vendor", r.get("id")) for r in rows]
    elif tool == "find_similar_precedents":
        rows = out if isinstance(out, list) else []
        finding = f"{len(rows)} earlier case(s) with the same vendor and the same signals"
        recs = [_rec("review", r.get("review_id")) for r in rows] + [_rec("invoice", r.get("invoice_id")) for r in rows]
    else:
        finding = "Lookup completed"
    return {"tool": tool, "args": {k: v for k, v in (args or {}).items() if k != "company_id"},
            "finding": finding, "records": _recs(*recs)}


def build_report(conn, invoice_id, company_id):
    inv = conn.execute(
        "SELECT i.id, i.invoice_number, i.amount, i.currency, i.status, i.vendor_id, i.po_id, v.name "
        "FROM invoices i JOIN vendors v ON v.id = i.vendor_id AND v.company_id = i.company_id "
        "WHERE i.id = %s AND i.company_id = %s", (invoice_id, company_id)).fetchone()
    if not inv:
        return None
    iid, number, amount, currency, status, vendor_id, po_id, vendor_name = inv
    verify = _verify_row(conn, iid)
    base = [_rec("invoice", iid)]
    if not verify:
        return {"invoice_id": str(iid), "invoice_number": number, "vendor": vendor_name, "status": status,
                "verified": False, "summary": "Verify has not run on this invoice yet.", "claims": [], "steps": [],
                "precedents": [], "ai": None, "human": None}
    vin = verify["inputs"]
    verdict = vin.get("verdict") or {}
    deliveries = [r[0] for r in conn.execute("SELECT id FROM deliveries WHERE po_id = %s AND company_id = %s",
                                             (po_id, company_id)).fetchall()] if po_id else []
    bank = [r[0] for r in conn.execute("SELECT id FROM vendor_bank_history WHERE vendor_id = %s AND company_id = %s",
                                       (vendor_id, company_id)).fetchall()]
    fraud_rows = {}
    for rid, reason in conn.execute("SELECT id, reason FROM audit_log WHERE entity_id = %s AND stage = 'fraud' "
                                    "ORDER BY id", (iid,)).fetchall():
        fraud_rows.setdefault(reason.split(":")[0], rid)
    src = _rec("audit_log", verify["id"])

    claims = []

    def add(basis, claim, supported, recs, detail=None, untrusted=False):
        claims.append({"n": len(claims) + 1, "basis": basis, "basis_label": BASIS_LABEL[basis], "claim": claim,
                       "supported": supported, "records": _recs(*recs, src), "detail": detail, "untrusted": untrusted})

    for e in verdict.get("evidence") or []:
        chk, passed, detail, data = e.get("check"), bool(e.get("passed")), e.get("detail", ""), e.get("data") or {}
        if chk in ("po_exists", "po_vendor_match", "amount_within_tolerance", "quantity_within_ordered"):
            add("code_check", detail, passed, base + [_rec("purchase_order", po_id)])
        elif chk == "delivery_exists":
            add("code_check", detail, passed, base + [_rec("purchase_order", po_id)] + [_rec("delivery", d) for d in deliveries])
        elif chk == "not_duplicate":
            add("code_check", detail, passed, base + [_rec("invoice", m.get("id")) for m in data.get("matches", [])])
        elif chk == "bank_unchanged":
            add("code_check", detail, passed, base + [_rec("vendor", vendor_id)] + [_rec("bank_history", b) for b in bank])
        elif chk.startswith("fraud_signal:"):
            name = chk.split(":", 1)[1]
            add("fraud_signal", detail, False, base + [_rec("vendor", vendor_id), _rec("audit_log", fraud_rows.get(name))])
        elif chk == "agent_summary":
            add("ai", f"AI's own summary: {detail}" if detail else "AI gave no summary", passed, base, untrusted=True,
                detail=f"AI said: {vin.get('agent_decision') or verdict.get('decision')}")
        elif chk in ("safety_gate", "fraud_gate", "reject_cap"):
            add("gate", detail, False, base)
        elif chk == "investigator_error":
            add("gate", f"The AI investigator failed, so the code decision stands: {detail[:200]}", False, base)
    review = conn.execute("SELECT id, reviewer, human_decision, note, confirmed_with_vendor, created_at FROM reviews "
                          "WHERE invoice_id = %s ORDER BY created_at DESC LIMIT 1", (iid,)).fetchone()
    human = None
    if review:
        human = {"reviewer": review[1], "decision": review[2], "note": review[3], "confirmed_with_vendor": review[4],
                 "at": review[5].isoformat(), "records": _recs(_rec("review", review[0]), *base)}
        add("human", f"{review[1]} chose to {review[2]}: {review[3]}", review[2] == "approve",
            base + [_rec("review", review[0])], untrusted=True)

    steps = [_step(t.get("tool"), t.get("args"), t.get("output")) for t in (vin.get("trail") or []) if isinstance(t, dict)]
    precedents = []
    for t in vin.get("trail") or []:
        res = _unwrap(t.get("output"))[0] if isinstance(t, dict) else None
        if isinstance(t, dict) and t.get("tool") == "find_similar_precedents" and isinstance(res, list):
            for r in res:
                precedents.append({
                    "invoice_number": r.get("invoice_number"), "ai_decision": r.get("ai_decision"),
                    "human_decision": r.get("human_decision"), "reviewer": r.get("reviewer"), "outcome": r.get("outcome"),
                    "signals": r.get("signals"), "note": r.get("note"), "untrusted": True,
                    "reviewed_at": str(r.get("reviewed_at")),
                    "records": _recs(_rec("review", r.get("review_id")), _rec("invoice", r.get("invoice_id")))})
    forced = [c["claim"] for c in claims if c["basis"] == "gate"]
    ai = {"suggested": vin.get("agent_decision"), "final": verdict.get("decision"), "confidence": verdict.get("confidence"),
          "model": vin.get("model"), "steps": vin.get("steps", len(steps)), "overruled_by_code": bool(forced),
          "note": "The AI only suggests. Code checks, fraud gates and people decide."}
    fr = conn.execute("SELECT id, decision, inputs FROM audit_log WHERE entity_id = %s AND stage = 'fraud' "
                      "AND reason LIKE 'Fraud risk %%' ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
    fraud = ({"risk": fr[1], "score": (fr[2] or {}).get("score"), "signals": (fr[2] or {}).get("signals") or [],
              "record": _rec("audit_log", fr[0])} if fr else None)
    code_claims = [c for c in claims if c["basis"] == "code_check"]
    stages = {
        "code": {"passed": sum(c["supported"] for c in code_claims), "failed": sum(not c["supported"] for c in code_claims),
                 "total": len(code_claims)},
        "fraud": {"risk": fraud["risk"] if fraud else None, "signals": len(fraud["signals"]) if fraud else 0},
        "ai": {"suggested": vin.get("agent_decision"), "ran": bool(vin.get("agent_decision") or vin.get("steps"))},
        "gate": {"overruled": bool(forced)},
        "human": {"decision": human["decision"] if human else None, "reviewer": human["reviewer"] if human else None},
    }
    reasons = verdict.get("reasons") or []
    if status == "paid":
        rec = "Paid. Nothing further to do. The ledger and reconcile show whether PayPal agrees."
    elif human and human["decision"] == "reject" or status in ("rejected", "cancelled"):
        rec = "Closed without payment."
    elif status == "approved":
        rec = "Cleared by every check. It can be paid, subject to the company's pay policy."
    elif status == "held":
        rec = "Do not pay yet. A person must look at the failed checks below and approve or reject."
    else:
        rec = "Waiting for the next step."
    summary = (f"Verify decided '{verdict.get('decision')}'"
               + (f" because of: {', '.join(reasons)}." if reasons else ": every check passed.")
               + (" The AI's suggestion was overruled by code." if forced else ""))
    return {"invoice_id": str(iid), "invoice_number": number, "vendor": vendor_name, "amount": str(Decimal(str(amount))),
            "currency": currency, "status": status, "verified": True, "verified_at": verify["at"], "summary": summary,
            "decision": verdict.get("decision"), "reasons": reasons, "claims": claims, "steps": steps,
            "precedents": precedents, "ai": ai, "human": human, "audit_record": src,
            "fraud": fraud, "stages": stages, "recommendation": rec,
            "counts": {"supported": sum(c["supported"] for c in claims), "problems": sum(not c["supported"] for c in claims),
                       "total": len(claims)},
            "report_id": f"{number}-{verify['id']}", "generated_at": datetime.now(timezone.utc).isoformat(),
            "basis_legend": BASIS_LABEL}
