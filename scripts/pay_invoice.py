"""
Pay one invoice by its number.

Usage (run from the repo root):
    python -m scripts.pay_invoice INV-2001            # runs the policy check; a new payee is held
    python -m scripts.pay_invoice INV-2001 owner      # a human named 'owner' releases the hold

Safe to run more than once: an invoice that already has a payment is never paid again.
"""

import sys

from backend.db import get_conn
from backend.pay import pay_invoice


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    number = sys.argv[1]
    approver = sys.argv[2] if len(sys.argv) > 2 else None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM invoices WHERE invoice_number = %s", (number,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"No invoice numbered {number}")
        print(pay_invoice(conn, row[0], approved_by=approver))


if __name__ == "__main__":
    main()