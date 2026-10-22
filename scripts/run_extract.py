"""Run AI extraction on rows of the extractions table.

python scripts/run_extract.py 05       one sample by its number (prints the full result)
python scripts/run_extract.py --all    every pending row, with a pause between calls
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import get_conn  # noqa: E402
from backend.intake import extract_row, route_row  # noqa: E402


def find_rows(where, params=()):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT id, source_name FROM extractions WHERE {where} ORDER BY source_name", params)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]


def show_routing(r):
    print(f"   -> {r['status'].upper()}" + (f": {r['reason']}" if r["reason"] else ""))


def main():
    args = [a for a in sys.argv[1:] if a != "--route-only"]
    route_only = len(args) != len(sys.argv) - 1  # --route-only skips the AI call
    arg = args[0] if args else ""
    if arg == "--all":
        rows = find_rows("status IN ('extracted', 'needs_review')" if route_only
                        else "status IN ('pending', 'failed')")
    elif arg:
        rows = find_rows("source_name LIKE %s", (f"{arg}_%",))
    else:
        sys.exit("usage: python scripts/run_extract.py 05 | --all   (add --route-only to skip the AI call)")
    for row_id, name in rows:
        if route_only:
            print(f"{name}:")
            show_routing(route_row(row_id))
            continue
        res = extract_row(row_id)
        print(f"{name}: {'OK' if res['ok'] else 'FAILED'} ({res['model']})")
        if len(rows) == 1:
            print(json.dumps(res["fields"] if res["ok"] else res["reason"],indent=2))
            if res["ok"]:
                print("field confidence:", json.dumps(res["field_confidence"]))
                print("OVERALL:", res["confidence"], "| issues:", res["issues"] or "none")
        elif res["ok"]:
            f = res["fields"]
            print(f"   {f['invoice_number']} | po {f['po_number']} | due {f['due_date']} | total {f['total']}"
                f" | OVERALL {res['confidence']} {res['issues'] or ''}")
        else:
            print("   reason:", res["reason"])
        show_routing(res["routing"])
        if len(rows) > 1:
            time.sleep(5)  # stay under the free-tier rate limit


if __name__ == "__main__":
    main()