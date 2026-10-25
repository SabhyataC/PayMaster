"""Gate check (read-only): every success payment has one settlement row and balanced ledger rows."""
import sys

from backend.db import get_conn

SUM = "SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END)"


def main():
    problems = []
    with get_conn() as conn:
        unbalanced = conn.execute(
            f"SELECT payment_id, currency, {SUM} FROM ledger_entries "
            f"GROUP BY payment_id, currency HAVING {SUM} <> 0").fetchall()
        no_settlement = conn.execute(
            "SELECT p.id FROM payments p LEFT JOIN settlements s ON s.payment_id = p.id "
            "WHERE p.status = 'success' AND s.id IS NULL").fetchall()
        no_ledger = conn.execute(
            "SELECT p.id FROM payments p WHERE p.status = 'success' "
            "AND NOT EXISTS (SELECT 1 FROM ledger_entries l WHERE l.payment_id = p.id)").fetchall()
        n = conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'success'").fetchone()[0]
    problems += [f"unbalanced: payment {p} in {c} off by {d}" for p, c, d in unbalanced]
    problems += [f"no settlement row: payment {p[0]}" for p in no_settlement]
    problems += [f"no ledger rows: payment {p[0]}" for p in no_ledger]
    print(f"{n} success payment(s) checked")
    print("\n".join(problems) if problems else "GATE OK")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()