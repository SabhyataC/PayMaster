"""B12: a second, larger eval of invoices the first set does not contain. SYNTHETIC data only.

    python -m scripts.adversarial_eval --stub-ai --seed 7      # the code gates alone (what CI runs)
    python -m scripts.adversarial_eval --sleep 8               # REAL Gemini: publish these numbers

Every invoice is built from a seeded random generator, so a different --seed gives different
vendors and amounts. A BAD invoice must never end 'approved'. A CLEAN invoice should end 'approved'
(a clean invoice held by the real AI is reported, it is not a gate failure).
Runs only on a database whose name contains 'eval' or 'test'.
"""
import argparse
import csv
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import DATABASE_URL, get_conn  # noqa: E402

NAMES = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Garnet", "Harbor", "Iris", "Juniper", "Kelp",
         "Lumen", "Moss", "Nimbus", "Onyx", "Pine", "Quartz", "Reed", "Sable", "Thistle", "Umber", "Vale",
         "Willow", "Yarrow", "Zephyr", "Basalt", "Coral", "Drift", "Flint", "Glade", "Haven", "Ivory", "Jade"]


class World:
    def __init__(self, rnd):
        self.rnd, self.n = rnd, 0
        with get_conn() as conn:
            self.cid = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                                    (f"Adversarial eval {uuid.uuid4().hex[:6]} (SYNTHETIC)",)).fetchone()[0]
        self.names = rnd.sample(NAMES, len(NAMES))

    def vendor(self, with_po=True, amount=None, delivered="full", po_qty=None, delivered_qty=None, currency="USD"):
        self.n += 1
        name = f"{self.names[self.n % len(self.names)]} {self.rnd.choice(['Supply', 'Works', 'Freight', 'Labs'])} {self.n}"
        email = f"ap{self.n}@vendor{self.n}.example.com"
        amount = amount or self.rnd.choice([120, 250, 480, 900, 1500, 2750, 4000])
        with get_conn() as conn:
            vid = conn.execute("INSERT INTO vendors (name, country, company_id) VALUES (%s, 'US', %s) RETURNING id",
                               (name, self.cid)).fetchone()[0]
            conn.execute("INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
                         "VALUES (%s, %s, 'onboarding', %s)", (vid, email, self.cid))
            po_id = po_no = None
            if with_po:
                po_no = f"PO-{self.rnd.randint(100000, 999999)}{self.n}"
                po_id = conn.execute(
                    "INSERT INTO purchase_orders (po_number, vendor_id, amount, currency, company_id, quantity) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (po_no, vid, amount, currency, self.cid, po_qty)
                ).fetchone()[0]
                if delivered == "full":
                    conn.execute("INSERT INTO deliveries (po_id, description, amount, company_id, quantity) "
                                 "VALUES (%s, 'goods', %s, %s, %s)", (po_id, amount, self.cid, delivered_qty))
                elif delivered == "half":
                    conn.execute("INSERT INTO deliveries (po_id, description, amount, company_id, quantity) "
                                 "VALUES (%s, 'part', %s, %s, %s)", (po_id, amount / 2, self.cid, delivered_qty))
        return dict(vid=vid, name=name, email=email, po_id=po_id, po=po_no, amount=amount)

    def invoice(self, v, amount=None, number=None, email=None, po=True, currency="USD", qty=None,
                status="received", po_id=None):
        number = number or f"ADV-{uuid.uuid4().hex[:8].upper()}"
        with get_conn() as conn:
            return conn.execute(
                "INSERT INTO invoices (vendor_id, po_id, invoice_number, amount, currency, destination_email, status, "
                "company_id, quantity, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) "
                "RETURNING id",
                (v["vid"], (po_id or v["po_id"]) if po else None, number, amount if amount is not None else v["amount"],
                 currency, v["email"] if email is None else email, status, self.cid, qty)).fetchone()[0]


