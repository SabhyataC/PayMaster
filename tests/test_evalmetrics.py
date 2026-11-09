"""Hand-checked tests for the evaluation metrics (no database needed):  python -m tests.test_evalmetrics"""
import sys

from backend import evalmetrics as em

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok)); print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if not ok else ""))


def r(scn, bad, decision, conf=0.9, agent="approve", reasons=(), failed=(), expected=None):
    return {"scenario": scn, "is_bad": bad, "decision": decision, "expected": expected or ("approve" if not bad else "hold"),
            "reasons": list(reasons), "failed_checks": list(failed), "agent_decision": agent, "confidence": conf, "eval_id": scn}


SET = [
    r("clean_plain", 0, "approve", 0.95, "approve"),                                              # right
    r("clean_plain", 0, "approve", 0.95, "approve"),                                              # right
    r("clean_plain", 0, "hold", 0.60, "hold", ["po_not_found"], ["po_exists"]),                   # clean wrongly held, shown false evidence
    r("no_po", 1, "reject", 0.40, "approve", ["po_not_found"], ["po_exists"], "reject"),          # bad, stopped by the code gate, right reason
    r("changed_bank", 1, "hold", 0.95, "hold", ["fraud_scam_wording"], ["fraud_signal:scam_wording"]),   # bad, stopped, WRONG reason
    r("scam_email", 1, "approve", 0.92, "approve"),                                               # bad approved, confident
]


def main():
    a = em.investigation_accuracy(SET)
    check("decision accuracy: 4 of 6 right (clean held and bad approved are wrong)", abs(a["accuracy"] - 4 / 6) < 1e-9, a["accuracy"])
    check("exact accuracy counts hold vs reject: 4 of 6", abs(a["exact_accuracy"] - 4 / 6) < 1e-9, a["exact_accuracy"])
    check("AI alone: 3 of 6 (two clean approvals + the bank change it stopped)", abs(a["agent_accuracy"] - 3 / 6) < 1e-9, a["agent_accuracy"])
    check("the code gate rescued exactly one bad invoice", a["bad_rescued_by_code"] == 1)
    check("the code gate blocked no clean invoice the AI had approved", a["clean_blocked_by_code"] == 0)
    check("per-scenario accuracy", a["per_scenario"]["clean_plain"] == {"n": 3, "correct": 2} and a["per_scenario"]["scam_email"]["correct"] == 0)

    e = em.evidence_correctness(SET)
    check("2 bad invoices were stopped; 1 cited the right reason", e["bad_stopped"] == 2 and e["bad_stopped_wrong_reason"] == 1, e)
    check("right reason when stopped: 50%", e["right_reason_when_stopped"] == 0.5)
    check("1 of 3 clean invoices was shown false evidence", e["clean_with_false_evidence"] == 1 and e["clean_total"] == 3)
    check("evidence-correct invoices: 2 clean + the no_po = 3 of 6", abs(e["evidence_correct"] - 3 / 6) < 1e-9, e["evidence_correct"])
    check("derived checks (gates, agent summary) never count as false evidence",
          em.evidence_correctness([r("clean_plain", 0, "approve", 0.9, "approve", [], ["safety_gate", "agent_summary"])])["clean_with_false_evidence"] == 0)

    c = em.calibration(SET)
    by = {b["range"]: b for b in c["bins"]}
    check("bins hold the right counts", [b["n"] for b in c["bins"]] == [1, 1, 0, 4], [b["n"] for b in c["bins"]])
    check("0.9+ bin: mean 94.25%, right 3 of 4", abs(by["0.9 to 1.0 (incl.)"]["mean_confidence"] - 0.9425) < 1e-9 and by["0.9 to 1.0 (incl.)"]["accuracy"] == 0.75)
    check("0.5-0.7 bin: confidence .60, clean wrongly held is wrong -> 0% right", by["0.5 to 0.7"]["accuracy"] == 0.0 and by["0.5 to 0.7"]["n"] == 1)
    check("0.0-0.5 bin: confidence .40, the reject was right -> 100%", by["0.0 to 0.5"]["accuracy"] == 1.0)
    exp = (1 * abs(0.40 - 1.0) + 1 * abs(0.60 - 0.0) + 4 * abs(0.9425 - 0.75)) / 6
    check("expected calibration error is the weighted gap", abs(c["ece"] - exp) < 1e-9, (c["ece"], exp))
    brier = ((0.95 - 1) ** 2 + (0.95 - 1) ** 2 + (0.60 - 0) ** 2 + (0.40 - 1) ** 2 + (0.95 - 1) ** 2 + (0.92 - 0) ** 2) / 6
    check("Brier score matches a hand calculation", abs(c["brier"] - brier) < 1e-9, (c["brier"], brier))
    check("wrong while 90%+ confident: only the approved scam", c["overconfident_wrong"] == 1)
    flat = em.calibration([r("clean_plain", 0, "approve", 1.0), r("no_po", 1, "reject", 1.0)])
    check("identical confidences are flagged as uninformative", "cannot show calibration" in flat["note"])
    check("empty input does not crash", em.calibration([])["ece"] is None and em.investigation_accuracy([])["accuracy"] is None and em.evidence_correctness([])["evidence_correct"] is None)
    md = em.markdown(SET, "unit")
    check("the markdown report has all three tables", all(t in md for t in ("Investigation accuracy", "Evidence correctness", "Confidence calibration")))

    from scripts.eval import failed_checks
    v = {"evidence": [{"check": "po_exists", "passed": False}, {"check": "fraud_signal:new_payee", "passed": False}, {"check": "fraud_signal:scam_wording", "passed": False}, {"check": "bank_unchanged", "passed": True}]}
    fr = {"signals": [{"signal": "new_payee", "severity": "low"}, {"signal": "scam_wording", "severity": "high"}]}
    check("low fraud signals are context, not evidence against the invoice", failed_checks(v, fr) == ["po_exists", "fraud_signal:scam_wording"], failed_checks(v, fr))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed"); sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
