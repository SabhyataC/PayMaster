"""Run the explainer on the latest exception of each type and compare its suggested action
with the expected one. Read the explanations yourself too: a matching action can still come
with a wrong or incomplete explanation."""
import time

from backend.db import get_conn
from backend.explain import DEFAULT_ACTION, explain_exception

with get_conn() as conn:
    rows = conn.execute(
        "SELECT DISTINCT ON (type) id, type FROM exceptions ORDER BY type, id DESC").fetchall()
    ok = 0
    for id_, type_ in rows:
        r = explain_exception(conn, id_)
        conn.commit()
        expected = DEFAULT_ACTION.get(type_, "escalate")
        hit = r["suggested_action"] == expected
        ok += hit
        print(f"#{id_} {type_} | source={r['source']} | action={r['suggested_action']} "
              f"| expected={expected} | {'MATCH' if hit else 'DIFFERENT'}")
        print("   ", r["explanation"])
        time.sleep(5)  # free-tier rate limit
    print(f"\nscore: {ok}/{len(rows)} actions match")
    missing = set(DEFAULT_ACTION) - {t for _, t in rows}
    if missing:
        print("types with no exception in the database (NOT tested):", ", ".join(sorted(missing)))