"""Print one invoice's full audit timeline:  python -m scripts.show_audit INV-2001"""
import sys

from backend.db import get_conn
from backend.timeline import find_invoice_id, get_timeline

if len(sys.argv) < 2:
    sys.exit("usage: python -m scripts.show_audit INV-2001")

with get_conn() as conn:
    inv = find_invoice_id(conn, sys.argv[1])
    if inv is None:
        sys.exit(f"no invoice {sys.argv[1]}")
    for e in get_timeline(conn, inv):
        print(f"{e['at'][:19]}  {e['stage']:<9} {e['decision']:<20} {e['actor']:<7} {e['reason'][:110]}")