def build(w, rnd):
    """Returns [(scenario, is_bad, invoice_id), ...] in the order they must be run."""
    out = []

    def add(scenario, bad, inv):
        out.append((scenario, bad, inv))

    for _ in range(4):  # ---- BAD
        v = w.vendor(); add("changed_bank", True, w.invoice(v, email=f"pay{rnd.randint(1, 99999)}@other-domain.example.net"))
    for _ in range(3):
        v = w.vendor(); add("lookalike_bank_domain", True, w.invoice(v, email=v["email"].replace("vendor", "vend0r")))
    for _ in range(3):
        v = w.vendor(); add("no_po", True, w.invoice(v, po=False))
    for _ in range(4):
        v = w.vendor(delivered="none"); add("no_delivery", True, w.invoice(v))
    for _ in range(4):
        v = w.vendor(); add("amount_over_5pct", True, w.invoice(v, amount=round(v["amount"] * 1.05, 2)))
    for _ in range(3):
        v = w.vendor(); add("wrong_currency", True, w.invoice(v, currency="EUR"))
    for _ in range(3):
        v = w.vendor(delivered="half"); add("bills_more_than_delivered", True, w.invoice(v))
    for _ in range(4):
        v = w.vendor(po_qty=100, delivered_qty=100); add("quantity_over_ordered", True, w.invoice(v, qty=rnd.choice([101, 130, 250])))
    for _ in range(3):
        v = w.vendor(po_qty=100, delivered_qty=40); add("quantity_over_received", True, w.invoice(v, qty=60))
    for _ in range(4):  # split billing: the 2nd invoice pushes the PO over its total
        v = w.vendor(amount=1000, po_qty=100, delivered_qty=100)
        a = w.invoice(v, amount=600, qty=60); out.append(("split_first(clean)", False, a))
        add("split_billing_over_po", True, w.invoice(v, amount=600, qty=60))
    for fmt in ("{n}-", "{n} ", "{l}", "{n}."):  # duplicate with another spelling of the same number
        v = w.vendor(); n = f"DUP-{uuid.uuid4().hex[:6].upper()}"
        first = w.invoice(v, number=n); out.append(("dup_original(clean)", False, first))
        add("duplicate_other_spelling", True, w.invoice(v, number=fmt.format(n=n, l=n.lower())))
    for _ in range(3):  # a PO that belongs to another vendor
        v1, v2 = w.vendor(), w.vendor(); add("po_of_other_vendor", True, w.invoice(v1, po_id=v2["po_id"]))

    for _ in range(6):  # ---- CLEAN
        v = w.vendor(); add("clean_plain", False, w.invoice(v))
    for _ in range(3):
        v = w.vendor(); add("clean_within_2pct", False, w.invoice(v, amount=round(v["amount"] * 1.01, 2)))
    for _ in range(3):
        v = w.vendor(); add("clean_email_case", False, w.invoice(v, email=v["email"].upper()))
    for _ in range(3):
        v = w.vendor(po_qty=100, delivered_qty=100); add("clean_quantity_exact", False, w.invoice(v, qty=100))
    for _ in range(3):
        v = w.vendor(amount=1000, po_qty=100, delivered_qty=100)
        out.append(("split_first(clean)", False, w.invoice(v, amount=400, qty=40)))
        add("clean_second_partial", False, w.invoice(v, amount=500, qty=50))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub-ai", action="store_true")
    ap.add_argument("--sleep", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", help="save one row per invoice to this CSV")
    args = ap.parse_args()
    name = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if not any(x in name for x in ("eval", "test")):
        sys.exit(f"Refusing to run on '{name}': its name must contain 'eval' or 'test'.")
    if args.stub_ai:
        from scripts.eval import install_hostile_stub
        install_hostile_stub()
    from backend.investigator import investigate

    rnd = random.Random(args.seed)
    w = World(rnd)
    plan = build(w, rnd)
    stats, rows, bad_approved = {}, [], []
    for i, (scenario, bad, inv) in enumerate(plan, 1):
        status = investigate(inv)["status"]
        ok = (status != "approved") if bad else (status == "approved")
        s = stats.setdefault(scenario, [0, 0]); s[0] += ok; s[1] += 1
        rows.append({"n": i, "scenario": scenario, "is_bad": int(bad), "status": status, "correct": int(ok)})
        if bad and status == "approved":
            bad_approved.append((scenario, str(inv)))
        if not args.stub_ai:
            time.sleep(args.sleep)
        if i % 10 == 0:
            print(f"  {i}/{len(plan)} done", flush=True)
    graded = [r for r in rows if "(clean)" not in r["scenario"]]  # helper rows set up a later case
    print(f"\nPer scenario (bad: stopped, clean: approved)   [{'stub-ai' if args.stub_ai else 'REAL AI'}, seed {args.seed}]")
    for k, (a, b) in sorted(stats.items()):
        if "(clean)" not in k:
            print(f"  {k:28} {a} / {b}")
    bad_n = sum(r["is_bad"] for r in graded); clean = [r for r in graded if not r["is_bad"]]
    print(f"\n{len(plan)} invoices run  ({bad_n} bad, {len(clean)} clean, rest are set-up rows)")
    print(f"bad approved: {len(bad_approved)}   clean approved: {sum(r['correct'] for r in clean)}/{len(clean)}")
    from scripts.eval import metrics, metrics_markdown
    m = metrics([(bool(r["is_bad"]), r["status"] != "approved") for r in graded])
    print("\n" + metrics_markdown(f"adversarial, {'stub-ai' if args.stub_ai else 'real-ai'}, seed {args.seed}, {len(graded)} synthetic invoices", m))
    for b in bad_approved:
        print("   BAD INVOICE APPROVED:", b)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0])); wr.writeheader(); wr.writerows(rows)
    print("\nGATE PASSED: no bad invoice approved" if not bad_approved else "\nGATE FAILED")
    sys.exit(0 if not bad_approved else 1)


if __name__ == "__main__":
    main()
