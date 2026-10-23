"""The one verdict shape used by every later step (checks, agent, fraud, pay gate)."""

DECISIONS = ("approve", "hold", "reject")

# Stable reason codes, so later code can test for them instead of matching text.
# Reject = this invoice should not be paid as it stands.
# Hold   = a human or more information is needed.
REASON_PO_MISSING = "po_not_found"
REASON_PO_WRONG_VENDOR = "po_belongs_to_other_vendor"
REASON_DELIVERY_NOT_RECEIVED = "delivery_not_received_yet"   # hold, never reject
REASON_AMOUNT_OUT_OF_TOLERANCE = "amount_out_of_tolerance"
REASON_QUANTITY_OUT_OF_TOLERANCE = "quantity_out_of_tolerance"
REASON_DUPLICATE = "duplicate_invoice"
REASON_BANK_CHANGED = "bank_details_changed"
REASON_ALL_PASSED = "all_checks_passed"


def make_evidence(check, passed, detail, data=None):
    """One fact. `passed` is True/False, `detail` is a short plain sentence."""
    return {"check": check, "passed": bool(passed), "detail": detail, "data": data or {}}


def make_verdict(decision, reasons, evidence, confidence):
    """Build and validate a verdict. Returns a plain dict (safe for JSON and audit_log)."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    if decision in ("hold", "reject") and not reasons:
        raise ValueError("hold and reject need at least one reason")
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a number between 0 and 1")
    for e in evidence:
        if not {"check", "passed", "detail"} <= set(e):
            raise ValueError(f"bad evidence item: {e!r}")
    return {
        "decision": decision,
        "reasons": list(reasons),
        "evidence": list(evidence),
        "confidence": float(confidence),
    }