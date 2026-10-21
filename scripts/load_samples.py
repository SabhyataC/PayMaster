"""Store the SYNTHETIC sample documents in the extractions table as 'pending' rows.

Run from the repo root:
  python scripts/load_samples.py            (adds rows that are not there yet)
  python scripts/load_samples.py --reset    (deletes all synthetic extraction rows first)
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import get_conn  # noqa: E402


def main():
    reset = "--reset" in sys.argv
    data = json.load(open("samples/ground_truth.json", encoding="utf-8"))
    with get_conn() as conn:
        cur = conn.cursor()
        if reset:
            cur.execute("DELETE FROM extractions WHERE is_synthetic")
            print(f"reset: removed {cur.rowcount} old rows")
        added = 0
        for s in data["samples"]:
            path = Path(s["file"])
            name = path.name
            cur.execute("SELECT 1 FROM extractions WHERE source_name = %s", (name,))
            if cur.fetchone():
                print(f"[{s['id']}] already loaded, skipped")
                continue
            if s["format"] == "email":
                text = path.read_text(encoding="utf-8")
                raw, blob = text.encode("utf-8"), None
            else:
                text = None
                raw = blob = path.read_bytes()
            cur.execute(
                """INSERT INTO extractions
                   (source_name, source_format, source_text, source_bytes, source_sha256, status)
                   VALUES (%s, %s, %s, %s, %s, 'pending')""",
                (name, s["format"], text, blob, hashlib.sha256(raw).hexdigest()),
            )
            added += 1
            print(f"[{s['id']}] loaded {name} ({len(raw)} bytes)")
        conn.commit()
    print(f"done: {added} rows added")


if __name__ == "__main__":
    main()