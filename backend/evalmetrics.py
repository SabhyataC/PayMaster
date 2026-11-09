"""Evaluation metrics beyond precision and recall. Pure functions over a list of per-invoice results.

Each result is a dict with: scenario, is_bad (0/1), expected (approve/hold/reject), decision (final),
reasons (reason codes in the verdict), failed_checks (names of evidence items that failed),
agent_decision (what the AI said before the code gates; None if the AI failed), confidence (0..1).

Three families:
  investigation accuracy  was the decision right?  (final pipeline vs the AI alone, plus the lift from the code layers)
  evidence correctness    was the decision backed by the RIGHT evidence, and no false evidence on clean invoices?
  confidence calibration  does a stated 90% mean right 90% of the time?
"""

# Which reason codes count as the right evidence for each kind of bad invoice. Any one of them is enough.
EXPECTED_REASONS = {
    "no_po": {"po_not_found"},
    "no_delivery": {"delivery_not_received_yet"},
    "changed_bank": {"bank_details_changed", "fraud_bank_details_differ", "fraud_bank_change_recent"},
    "amount_mismatch": {"amount_out_of_tolerance", "quantity_out_of_tolerance"},
    "duplicate_copy": {"duplicate_invoice"},
    "lookalike_vendor": {"fraud_lookalike_vendor", "po_belongs_to_other_vendor"},   # a look-alike vendor shows up as a PO that is not theirs
    "scam_email": {"fraud_scam_wording"},
}
# Evidence items that are summaries or gates, not facts about the invoice, so they never count as false evidence.
DERIVED_CHECKS = {"agent_summary", "fraud_gate", "safety_gate", "reject_cap", "investigator_error"}


def _pct(a, b):
    return None if not b else a / b


def _stopped(decision):
    return decision != "approve"


def _correct(r):
    """The safe outcome: a bad invoice is stopped (held or rejected), a clean one is approved."""
    return _stopped(r["decision"]) if r["is_bad"] else r["decision"] == "approve"


def investigation_accuracy(results):
    n = len(results)
    final = sum(_correct(r) for r in results)
    exact = sum(r["decision"] == r["expected"] for r in results)
    with_agent = [r for r in results if r.get("agent_decision") is not None]
    agent_ok = sum((_stopped(r["agent_decision"]) if r["is_bad"] else r["agent_decision"] == "approve") for r in with_agent)
    final_on_same = sum(_correct(r) for r in with_agent)
    per = {}
    for r in results:
        p = per.setdefault(r["scenario"], {"n": 0, "correct": 0})
        p["n"] += 1
        p["correct"] += _correct(r)
    bad_rescued = sum(1 for r in with_agent if r["is_bad"] and not _stopped(r["agent_decision"]) and _stopped(r["decision"]))
    clean_spoiled = sum(1 for r in with_agent if not r["is_bad"] and r["agent_decision"] == "approve" and r["decision"] != "approve")
    return {
        "total": n, "accuracy": _pct(final, n), "exact_accuracy": _pct(exact, n),
        "agent_scored": len(with_agent), "agent_accuracy": _pct(agent_ok, len(with_agent)),
        "pipeline_accuracy_same_invoices": _pct(final_on_same, len(with_agent)),
        "bad_rescued_by_code": bad_rescued, "clean_blocked_by_code": clean_spoiled,
        "ai_unavailable": n - len(with_agent), "per_scenario": per,
    }


def evidence_correctness(results):
    bad = [r for r in results if r["is_bad"]]
    clean = [r for r in results if not r["is_bad"]]
    stopped_bad = [r for r in bad if _stopped(r["decision"])]
    right = [r for r in stopped_bad if set(r["reasons"]) & EXPECTED_REASONS.get(r["scenario"], set())]
    false_alarm = [r for r in clean if [c for c in r["failed_checks"] if c not in DERIVED_CHECKS]]
    stopped = [r for r in results if _stopped(r["decision"])]
    right_and_stopped = len(right)
    # an invoice is "evidence-correct" when its evidence tells the truth about it
    ok = sum(1 for r in bad if r in right) + sum(1 for r in clean if r["decision"] == "approve" and r not in false_alarm)
    missing = [r["eval_id"] if "eval_id" in r else r["scenario"] for r in stopped_bad if r not in right]
    return {
        "total": len(results),
        "evidence_correct": _pct(ok, len(results)),
        "right_reason_when_stopped": _pct(len(right), len(stopped_bad)),
        "stopped_for_right_reason": _pct(right_and_stopped, len(stopped)),
        "clean_with_false_evidence": len(false_alarm), "clean_total": len(clean),
        "bad_stopped": len(stopped_bad), "bad_stopped_wrong_reason": len(stopped_bad) - len(right),
        "wrong_reason_ids": missing,
    }


