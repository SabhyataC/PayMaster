"""Run the Verify pipeline on the labeled evaluation set and print three numbers.

  python -m scripts.eval                    development split, real Gemini (slow on the free tier)
  python -m scripts.eval --stub-ai          development split, code layers only: the AI is replaced by a
                                            hostile stub that approves everything and sees no scam/look-alike
  python -m scripts.eval --holdout          the hold-out split. Run it ONCE, at the very end.
  python -m scripts.eval --out run.csv      also save one row per invoice

The three numbers:
  frauds caught                  bad invoices that ended held or rejected
  clean invoices wrongly held    clean invoices that did not end approved
  bad invoices wrongly approved  bad invoices that ended approved (this must be 0)

Like make_test_set.py this resets invoice statuses and clears the audit log, so it refuses to run
unless the database name contains 'eval' or 'test'. All data is SYNTHETIC.
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import DATABASE_URL, get_conn  # noqa: E402

CSV_PATH = Path("samples/eval_set.csv")
HOLDOUT_LOCK = Path("eval_runs/.holdout_used")


class _Part:
    function_call = None


class _Content:
    parts = [_Part()]


class _Candidate:
    content = _Content()


class _Reply:
    candidates = [_Candidate()]

    def __init__(self, text=None):
        self.text = text


def install_hostile_stub():
    """Replace both AI calls: the investigator always approves, the fraud AI never sees a problem.
    What is left is the code: hard checks, code signals, the plain-text scan and the gates."""
    import backend.fraud as fraud
    import backend.investigator as investigator

    def generate(contents, cfg):
        if getattr(cfg, "response_mime_type", None) == "application/json":
            return _Reply(json.dumps({"decision": "approve", "reasons": [],
                                      "explanation": "Stub AI: always approves.", "confidence": 1.0}))
        return _Reply()  # no tool calls

    investigator._generate = generate
    fraud._ask_json = lambda prompt: ({"lookalike": False, "reason": "stub"} if "look-alike" in prompt
                                      else {"scam_wording": False, "reason": "stub"})


def check_holdout_lock(path, now=None):
    """The hold-out set may be run once. Delete the lock file only if you truly mean to run it again."""
    if path.exists():
        sys.exit(f"The hold-out set was already used ({path.read_text().strip()}).\n"
                 f"Running it again would defeat its purpose. If you really must, delete {path}.")


def write_holdout_lock(path, mode, now=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{mode}, {(now or datetime.now()).isoformat(timespec='seconds')}")


def load_rows(split):
    with open(CSV_PATH, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["split"] == split]


def summarize(results):
    """results: list of dicts with is_bad, decision, status, expected, scenario, reasons."""
    bad = [r for r in results if r["is_bad"]]
    clean = [r for r in results if not r["is_bad"]]
    return {
        "bad_total": len(bad),
        "caught": sum(r["decision"] in ("hold", "reject") for r in bad),
        "bad_approved": sum(r["status"] == "approved" or r["decision"] == "approve" for r in bad),
        "clean_total": len(clean),
        "clean_held": sum(r["decision"] != "approve" for r in clean),
        "exact": sum(r["decision"] == r["expected"] for r in results),
        "total": len(results),
        "ai_errors": sum("investigator_failed" in r["reasons"] for r in results),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="run the hold-out split (once)")
    ap.add_argument("--stub-ai", action="store_true", help="replace the AI with a hostile stub")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between real-AI invoices")
    ap.add_argument("--out", help="save one row per invoice to this CSV")
    args = ap.parse_args()

    db_name = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(w in db_name.lower() for w in ("eval", "test")):
        sys.exit(f"Refusing to run on database '{db_name}': it resets statuses and clears the audit log, "
                 "so its name must contain 'eval' or 'test'.")
    split = "holdout" if args.holdout else "dev"
    mode = "stub-ai" if args.stub_ai else "real-ai"
    if args.holdout:
        check_holdout_lock(HOLDOUT_LOCK)
    if args.stub_ai:
        install_hostile_stub()
    from backend.investigator import investigate  # imported after the stub so nothing caches the real call

    rows = load_rows(split)
    with get_conn() as conn:
        conn.execute("UPDATE invoices SET status = 'received'")
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        order = {n: i for i, (n,) in enumerate(conn.execute(
            "SELECT invoice_number FROM invoices ORDER BY received_at").fetchall())}
        ids = dict(conn.execute("SELECT invoice_number, id FROM invoices").fetchall())
    rows.sort(key=lambda r: order[r["invoice_number"]])  # originals before their copies

    results = []
    print(f"Running {len(rows)} invoices ({split} split, {mode}) ...")
    for i, row in enumerate(rows, 1):
        out = investigate(ids[row["invoice_number"]])
        v = out["verdict"]
        results.append({**row, "is_bad": int(row["is_bad"]), "decision": v["decision"],
                        "status": out["status"], "reasons": v["reasons"]})
        print(f"  {i:2}/{len(rows)} {row['eval_id']} {row['scenario']:19} -> {v['decision']}", flush=True)
        if not args.stub_ai:
            time.sleep(args.sleep)  # free-tier rate limit

    s = summarize(results)
    print("\n" + "=" * 62)
    print(f"{split} split, {mode}, {s['total']} SYNTHETIC invoices")
    print("=" * 62)
    print(f"Frauds caught:                     {s['caught']} / {s['bad_total']}")
    print(f"Clean invoices wrongly held:       {s['clean_held']} / {s['clean_total']}")
    print(f"Bad invoices wrongly approved:     {s['bad_approved']}")
    print(f"(exact expected decision:          {s['exact']} / {s['total']})")
    if s["ai_errors"]:
        print(f"WARNING: {s['ai_errors']} invoice(s) hit investigator_failed (AI error). Those count as "
              "held, which flatters 'frauds caught' and hurts 'wrongly held'.")

    per = defaultdict(lambda: [0, 0])
    for r in results:
        per[r["scenario"]][1] += 1
        per[r["scenario"]][0] += (r["decision"] in ("hold", "reject")) if r["is_bad"] else (r["decision"] == "approve")
    print("\nPer scenario (bad: caught, clean: approved):")
    for name, (ok, n) in sorted(per.items()):
        print(f"  {name:20} {ok} / {n}")

    failures = [r for r in results if (r["is_bad"] and r["decision"] == "approve")
                or (not r["is_bad"] and r["decision"] != "approve")
                or r["decision"] != r["expected"]]
    if failures:
        print("\nEvery miss or surprise:")
        for r in failures:
            kind = ("BAD APPROVED" if r["is_bad"] and r["decision"] == "approve"
                    else "CLEAN HELD" if not r["is_bad"] else "different decision")
            print(f"  {r['eval_id']} {r['scenario']:19} expected {r['expected']:7} got {r['decision']:7} [{kind}]")
            print(f"      {r['note']}")
            print(f"      reasons: {', '.join(r['reasons']) or '-'}")

    print("\nGATE PASSED: no bad invoice approved" if s["bad_approved"] == 0
          else f"\nGATE FAILED: {s['bad_approved']} bad invoice(s) approved. Fix before moving on.")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["eval_id", "scenario", "is_bad", "expected", "decision", "status", "reasons"])
            for r in results:
                w.writerow([r["eval_id"], r["scenario"], r["is_bad"], r["expected"], r["decision"],
                            r["status"], "; ".join(r["reasons"])])
    if args.holdout:
        write_holdout_lock(HOLDOUT_LOCK, mode)
        print(f"\nHold-out used. Lock written to {HOLDOUT_LOCK}.")


if __name__ == "__main__":
    main()