BINS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0001)]


def calibration(results, bins=BINS):
    """Group invoices by the confidence the verdict reported and compare with how often the decision was right."""
    rows, ece, brier, n = [], 0.0, 0.0, 0
    scored = [r for r in results if r.get("confidence") is not None]
    for lo, hi in bins:
        grp = [r for r in scored if lo <= r["confidence"] < hi]
        if not grp:
            rows.append({"range": _label(lo, hi), "n": 0, "mean_confidence": None, "accuracy": None, "gap": None})
            continue
        acc = sum(_correct(r) for r in grp) / len(grp)
        mc = sum(r["confidence"] for r in grp) / len(grp)
        rows.append({"range": _label(lo, hi), "n": len(grp), "mean_confidence": mc, "accuracy": acc, "gap": mc - acc})
        ece += len(grp) * abs(mc - acc)
    for r in scored:
        brier += (r["confidence"] - (1.0 if _correct(r) else 0.0)) ** 2
        n += 1
    overconfident = sum(1 for r in scored if r["confidence"] >= 0.9 and not _correct(r))
    return {"bins": rows, "n": n, "ece": ece / n if n else None, "brier": brier / n if n else None,
            "overconfident_wrong": overconfident,
            "distinct_confidences": len({round(r["confidence"], 2) for r in scored}),
            "note": ("Every invoice reported the same confidence, so this table cannot show calibration. "
                     "Run with the real AI to see it.") if n and len({round(r["confidence"], 2) for r in scored}) == 1 else ""}


def _label(lo, hi):
    return f"{lo:.1f} to {min(hi, 1.0):.1f}" + (" (incl.)" if hi > 1 else "")


def _pc(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def markdown(results, label):
    a, e, c = investigation_accuracy(results), evidence_correctness(results), calibration(results)
    out = [f"### Investigation accuracy ({label})", "",
           "| Measure | Value |", "|---|---|",
           f"| Decision accuracy (bad stopped, clean approved) | {_pc(a['accuracy'])} |",
           f"| Exact decision (hold vs reject also right) | {_pc(a['exact_accuracy'])} |",
           f"| AI alone, before the code gates | {_pc(a['agent_accuracy'])} on {a['agent_scored']} invoices |",
           f"| Whole pipeline on the same invoices | {_pc(a['pipeline_accuracy_same_invoices'])} |",
           f"| Bad invoices the code gates rescued from the AI | {a['bad_rescued_by_code']} |",
           f"| Clean invoices the code gates blocked | {a['clean_blocked_by_code']} |", "",
           f"### Evidence correctness ({label})", "",
           "| Measure | Value |", "|---|---|",
           f"| Invoices whose evidence is right | {_pc(e['evidence_correct'])} |",
           f"| Bad invoices stopped for the right reason | {_pc(e['right_reason_when_stopped'])} ({e['bad_stopped'] - e['bad_stopped_wrong_reason']} of {e['bad_stopped']}) |",
           f"| Stops that are right-reason stops | {_pc(e['stopped_for_right_reason'])} |",
           f"| Clean invoices shown false evidence | {e['clean_with_false_evidence']} of {e['clean_total']} |", "",
           f"### Confidence calibration ({label})", "",
           "| Confidence | Invoices | Mean confidence | Actually right | Gap |", "|---|---|---|---|---|"]
    for b in c["bins"]:
        out.append(f"| {b['range']} | {b['n']} | {_pc(b['mean_confidence'])} | {_pc(b['accuracy'])} | "
                   f"{'n/a' if b['gap'] is None else format(b['gap'] * 100, '+.1f') + ' pts'} |")
    out += ["", f"Expected calibration error: {_pc(c['ece'])}. Brier score: {'n/a' if c['brier'] is None else format(c['brier'], '.3f')}. "
            f"Wrong while at least 90% confident: {c['overconfident_wrong']}." + (f" {c['note']}" if c["note"] else "")]
    return "\n".join(out)


def all_metrics(results):
    return {"investigation_accuracy": investigation_accuracy(results), "evidence_correctness": evidence_correctness(results),
            "calibration": calibration(results)}
